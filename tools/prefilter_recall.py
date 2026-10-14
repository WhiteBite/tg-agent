#!/usr/bin/env python3
"""Замер префильтра пака: recall и volume на уже извлечённых сущностях.

    python tools/prefilter_recall.py [--pack services] [--days 35] [--db путь]

Корпус — сообщения за N дней; позитивы — сообщения, на которые ссылаются
evidence сущностей пака (entity_events). recall — доля позитивов, которую
префильтр оставляет; volume — доля корпуса, которая доходит до LLM.
Гейт: recall >= 0.85, volume <= 0.30. Транскрипты медиа в замере не
участвуют (в корпусе их единицы) — recall чуть занижен, это запас.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402
from packs import PACKS  # noqa: E402


def _arg(args: list[str], name: str, default: str) -> str:
    return args[args.index(name) + 1] if name in args else default


def main() -> int:
    args = sys.argv[1:]
    pack_id = _arg(args, "--pack", "services")
    days = int(_arg(args, "--days", "35"))
    db_path = _arg(args, "--db", os.environ.get("TG_DB", str(HERE / "tg_index.sqlite")))
    pack = PACKS[pack_id]
    pf = pack.get("prefilter") or {}
    rx = re.compile(pf.get("text_regex") or "", re.IGNORECASE)
    since = int(time.time()) - days * 86400
    with DB(db_path) as db:
        rows = db.conn.execute(
            "SELECT chat_id, msg_id, text FROM messages WHERE ts >= ?", (since,)).fetchall()
        ref_ids = set()
        if pf.get("use_refs"):
            ref_ids = {(r["chat_id"], r["msg_id"]) for r in db.conn.execute(
                "SELECT DISTINCT chat_id, msg_id FROM refs")}
        positives = {(r["chat_id"], r["msg_id"]) for r in db.conn.execute(
            "SELECT DISTINCT ev.chat_id, ev.msg_id FROM entity_events ev "
            "JOIN entities e ON e.id = ev.entity_id WHERE e.pack = ?", (pack_id,))}
    corpus = {(r["chat_id"], r["msg_id"]) for r in rows}
    kept = {(r["chat_id"], r["msg_id"]) for r in rows
            if rx.search(r["text"] or "") or (r["chat_id"], r["msg_id"]) in ref_ids}
    pos = positives & corpus
    hit = pos & kept
    recall = len(hit) / len(pos) if pos else 1.0
    volume = len(kept) / len(corpus) if corpus else 0.0
    missed = sorted(pos - kept)[:20]
    print(f"корпус: {len(corpus)} сообщений за {days}д")
    print(f"после префильтра: {len(kept)} ({volume:.1%} объёма)")
    print(f"позитивы (evidence {pack_id}): {len(pos)}, поймано {len(hit)} ({recall:.1%})")
    if missed:
        print(f"пропущено (до 20): {missed}")
    ok = recall >= 0.85 and volume <= 0.30
    print("гейт recall>=85% volume<=30%:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
