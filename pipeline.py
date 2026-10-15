#!/usr/bin/env python3
"""Автономный конвейер: sync (watch) -> media -> extract -> fuzzy -> export -> digest -> monitor.

    python3 pipeline.py                      # полный прогон по watch-листу
    python3 pipeline.py --pack services      # один пакет извлечения
    python3 pipeline.py --skip sync          # офлайн-шаги, без Telegram
    python3 pipeline.py --days-ahead 7 --send
    python3 pipeline.py --loop 30 --send     # демон: цикл каждые 30 минут

Шаги независимы: сбой одного не останавливает остальные — упавший шаг
отражается в отчёте и в коде выхода (1). Extract дожёвывает хвост сообщений
до конца (until_done). Для регулярности — либо --loop, либо cron; live.py
остаётся отдельным демоном для мгновенной подхватки новых сообщений.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

import sync as sync_mod  # noqa: E402
import media as media_mod  # noqa: E402
import extract as extract_mod  # noqa: E402
import fuzzy_dedup as fuzzy_mod  # noqa: E402
import export as export_mod  # noqa: E402
import digest as digest_mod  # noqa: E402
import monitor as monitor_mod  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))


async def run(db_path: str = DB_PATH, pack_id: str | None = None,
              days_ahead: int = 7, send: bool = False, skip_sync: bool = False,
              skip_fuzzy: bool = False, skip_export: bool = False,
              skip_media: bool = False, client=None) -> dict:
    report: dict[str, dict] = {}
    if not skip_sync:
        try:
            rep = await sync_mod.run(db_path=db_path, client=client, watched_only=True)
            report["sync"] = {
                "ok": True,
                "chats": len({r["chat_id"] for r in rep["chats"]}),
                "new_messages": sum(r["new_messages"] for r in rep["chats"]),
            }
        except Exception as exc:
            report["sync"] = {"ok": False, "error": str(exc)}
    if not skip_media:
        try:
            med = await media_mod.run_async(db_path, client=client)
            report["media"] = {"ok": True, **med}
        except Exception as exc:
            report["media"] = {"ok": False, "error": str(exc)}
    try:
        made = extract_mod.run(db_path, pack_id=pack_id, until_done=True)
        report["extract"] = {
            "ok": True,
            "areas": len(made),
            "ops_applied": sum(m["ops_applied"] for m in made),
            "ops_overflow": sum(m["ops_overflow"] for m in made),
        }
    except Exception as exc:
        report["extract"] = {"ok": False, "error": str(exc)}
    if not skip_fuzzy:
        try:
            fz = fuzzy_mod.run(db_path, pack=pack_id,
                               max_merges=int(os.environ.get("FUZZY_MAX_MERGES") or 500))
            report["fuzzy"] = {"ok": True, "clusters": fz["clusters"],
                               "merged": fz["merged"]}
        except Exception as exc:
            report["fuzzy"] = {"ok": False, "error": str(exc)}
    if not skip_export:
        try:
            ex = export_mod.run(db_path, system="file")
            report["export"] = {"ok": bool(ex.get("ok")),
                                "exported": ex.get("exported"),
                                "path": ex.get("path")}
            if not ex.get("ok"):
                report["export"]["error"] = ex.get("error")
        except Exception as exc:
            report["export"] = {"ok": False, "error": str(exc)}
    try:
        digests = digest_mod.run(db_path)
        report["digest"] = {"ok": True, "made": len(digests)}
    except Exception as exc:
        report["digest"] = {"ok": False, "error": str(exc)}
    try:
        mon = monitor_mod.run(db_path, days_ahead=days_ahead, send=send,
                              dry_run=not send)
        report["monitor"] = {
            "ok": True,
            "overdue": len(mon["overdue"]),
            "upcoming": len(mon["upcoming"]),
            "lines": mon["lines"],
            "notify": mon["notify"],
        }
    except Exception as exc:
        report["monitor"] = {"ok": False, "error": str(exc)}
    return report


async def run_loop(db_path: str, pack_id: str | None, days_ahead: int,
                   send: bool, minutes: int) -> None:
    while True:
        stamp = time.strftime("%H:%M:%S")
        report = await run(db_path, pack_id=pack_id, days_ahead=days_ahead, send=send)
        for name, res in report.items():
            state = "ok" if res.get("ok") else f"FAIL {res.get('error')}"
            details = "  ".join(f"{k}={v}" for k, v in res.items()
                                if k not in ("ok", "lines", "notify", "error"))
            print(f"[{stamp}] {name}: {state}  {details}")
        print(f"[{stamp}] сплю {minutes} мин до следующего цикла (Ctrl+C — выход)")
        await asyncio.sleep(minutes * 60)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="sync -> extract -> digest -> monitor одним прогоном")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--pack", help="один пакет извлечения (иначе все)")
    ap.add_argument("--days-ahead", type=int, default=7,
                    help="горизонт сроков в сводке мониторинга")
    ap.add_argument("--send", action="store_true",
                    help="отправить сводку в Saved Messages (нужен TG_ALLOW_SEND=1)")
    ap.add_argument("--skip", action="append", default=[],
                    choices=["sync", "media", "fuzzy", "export"],
                    metavar="STEP", help="пропустить шаг (sync/media/fuzzy/export)")
    ap.add_argument("--loop", type=int, metavar="MIN",
                    help="демон: повторять цикл каждые N минут (cron не нужен)")
    args = ap.parse_args()
    if args.loop:
        asyncio.run(run_loop(args.db, args.pack, args.days_ahead,
                             args.send, args.loop))
        return
    report = asyncio.run(run(args.db, pack_id=args.pack, days_ahead=args.days_ahead,
                             send=args.send, skip_sync="sync" in args.skip,
                             skip_media="media" in args.skip,
                             skip_fuzzy="fuzzy" in args.skip,
                             skip_export="export" in args.skip))
    for name, res in report.items():
        if res.get("ok"):
            details = "  ".join(f"{k}={v}" for k, v in res.items()
                                if k not in ("ok", "lines", "notify"))
            print(f"  {name}: ok  {details}")
            if name == "monitor":
                for line in res["lines"]:
                    print(f"    {line}")
        else:
            print(f"  {name}: FAIL  {res.get('error')}", file=sys.stderr)
    if any(not res.get("ok") for res in report.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
