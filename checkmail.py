#!/usr/bin/env python3
"""Проверка cloud.mail.ru-ссылок (публичные «облака»): жива или удалена.

    python3 checkmail.py --link "https://cloud.mail.ru/public/AbCd12/EfGh34"
    python3 checkmail.py --newest 50                 # свежие облака из всего индекса
    python3 checkmail.py --watched                   # облака из чатов watch-листа
    python3 checkmail.py --chat -100123 --days 7     # один чат, окно по времени
    python3 checkmail.py --watched --force           # игнорировать кэш

Обёртка над публичным web-API облака (GET cloud.mail.ru/api/v2/folder?weblink=…):
тот же запрос, которым веб-интерфейс открывает публичную ссылку, — без аккаунта
и без скачивания. Живое «облако» отдаёт имя, размер и число файлов; удалённое
отвечает not_exists и считается терминальным (повторно не проверяется, даже с
--force), живое перепроверяется не чаще раза в --max-age дней. Ответ кладётся в
таблицу link_status (host='mailru'). Телеграм-сессия не нужна."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB, cloudmail_link  # noqa: E402
from checkmega import _fmt_size  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

API_URL = "https://cloud.mail.ru/api/v2/folder"

TERMINAL = ("dead",)
_RETRY_HTTP = (429, 500, 502, 503, 504)

Transport = Callable[[str], Any]


def _api_call(weblink: str, transport: Optional[Transport] = None) -> Any:
    """GET api/v2/folder по веблинку; тело 404 несёт not_exists, поэтому
    ошибочный статус возвращается как разобранный JSON, а не исключение."""
    url = API_URL + "?weblink=" + urllib.parse.quote("/" + weblink, safe="")
    if transport is not None:
        return transport(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return json.loads(exc.read().decode("utf-8", "replace"))
        raise


def _parse_result(res: Any) -> dict:
    """Ответ api/v2/folder -> поля link_status. Живое — body с name/size/count;
    удалённое — 404 c weblink.error=not_exists."""
    if not isinstance(res, dict):
        return {"status": "error", "error": "unexpected response"}
    body = res.get("body") or {}
    wb = body.get("weblink")
    if isinstance(wb, dict) and wb.get("error") == "not_exists":
        return {"status": "dead"}
    if isinstance(body, dict) and body.get("name") is not None:
        cnt = body.get("count") or {}
        return {"status": "ok", "kind": body.get("kind"),
                "title": body.get("name"), "size": body.get("size"),
                "nodes": (cnt.get("folders") or 0) + (cnt.get("files") or 0)}
    return {"status": "error", "error": "unexpected response"}


def check_one(weblink: str, attempts: int = 3, retry_wait: float = 2.0,
              transport: Optional[Transport] = None) -> dict:
    """Одна живая проверка; 429/5xx спят и повторяют, удалённое (not_exists) —
    терминальное, прочие сбои — error без падения прогона."""
    for attempt in range(1, attempts + 1):
        try:
            res = _api_call(weblink, transport=transport)
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRY_HTTP and attempt < attempts:
                time.sleep(retry_wait * attempt)
                continue
            return {"status": "error", "error": f"http {exc.code}"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:200]}
        return _parse_result(res)
    return {"status": "error", "error": "unreachable"}


def check_mailru(
    urls: list[str],
    db: DB,
    max_age_days: float = 7.0,
    force: bool = False,
    delay: float = 0.6,
    live_cap: Optional[int] = None,
    exclude_chat_id: Optional[int] = None,
    transport: Optional[Transport] = None,
) -> dict:
    """Проверить список ссылок/текстов с кэшем link_status (host='mailru').

    Терминальные (dead) и свежие статусы берутся из кэша; живыми запросами
    добирается только протухшее. live_cap ограничивает число живых запросов за
    вызов, остальное уходит со status=unchecked. exclude_chat_id — облака, уже
    встречавшиеся в этом чате, не проверяются и возвращаются со
    status=duplicate (дедуп перед публикацией куда-то).
    """
    uniq: dict[str, None] = dict.fromkeys(
        v for v in (cloudmail_link(u) for u in urls) if v)
    known: set[str] = set()
    if exclude_chat_id is not None:
        for r in db.mailru_refs(limit=10**6, chat_id=exclude_chat_id):
            v = cloudmail_link(r["value"])
            if v:
                known.add(v)
    cached = db.link_status_map("mailru", list(uniq))
    now = int(time.time())
    fresh = max_age_days * 86400
    results: list[dict] = []
    live = 0
    skipped: list[str] = []
    for weblink in uniq:
        if weblink in known:
            results.append({"value": weblink, "status": "duplicate",
                            "already_in": exclude_chat_id})
            continue
        row = cached.get(weblink)
        terminal = bool(row) and row["status"] in TERMINAL
        if row and (terminal or (not force and now - row["checked_ts"] < fresh)):
            out = dict(row)
            out["from_cache"] = True
            results.append(out)
            continue
        if live_cap is not None and live >= live_cap:
            skipped.append(weblink)
            results.append({"value": weblink, "status": "unchecked",
                            "reason": "live_cap"})
            continue
        if live and delay:
            time.sleep(delay)
        live += 1
        res = check_one(weblink, transport=transport)
        db.link_status_set("mailru", weblink, **res)
        results.append({"value": weblink, **res,
                        "checked_ts": now, "from_cache": False})
    return {"results": results, "live_checks": live,
            "skipped_cap": skipped, "count": len(results)}


def _urls_from_index(db: DB, args: argparse.Namespace) -> list[str]:
    chat_ids = None
    if args.watched:
        chat_ids = [w["chat_id"] for w in db.watch_list()] or [-1]
    since = int(time.time()) - args.days * 86400 if args.days else None
    if args.chat is not None:
        rows = db.mailru_refs(limit=args.limit, chat_id=args.chat, since_ts=since)
    else:
        rows = db.mailru_refs(limit=args.limit, chat_ids=chat_ids, since_ts=since)
    return [r["value"] for r in rows]


_STATUS_RU = {
    "ok": "ЖИВА",
    "dead": "НЕ ДОСТУПНА",
    "duplicate": "ДУБЛЬ (уже в чате)",
    "error": "ошибка",
    "unchecked": "не проверена",
}


def _print_report(rep: dict) -> None:
    for r in rep["results"]:
        st = _STATUS_RU.get(r["status"], (r["status"] or "?").upper())
        parts = [f"  {st:16}", r["value"]]
        if r.get("kind"):
            parts.append("папка" if r["kind"] == "folder" else "файл")
        if r.get("title"):
            parts.append(f"«{r['title']}»")
        size_s = _fmt_size(r.get("size"))
        if size_s:
            parts.append(size_s)
        if r.get("nodes") is not None:
            parts.append(f"{r['nodes']} объектов")
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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Проверка cloud.mail.ru-ссылок: жива/удалена (без аккаунта и скачивания)")
    ap.add_argument("--link", action="append", metavar="URL",
                    help="проверить конкретную ссылку (можно несколько раз)")
    ap.add_argument("--newest", type=int, metavar="N",
                    help="свежие N облаков из всего индекса")
    ap.add_argument("--watched", action="store_true",
                    help="облака из чатов watch-листа (режим по умолчанию)")
    ap.add_argument("--chat", type=int, metavar="ID",
                    help="облака одного чата")
    ap.add_argument("--days", type=int, metavar="N",
                    help="окно свежести по времени сообщений")
    ap.add_argument("--limit", type=int, default=50, metavar="N",
                    help="максимум ссылок за прогон (по умолчанию 50)")
    ap.add_argument("--max-age", type=float, default=7.0, metavar="DAYS",
                    help="перепроверка живых не чаще раза в D дней (по умолчанию 7)")
    ap.add_argument("--delay", type=float, default=0.6, metavar="SEC",
                    help="пауза между живыми запросами (по умолчанию 0.6с)")
    ap.add_argument("--force", action="store_true",
                    help="игнорировать возраст кэша (dead не перепроверяются никогда)")
    ap.add_argument("--exclude-chat", type=int, metavar="ID",
                    help="не нести облака, уже встречавшиеся в чате ID (дедуп перед публикацией)")
    args = ap.parse_args()
    if not (args.link or args.newest or args.watched or args.chat):
        args.watched = True
    if args.newest:
        args.limit = args.newest
        args.watched = False
    with DB(DB_PATH) as db:
        if args.link:
            urls: list[str] = args.link
        else:
            urls = _urls_from_index(db, args)
            if not urls:
                print("cloud.mail.ru-ссылок в индексе не нашлось")
                return
            print(f"проверяю {len(urls)} облачных ссылку(ок) из индекса")
        rep = check_mailru(urls, db, max_age_days=args.max_age,
                           force=args.force, delay=args.delay,
                           exclude_chat_id=args.exclude_chat)
    _print_report(rep)


if __name__ == "__main__":
    main()
