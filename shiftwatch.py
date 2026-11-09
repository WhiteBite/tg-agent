#!/usr/bin/env python3
"""Контроль срыва сроков: сдвиг due_ts в entity_events -> задача-перепроверка.

    python3 shiftwatch.py                 # dry-run: показать, что было бы создано
    python3 shiftwatch.py --apply         # создать контроллеров

Скан entity_events на отложенные сроки (field='due_ts', from_val < to_val) по
открытым сущностям за окно days; на каждую такую сущность заводится одна
открытая задача «Контрольно уточнить: …» со сроком now + recheck_days*86400.
Дедуп — по (pack, dedup_key=sha1("controller|<entity_id>")): повторный прогон
дубликатов не даёт.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env

load_env()

from db import DB

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))


def _is_deferral(from_val, to_val) -> bool:
    try:
        return int(from_val) < int(to_val)
    except (TypeError, ValueError):
        return False


def _controller_dedup(entity_id: int) -> str:
    return hashlib.sha1(f"controller|{entity_id}".encode("utf-8")).hexdigest()


def _scan(db: DB, since_ts: int) -> list[dict]:
    rows = db.conn.execute(
        "SELECT ev.entity_id, ev.chat_id, ev.msg_id, ev.quote, ev.ts, "
        "ev.from_val, ev.to_val, e.pack, e.chat_id AS ent_chat, e.topic_id, "
        "e.title, e.owner, e.project "
        "FROM entity_events ev JOIN entities e ON e.id = ev.entity_id "
        "WHERE ev.field = 'due_ts' AND ev.from_val IS NOT NULL "
        "AND ev.to_val IS NOT NULL AND ev.ts >= ? AND e.closed_ts IS NULL "
        "ORDER BY ev.entity_id, ev.ts DESC, ev.id DESC",
        (since_ts,)).fetchall()
    triggers: dict[int, dict] = {}
    for r in rows:
        if r["entity_id"] in triggers:
            continue
        if _is_deferral(r["from_val"], r["to_val"]):
            triggers[r["entity_id"]] = dict(r)
    return list(triggers.values())


def _create_controller(db: DB, t: dict, dedup_key: str, recheck_ts: int,
                       owner: str | None, now: int) -> int:
    attrs = json.dumps({"controller_for": t["entity_id"]}, ensure_ascii=False)
    cur = db.conn.execute(
        "INSERT INTO entities(pack, chat_id, topic_id, kind, title, body, owner, "
        "due_ts, project, confidence, attrs_json, dedup_key, status, created_ts, "
        "updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["pack"], t["ent_chat"], t["topic_id"], "task",
         f"Контрольно уточнить: {t['title']}", None,
         owner if owner is not None else t["owner"], recheck_ts, t["project"],
         None, attrs, dedup_key, "open", now, now))
    controller_id = cur.lastrowid
    db.conn.execute(
        "INSERT INTO entity_sources(entity_id, chat_id, topic_id, first_seen_ts, "
        "last_seen_ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(entity_id, chat_id, topic_id) DO UPDATE SET "
        "first_seen_ts=MIN(entity_sources.first_seen_ts, excluded.first_seen_ts), "
        "last_seen_ts=MAX(entity_sources.last_seen_ts, excluded.last_seen_ts)",
        (controller_id, t["ent_chat"], t["topic_id"], now, now))
    db.conn.execute(
        "INSERT INTO entity_events(entity_id, run_id, ts, op, field, chat_id, "
        "msg_id, quote, actor) VALUES(?,?,?,?,?,?,?,?,?)",
        (controller_id, None, now, "new", None, t["chat_id"], t["msg_id"],
         t["quote"], "shiftwatch"))
    return controller_id


def run(db_path, days: int = 7, recheck_days: int = 2,
        owner: str | None = None, dry_run: bool = False) -> dict:
    now = int(time.time())
    since_ts = now - days * 86400
    recheck_ts = now + recheck_days * 86400
    created = 0
    skipped = 0
    items: list[dict] = []
    with DB(db_path) as db:
        triggers = _scan(db, since_ts)
        considered = len(triggers)
        for t in triggers:
            dedup_key = _controller_dedup(t["entity_id"])
            existing = db.conn.execute(
                "SELECT 1 FROM entities WHERE pack=? AND dedup_key=? "
                "AND closed_ts IS NULL", (t["pack"], dedup_key)).fetchone()
            if existing is not None:
                skipped += 1
                continue
            created += 1
            items.append({"entity_id": t["entity_id"], "recheck": recheck_ts})
            if not dry_run:
                _create_controller(db, t, dedup_key, recheck_ts, owner, now)
        if not dry_run:
            db.conn.commit()
    return {"ok": True, "created": created, "considered": considered,
            "dry": bool(dry_run), "skipped": skipped, "items": items}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Контроль срыва сроков")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--recheck-days", type=int, default=2)
    ap.add_argument("--owner")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    res = run(args.db, days=args.days, recheck_days=args.recheck_days,
              owner=args.owner, dry_run=not args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
