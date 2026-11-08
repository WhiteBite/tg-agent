#!/usr/bin/env python3
"""Сущностные алерты: новые сущности сразу после шага extract в конвейере.

    TG_ENTITY_ALERTS=1 python3 pipeline.py --skip sync   # шаг alerts после extract

Берёт открытые сущности с событиями op='new' новее курсора
`entity_alerts:cursor`, оставляет виды из TG_ENTITY_ALERT_KINDS (по умолчанию
trial,question), строит короткие строки и шлёт их в Saved Messages через
notify.py (реальная отправка — TG_ALLOW_SEND=1, иначе dry-run). Курсор
двигается только после успешной реальной отправки, поэтому dry-run ничего не
съедает.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import notify as notify_mod

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))
CURSOR_KEY = "entity_alerts:cursor"
DEFAULT_KINDS = "trial,question"
SCAN_LIMIT = 200


def _gate_on() -> bool:
    return os.environ.get("TG_ENTITY_ALERTS", "").strip().lower() in notify_mod.ALLOWED


def _kinds() -> set[str]:
    raw = os.environ.get("TG_ENTITY_ALERT_KINDS", DEFAULT_KINDS)
    return {k.strip() for k in raw.split(",") if k.strip()}


def _due_note(due_ts, now: int) -> str:
    if not due_ts:
        return ""
    if due_ts < now:
        return " (просрочено)"
    days = (due_ts - now) // 86400
    return " (срок сегодня)" if days < 1 else f" (срок через {days} дн.)"


def collect(db_path: str | Path) -> tuple[list[dict], int]:
    kinds = _kinds()
    with DB(db_path) as db:
        cursor = int(db.get_meta(CURSOR_KEY) or 0)
        rows = [dict(r) for r in db.conn.execute(
            "SELECT ev.id AS event_id, e.* FROM entity_events ev "
            "JOIN entities e ON e.id = ev.entity_id "
            "WHERE ev.id > ? AND ev.op = 'new' AND e.closed_ts IS NULL "
            "ORDER BY ev.id LIMIT ?", (cursor, SCAN_LIMIT))]
    max_id = max((r["event_id"] for r in rows), default=cursor)
    return [r for r in rows if r["kind"] in kinds], max_id


def format_lines(items: list[dict], now: int) -> list[str]:
    if not items:
        return []
    lines = [f"Новые сущности ({len(items)}):"]
    for e in items:
        owner = f" · {e['owner']}" if e["owner"] else ""
        lines.append(f"  [{e['pack']}/{e['kind']}] {e['title'][:120]}"
                     f"{owner}{_due_note(e['due_ts'], now)}")
    return lines


def run(db_path: str | Path = DB_PATH, send: bool = False) -> dict:
    if not _gate_on():
        return {"ok": True, "skipped": True, "reason": "TG_ENTITY_ALERTS выключен"}
    items, max_id = collect(db_path)
    lines = format_lines(items, int(time.time()))
    result = notify_mod.notify(lines, dry_run=not send) if lines else {"ok": True, "sent": 0}
    if send and result.get("ok") and max_id > 0:
        with DB(db_path) as db:
            if max_id > int(db.get_meta(CURSOR_KEY) or 0):
                db.set_meta(CURSOR_KEY, str(max_id))
    return {"ok": True, "new": len(items), "lines": lines, "notify": result}


def main() -> None:
    send = "--send" in sys.argv[1:]
    res = run(DB_PATH, send=send)
    for line in res.get("lines", []):
        print(line)
    if res.get("skipped"):
        print(f"пропущено: {res['reason']}")
    elif send:
        print(res["notify"])


if __name__ == "__main__":
    main()
