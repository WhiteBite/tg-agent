#!/usr/bin/env python3
"""Автономный конвейер: sync (watch) -> media -> extract -> fuzzy -> export -> digest -> monitor.

    python3 pipeline.py                      # полный прогон по watch-листу
    python3 pipeline.py --pack services      # один пакет извлечения
    python3 pipeline.py --skip sync          # офлайн-шаги, без Telegram
    python3 pipeline.py --days-ahead 7 --send
    python3 pipeline.py --loop 30 --send     # демон: цикл каждые 30 минут

Шаги независимы: сбой одного не останавливает остальные — упавший шаг
отражается в отчёте и в коде выхода (1). Каждый шаг ограничен по времени
(PIPELINE_STEP_BUDGET, по умолчанию 900с): зависший шаг помечается
проваленным и не вешает демон. Extract дожёвывает хвост до конца
(until_done); в --loop — окно за цикл, курсоры делают прогоны
возобновляемыми. Для регулярности — либо --loop, либо cron; live.py
остаётся отдельным демоном для мгновенной подхватки новых сообщений.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

import proclock  # noqa: E402
from db import DB  # noqa: E402
import sync as sync_mod  # noqa: E402
import media as media_mod  # noqa: E402
import extract as extract_mod  # noqa: E402
import fuzzy_dedup as fuzzy_mod  # noqa: E402
import export as export_mod  # noqa: E402
import digest as digest_mod  # noqa: E402
import monitor as monitor_mod  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

DEFAULT_STEP_BUDGET = 900


def _step_budget() -> int:
    return int(os.environ.get("PIPELINE_STEP_BUDGET") or DEFAULT_STEP_BUDGET)


def _timeout_error(budget: int) -> str:
    return f"таймаут шага {budget}с (PIPELINE_STEP_BUDGET)"


def _step_err(exc: BaseException, budget: int) -> dict:
    if isinstance(exc, TimeoutError) and not str(exc):
        return {"ok": False, "error": _timeout_error(budget)}
    return {"ok": False, "error": str(exc)}


async def _call_step(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def _finish(kind: str, val) -> None:
        if not fut.done():
            fut.set_result((kind, val))

    def _worker() -> None:
        try:
            res = fn(*args, **kwargs)
        except BaseException as exc:
            payload = ("err", exc)
        else:
            payload = ("ok", res)
        try:
            loop.call_soon_threadsafe(_finish, *payload)
        except RuntimeError:
            pass

    threading.Thread(target=_worker, daemon=True, name="pipeline-step").start()
    kind, val = await fut
    if kind == "err":
        raise val
    return val


async def run(db_path: str = DB_PATH, pack_id: str | None = None,
              days_ahead: int = 7, send: bool = False, skip_sync: bool = False,
              skip_fuzzy: bool = False, skip_export: bool = False,
              skip_media: bool = False, client=None, until_done: bool = True,
              acquire_lock: bool = True) -> dict:
    lock = None
    if acquire_lock:
        lock = proclock.acquire_instance_lock(str(db_path) + ".pipeline.lock")
        if lock is None:
            return {"already_running": True}
    budget = _step_budget()
    report: dict[str, dict] = {}
    if not skip_sync:
        try:
            rep = await asyncio.wait_for(
                sync_mod.run(db_path=db_path, client=client, watched_only=True),
                timeout=budget)
            report["sync"] = {
                "ok": True,
                "chats": len({r["chat_id"] for r in rep["chats"]}),
                "new_messages": sum(r["new_messages"] for r in rep["chats"]),
            }
        except Exception as exc:
            report["sync"] = _step_err(exc, budget)
    if not skip_media:
        try:
            med = await asyncio.wait_for(
                media_mod.run_async(db_path, client=client), timeout=budget)
            report["media"] = {"ok": True, **med}
        except Exception as exc:
            report["media"] = _step_err(exc, budget)
    try:
        made = await asyncio.wait_for(
            _call_step(extract_mod.run, db_path, pack_id=pack_id, until_done=until_done),
            timeout=budget)
        report["extract"] = {
            "ok": True,
            "areas": len(made),
            "ops_applied": sum(m["ops_applied"] for m in made),
            "ops_overflow": sum(m["ops_overflow"] for m in made),
            "ops_rejected": sum(m["ops_rejected"] for m in made),
        }
    except Exception as exc:
        report["extract"] = _step_err(exc, budget)
    if not skip_fuzzy:
        try:
            fz = await asyncio.wait_for(
                _call_step(fuzzy_mod.run, db_path, pack=pack_id,
                           max_merges=int(os.environ.get("FUZZY_MAX_MERGES") or 500)),
                timeout=budget)
            report["fuzzy"] = {"ok": True, "clusters": fz["clusters"],
                               "merged": fz["merged"]}
        except Exception as exc:
            report["fuzzy"] = _step_err(exc, budget)
    if not skip_export:
        try:
            ex = await asyncio.wait_for(
                _call_step(export_mod.run, db_path, system="file"),
                timeout=budget)
            report["export"] = {"ok": bool(ex.get("ok")),
                                "exported": ex.get("exported"),
                                "path": ex.get("path")}
            if not ex.get("ok"):
                report["export"]["error"] = ex.get("error")
        except Exception as exc:
            report["export"] = _step_err(exc, budget)
    try:
        digests = await asyncio.wait_for(
            _call_step(digest_mod.run, db_path), timeout=budget)
        report["digest"] = {"ok": True, "made": len(digests)}
    except Exception as exc:
        report["digest"] = _step_err(exc, budget)
    try:
        mon = await asyncio.wait_for(
            _call_step(monitor_mod.run, db_path,
                       days_ahead=days_ahead, send=send, dry_run=not send),
            timeout=budget)
        report["monitor"] = {
            "ok": True,
            "overdue": len(mon["overdue"]),
            "upcoming": len(mon["upcoming"]),
            "lines": mon["lines"],
            "notify": mon["notify"],
        }
    except Exception as exc:
        report["monitor"] = _step_err(exc, budget)
    if all(res.get("ok") for res in report.values()):
        with DB(db_path) as db:
            db.set_meta("pipeline:last_ok_ts", str(int(time.time())))
    return report


async def run_loop(db_path: str, pack_id: str | None, days_ahead: int,
                   send: bool, minutes: int) -> None:
    lock = proclock.acquire_instance_lock(str(db_path) + ".pipeline.lock")
    if lock is None:
        raise SystemExit("pipeline --loop уже запущен (lockfile занят)")
    while True:
        stamp = time.strftime("%H:%M:%S")
        report = await run(db_path, pack_id=pack_id, days_ahead=days_ahead,
                           send=send, until_done=False, acquire_lock=False)
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
    if report.get("already_running"):
        print("pipeline: уже запущен другой прогон (lockfile занят) — выходим")
        return
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
