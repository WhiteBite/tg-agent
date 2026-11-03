#!/usr/bin/env python3
"""Проверка Telegram-инвайт-ссылок (t.me/+…, t.me/joinchat/…): жива или истекла.

    python3 checklinks.py --link "https://t.me/+AbCdEf"
    python3 checklinks.py --newest 50                 # свежие инвайты из всего индекса
    python3 checklinks.py --watched                   # инвайты из чатов watch-листа
    python3 checklinks.py --chat -100123 --days 7     # один чат, окно по времени
    python3 checklinks.py --watched --force           # игнорировать кэш

Обёртка над messages.checkChatInvite: это тот же запрос, что делает клиент
Telegram при открытии ссылки (превью до кнопки «Присоединиться») — в чат он
НЕ вступает. Ответ кладётся в таблицу invite_status; истекшие/невалидные
считаются терминальными и повторно не проверяются (даже с --force), живые
перепроверяются не чаще раза в --max-age дней. Между живыми запросами пауза
--delay (за серию проверок Telegram отвечает INVITE_HASH_FLOOD)."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

import sync as sync_mod  # noqa: E402
from db import DB  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

_INVITE_RE = re.compile(r"(?:t|telegram)\.me/(?:\+([\w-]+)|joinchat/([\w-]+))", re.IGNORECASE)
_RAW_HASH_RE = re.compile(r"[\w-]+")

try:  # телега не обязательна в офлайн-тестах
    from telethon.errors import FloodWaitError as _FloodWait
    from telethon.errors import InviteHashExpiredError as _InviteExpired
    from telethon.errors import InviteHashInvalidError as _InviteInvalid
except Exception:  # pragma: no cover
    class _FloodWait(Exception):  # type: ignore[no-redef]
        pass

    class _InviteExpired(Exception):  # type: ignore[no-redef]
        pass

    class _InviteInvalid(Exception):  # type: ignore[no-redef]
        pass


TERMINAL = ("expired", "invalid")


def invite_hash(link: str) -> Optional[str]:
    """Хэш инвайта из любого вида: t.me/+AbCd, t.me/joinchat/AbCd, +AbCd,
    joinchat/AbCd, сырой AbCd, ссылка внутри произвольного текста."""
    if not link:
        return None
    m = _INVITE_RE.search(link)
    if m:
        return m.group(1) or m.group(2)
    s = link.strip()
    if s.startswith("+"):
        body = s[1:]
    elif s.lower().startswith("joinchat/"):
        body = s[len("joinchat/"):]
    else:
        body = s
    if body and _RAW_HASH_RE.fullmatch(body):
        return body
    return None


def _parse_result(res: Any) -> dict:
    """checkChatInvite -> поля invite_status. ChatInvite — превью (не участник),
    любой чат-объект — уже участник."""
    if type(res).__name__ == "ChatInvite":
        return {
            "status": "ok",
            "title": getattr(res, "title", None),
            "participants": getattr(res, "participants_count", None),
            "is_channel": bool(getattr(res, "broadcast", False)),
            "request_needed": bool(getattr(res, "request_needed", False)),
        }
    return {"status": "member", "title": getattr(res, "title", None)}


async def check_one(client: Any, hash_: str, attempts: int = 3) -> dict:
    """Одна живая проверка; FloodWait спит и повторяет, истёкший/невалидный
    хэш — терминальный статус, прочие сбои — error без падения прогона."""
    from telethon.tl import functions
    for attempt in range(1, attempts + 1):
        try:
            res = await client(functions.messages.CheckChatInviteRequest(hash=hash_))
            return _parse_result(res)
        except _InviteExpired:
            return {"status": "expired"}
        except _InviteInvalid:
            return {"status": "invalid"}
        except _FloodWait as exc:
            if attempt == attempts:
                return {"status": "error", "error": f"flood {getattr(exc, 'seconds', '?')}s"}
            wait = min(int(getattr(exc, "seconds", 0) or 0) + 1, 900)
            print(f"    FloodWait: сплю {wait}s (попытка {attempt}/{attempts})…", flush=True)
            await asyncio.sleep(wait)
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:200]}
    return {"status": "error", "error": "unreachable"}


async def check_invites(
    client: Any,
    hashes: list[str],
    db: DB,
    max_age_days: float = 7.0,
    force: bool = False,
    delay: float = 0.6,
    live_cap: Optional[int] = None,
    exclude_chat_id: Optional[int] = None,
) -> dict:
    """Проверить список ссылок/хэшей с кэшем invite_status.

    Терминальные (expired/invalid) и свежие статусы берутся из кэша; живыми
    запросами добирается только протухшее. live_cap ограничивает число живых
    запросов за вызов (защита от флуда), остальное уходит со status=unchecked.
    exclude_chat_id — инвайты, уже встречавшиеся в этом чате, не проверяются и
    возвращаются со status=duplicate (дедуп перед публикацией куда-то).
    """
    uniq = list(dict.fromkeys(h for h in (invite_hash(x) for x in hashes) if h))
    known: set[str] = set()
    if exclude_chat_id is not None:
        for r in db.invite_refs(limit=10**6, chat_id=exclude_chat_id):
            h = invite_hash(r["value"])
            if h:
                known.add(h)
    cached = db.invite_status_map(uniq)
    now = int(time.time())
    fresh = max_age_days * 86400
    results: list[dict] = []
    live = 0
    skipped: list[str] = []
    for h in uniq:
        if h in known:
            results.append({"value": h, "status": "duplicate",
                            "already_in": exclude_chat_id})
            continue
        row = cached.get(h)
        terminal = bool(row) and row["status"] in TERMINAL
        if row and (terminal or (not force and now - row["checked_ts"] < fresh)):
            out = dict(row)
            out["from_cache"] = True
            results.append(out)
            continue
        if live_cap is not None and live >= live_cap:
            skipped.append(h)
            results.append({"value": h, "status": "unchecked", "reason": "live_cap"})
            continue
        if live and delay:
            await asyncio.sleep(delay)
        live += 1
        res = await check_one(client, h)
        db.invite_status_set(h, **res)
        results.append({"value": h, **res, "checked_ts": now, "from_cache": False})
    return {"results": results, "live_checks": live,
            "skipped_cap": skipped, "count": len(results)}


def _hashes_from_index(db: DB, args: argparse.Namespace) -> list[str]:
    chat_ids = None
    if args.watched:
        chat_ids = [w["chat_id"] for w in db.watch_list()] or [-1]
    since = int(time.time()) - args.days * 86400 if args.days else None
    if args.chat is not None:
        rows = db.invite_refs(limit=args.limit, chat_id=args.chat, since_ts=since)
    else:
        rows = db.invite_refs(limit=args.limit, chat_ids=chat_ids, since_ts=since)
    return [r["value"] for r in rows]


_STATUS_RU = {
    "ok": "ЖИВА",
    "member": "ты уже участник",
    "expired": "ИСТЕКЛА",
    "invalid": "НЕВАЛИДНА",
    "duplicate": "ДУБЛЬ (уже в чате)",
    "unchecked": "не проверена",
}


def _print_report(rep: dict) -> None:
    for r in rep["results"]:
        h = r["value"]
        st = _STATUS_RU.get(r["status"], r["status"].upper() if r["status"] else "?")
        parts = [f"  {st:12}", h]
        if r.get("title"):
            parts.append(f"«{r['title']}»")
        if r.get("participants"):
            parts.append(f"{r['participants']} участников")
        if r.get("request_needed"):
            parts.append("по заявке")
        if r.get("error"):
            parts.append(str(r["error"]))
        if r.get("from_cache"):
            parts.append("(кэш)")
        print("  ".join(str(p) for p in parts))
    counts: dict[str, int] = {}
    for r in rep["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"итого: {rep['count']} (живых запросов {rep['live_checks']}) {counts}")
    if rep.get("skipped_cap"):
        print(f"не проверено из-за лимита: {len(rep['skipped_cap'])} — прогони ещё раз")


async def _amain(args: argparse.Namespace) -> int:
    with DB(DB_PATH) as db:
        if args.link:
            hashes: list[str] = args.link
        else:
            hashes = _hashes_from_index(db, args)
            if not hashes:
                print("инвайт-ссылок в индексе не нашлось")
                return 0
            print(f"проверяю {len(hashes)} инвайт(ов) из индекса")
        client = sync_mod.make_client()
        await client.start(**sync_mod.start_kwargs())
        try:
            rep = await check_invites(client, hashes, db, max_age_days=args.max_age,
                                      force=args.force, delay=args.delay,
                                      exclude_chat_id=args.exclude_chat)
        finally:
            await client.disconnect()
    _print_report(rep)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Проверка Telegram-инвайт-ссылок: жива/истекла (в чат не вступает)")
    ap.add_argument("--link", action="append", metavar="URL",
                    help="проверить конкретную ссылку (можно несколько раз)")
    ap.add_argument("--newest", type=int, metavar="N",
                    help="свежие N инвайтов из всего индекса")
    ap.add_argument("--watched", action="store_true",
                    help="инвайты из чатов watch-листа (режим по умолчанию)")
    ap.add_argument("--chat", type=int, metavar="ID",
                    help="инвайты одного чата")
    ap.add_argument("--days", type=int, metavar="N",
                    help="окно свежести по времени сообщений")
    ap.add_argument("--limit", type=int, default=50, metavar="N",
                    help="максимум ссылок за прогон (по умолчанию 50)")
    ap.add_argument("--max-age", type=float, default=7.0, metavar="DAYS",
                    help="перепроверка живых не чаще раза в D дней (по умолчанию 7)")
    ap.add_argument("--delay", type=float, default=0.6, metavar="SEC",
                    help="пауза между живыми запросами (по умолчанию 0.6с)")
    ap.add_argument("--force", action="store_true",
                    help="игнорировать возраст кэша (expired/invalid не перепроверяются никогда)")
    ap.add_argument("--exclude-chat", type=int, metavar="ID",
                    help="не нести инвайты, уже встречавшиеся в чате ID (дедуп перед публикацией)")
    args = ap.parse_args()
    if not (args.link or args.newest or args.watched or args.chat):
        args.watched = True
    if args.newest:
        args.limit = args.newest
        args.watched = False
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
