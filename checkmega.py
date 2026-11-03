#!/usr/bin/env python3
"""Проверка mega.nz-ссылок (файлы и папки): жива, не существует или снята по жалобе.

    python3 checkmega.py --link "https://mega.nz/file/AbCdEf#key"
    python3 checkmega.py --newest 50                 # свежие меги из всего индекса
    python3 checkmega.py --watched                   # меги из чатов watch-листа
    python3 checkmega.py --chat -100123 --days 7     # один чат, окно по времени
    python3 checkmega.py --watched --force           # игнорировать кэш

Обёртка над публичным web-API MEGA (POST g.api.mega.co.nz/cs): файл — команда
g по хэндлу, папка — команда f с n=<хэндл>. Это те же запросы, что делает
браузер при открытии ссылки: скачивание не запускается, ключ из ссылки
серверу не уходит, аккаунт и телеграм-сессия не нужны. Ответ кладётся в
таблицу link_status; несуществующее (-9 ENOENT) и снятое по жалобе
(-16 EBLOCKED) считаются терминальными и повторно не проверяются (даже с
--force), живые перепроверяются не чаще раза в --max-age дней. Между живыми
запросами пауза --delay, EAGAIN/ERATELIMIT пережидаются повтором."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB, mega_link  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

API_URL = "https://g.api.mega.co.nz/cs"

TERMINAL = ("dead", "blocked")
_RETRY_CODES = (-3, -4)  # EAGAIN, ERATELIMIT — повторить позже

Transport = Callable[[str, dict], Any]


def _api_call(payload: dict, n: Optional[str] = None,
              transport: Optional[Transport] = None) -> Any:
    """POST к g.api.mega.co.nz/cs; у папок хэндл идёт query-параметром n=."""
    url = API_URL + "?id=0" + (f"&n={n}" if n else "")
    if transport is not None:
        return transport(url, payload)
    req = urllib.request.Request(
        url, data=json.dumps([payload]).encode("utf-8"),
        headers={"Content-Type": "text/plain;charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_result(res: Any) -> dict:
    """Ответ /cs -> поля link_status. Живой файл — dict с s/at, живая папка —
    dict с f; ошибки числом: файлы присылают [-16], папки — голое -9."""
    el = res[0] if isinstance(res, list) else res
    if isinstance(el, int):
        if el == -9:
            return {"status": "dead"}
        if el == -16:
            return {"status": "blocked"}
        if el in _RETRY_CODES:
            return {"status": "retry", "error": f"api {el}"}
        return {"status": "error", "error": f"api {el}"}
    if isinstance(el, dict):
        if "f" in el:
            return {"status": "ok", "nodes": len(el["f"])}
        return {"status": "ok", "size": el.get("s")}
    return {"status": "error", "error": "unexpected response"}


def check_one(kind: str, handle: str, attempts: int = 3, retry_wait: float = 2.0,
              transport: Optional[Transport] = None) -> dict:
    """Одна живая проверка; EAGAIN/ERATELIMIT спят и повторяют, несуществующее
    (-9) и снятое по жалобе (-16) — терминальные статусы, прочие сбои — error
    без падения прогона."""
    payload = ({"a": "g", "p": handle} if kind == "file"
               else {"a": "f", "c": 1, "r": 1})
    for attempt in range(1, attempts + 1):
        try:
            res = _api_call(payload, n=(handle if kind == "folder" else None),
                            transport=transport)
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:200]}
        out = _parse_result(res)
        if out["status"] != "retry":
            return out
        if attempt == attempts:
            return {"status": "error", "error": out["error"]}
        time.sleep(retry_wait * attempt)
    return {"status": "error", "error": "unreachable"}


def check_megas(
    urls: list[str],
    db: DB,
    max_age_days: float = 7.0,
    force: bool = False,
    delay: float = 0.6,
    live_cap: Optional[int] = None,
    transport: Optional[Transport] = None,
) -> dict:
    """Проверить список ссылок/текстов с кэшем link_status.

    Терминальные (dead/blocked) и свежие статусы берутся из кэша; живыми
    запросами добирается только протухшее. live_cap ограничивает число живых
    запросов за вызов, остальное уходит со status=unchecked.
    """
    uniq: dict[str, str] = {}
    for u in urls:
        parsed = mega_link(u)
        if parsed:
            uniq.setdefault(parsed[1], parsed[0])
    cached = db.link_status_map("mega", list(uniq))
    now = int(time.time())
    fresh = max_age_days * 86400
    results: list[dict] = []
    live = 0
    skipped: list[str] = []
    for handle, kind in uniq.items():
        row = cached.get(handle)
        terminal = bool(row) and row["status"] in TERMINAL
        if row and (terminal or (not force and now - row["checked_ts"] < fresh)):
            out = dict(row)
            out["from_cache"] = True
            results.append(out)
            continue
        if live_cap is not None and live >= live_cap:
            skipped.append(handle)
            results.append({"value": handle, "kind": kind,
                            "status": "unchecked", "reason": "live_cap"})
            continue
        if live and delay:
            time.sleep(delay)
        live += 1
        res = check_one(kind, handle, transport=transport)
        db.link_status_set("mega", handle, kind=kind, **res)
        results.append({"value": handle, "kind": kind, **res,
                        "checked_ts": now, "from_cache": False})
    return {"results": results, "live_checks": live,
            "skipped_cap": skipped, "count": len(results)}


def _urls_from_index(db: DB, args: argparse.Namespace) -> list[str]:
    chat_ids = None
    if args.watched:
        chat_ids = [w["chat_id"] for w in db.watch_list()] or [-1]
    since = int(time.time()) - args.days * 86400 if args.days else None
    if args.chat is not None:
        rows = db.mega_refs(limit=args.limit, chat_id=args.chat, since_ts=since)
    else:
        rows = db.mega_refs(limit=args.limit, chat_ids=chat_ids, since_ts=since)
    return [r["value"] for r in rows]


_STATUS_RU = {
    "ok": "ЖИВА",
    "dead": "НЕ ДОСТУПНА",
    "blocked": "УДАЛЕНА (жалоба)",
    "error": "ошибка",
    "unchecked": "не проверена",
}


def _fmt_size(size: Optional[int]) -> Optional[str]:
    if size is None:
        return None
    val = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if val < 1024 or unit == "ТБ":
            return f"{val:.0f} {unit}" if unit in ("Б", "КБ") else f"{val:.1f} {unit}"
        val /= 1024
    return None


def _print_report(rep: dict) -> None:
    for r in rep["results"]:
        st = _STATUS_RU.get(r["status"], (r["status"] or "?").upper())
        parts = [f"  {st:16}", r["value"]]
        if r.get("kind"):
            parts.append("файл" if r["kind"] == "file" else "папка")
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
        description="Проверка mega.nz-ссылок: жива/удалена (без аккаунта и скачивания)")
    ap.add_argument("--link", action="append", metavar="URL",
                    help="проверить конкретную ссылку (можно несколько раз)")
    ap.add_argument("--newest", type=int, metavar="N",
                    help="свежие N мега-ссылок из всего индекса")
    ap.add_argument("--watched", action="store_true",
                    help="меги из чатов watch-листа (режим по умолчанию)")
    ap.add_argument("--chat", type=int, metavar="ID",
                    help="меги одного чата")
    ap.add_argument("--days", type=int, metavar="N",
                    help="окно свежести по времени сообщений")
    ap.add_argument("--limit", type=int, default=50, metavar="N",
                    help="максимум ссылок за прогон (по умолчанию 50)")
    ap.add_argument("--max-age", type=float, default=7.0, metavar="DAYS",
                    help="перепроверка живых не чаще раза в D дней (по умолчанию 7)")
    ap.add_argument("--delay", type=float, default=0.6, metavar="SEC",
                    help="пауза между живыми запросами (по умолчанию 0.6с)")
    ap.add_argument("--force", action="store_true",
                    help="игнорировать возраст кэша (dead/blocked не перепроверяются никогда)")
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
                print("mega.nz-ссылок в индексе не нашлось")
                return
            print(f"проверяю {len(urls)} мега-ссылку(ок) из индекса")
        rep = check_megas(urls, db, max_age_days=args.max_age,
                          force=args.force, delay=args.delay)
    _print_report(rep)


if __name__ == "__main__":
    main()
