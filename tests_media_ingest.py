#!/usr/bin/env python3
"""Харднинг инджеста: дренаж легаси-очереди медиа + граница tg_mark_read.

Что реально исполняется:
  1. media.py: sweep_pending — все pending -> skipped одним UPDATE, без
     скачивания; done/failed/missing/skipped и сами строки не трогаются,
     возвращается счёт
  2. server.py: tg_mark_read — mark_read_id выше MAX(msg_id) темы отклоняется
     структурной ошибкой, last_read не меняется; id <= max принимается
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB  # noqa: E402
import media as media_mod  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def seed_assets(db: DB, rows: list[tuple[int, int, str, str]]) -> None:
    for chat_id, msg_id, kind, status in rows:
        db.conn.execute(
            "INSERT INTO media_assets(chat_id, msg_id, kind, status, text, engine, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (chat_id, msg_id, kind, status,
             "транскрипция" if status == "done" else None,
             "stub" if status == "done" else None,
             1700000000 if status != "pending" else None))
    db.conn.commit()


def test_sweep_pending(root: Path) -> None:
    print("=== sweep_pending: pending -> skipped, остальное не трогает ===")
    db_path = root / "sweep.sqlite"
    rows = [
        (-100500, 1, "voice", "pending"),
        (-100500, 2, "document", "pending"),
        (-100600, 1, "document", "pending"),
        (-100500, 3, "voice", "done"),
        (-100500, 4, "voice", "failed"),
        (-100500, 5, "voice", "missing"),
        (-100500, 6, "document", "skipped"),
    ]
    with DB(db_path) as db:
        seed_assets(db, rows)

    n = media_mod.sweep_pending(str(db_path))
    check(n == 3, "sweep_pending: возвращает число pending-строк", str(n))

    with DB(db_path) as db:
        got = {(r["chat_id"], r["msg_id"]): dict(r)
               for r in db.conn.execute("SELECT * FROM media_assets")}
        check(len(got) == 7, "sweep_pending: строки не удаляются", str(len(got)))
        check(all(got[k]["status"] == "skipped"
                  for k in ((-100500, 1), (-100500, 2), (-100600, 1))),
              "sweep_pending: pending стали skipped",
              str({k: got[k]["status"] for k in
                   ((-100500, 1), (-100500, 2), (-100600, 1))}))
        check(got[(-100500, 3)]["status"] == "done"
              and got[(-100500, 3)]["text"] == "транскрипция"
              and got[(-100500, 3)]["engine"] == "stub"
              and got[(-100500, 3)]["ts"] == 1700000000,
              "sweep_pending: done не тронут (status/text/engine/ts)",
              str(got[(-100500, 3)]))
        check(got[(-100500, 4)]["status"] == "failed"
              and got[(-100500, 5)]["status"] == "missing"
              and got[(-100500, 6)]["status"] == "skipped",
              "sweep_pending: failed/missing/skipped не тронуты",
              str({k: got[k]["status"] for k in
                   ((-100500, 4), (-100500, 5), (-100500, 6))}))

    n2 = media_mod.sweep_pending(str(db_path))
    check(n2 == 0, "sweep_pending: повторный прогон ничего не находит", str(n2))


def test_mark_read_bound(root: Path) -> None:
    print("=== tg_mark_read: граница по MAX(msg_id) темы ===")
    db_path = root / "mark_read.sqlite"
    chat, topic = -100700, 7
    chat2 = -100800
    with DB(db_path) as db:
        db.upsert_chat(chat_id=chat, title="Форум", username=None,
                       kind="supergroup", is_forum=True, members=1)
        db.upsert_topic(chat, topic, "Тема")
        db.watch_add(chat, topic, alias="тема")
        for i in range(1, 6):
            db.insert_message({"chat_id": chat, "msg_id": i, "topic_id": topic,
                               "ts": 1700000000 + i, "text": f"сообщение {i}"})
        db.upsert_chat(chat_id=chat2, title="Группа", username=None,
                       kind="group", is_forum=False, members=1)
        db.watch_add(chat2, 1)
        for i in range(1, 4):
            db.insert_message({"chat_id": chat2, "msg_id": i, "topic_id": 1,
                               "ts": 1700000000 + i, "text": f"общее {i}"})

    import server as server_mod
    server_mod.INDEX = str(db_path)
    server_mod._tls.__dict__.pop("db", None)
    try:
        out = json.loads(server_mod.tg_mark_read(chat, topic, 5))
        check(out.get("ok") is True and out.get("last_read") == 5,
              "mark_read: id = max принимается",
              json.dumps(out, ensure_ascii=False))

        out = json.loads(server_mod.tg_mark_read(chat, topic, 6))
        check(out.get("ok") is False and out.get("error") == "mark_read_id beyond index",
              "mark_read: id = max+1 отклоняется структурной ошибкой",
              json.dumps(out, ensure_ascii=False))

        out = json.loads(server_mod.tg_mark_read(chat, topic, 999))
        check(out.get("ok") is False and out.get("error") == "mark_read_id beyond index",
              "mark_read: id сильно больше max отклоняется",
              json.dumps(out, ensure_ascii=False))

        with DB(db_path) as db:
            lr = db.conn.execute(
                "SELECT last_read FROM watch WHERE chat_id=? AND topic_id=?",
                (chat, topic)).fetchone()[0]
        check(lr == 5, "mark_read: отказ не меняет last_read", str(lr))

        out = json.loads(server_mod.tg_mark_read(chat, topic, 3))
        check(out.get("ok") is True and out.get("last_read") == 3,
              "mark_read: id < max принимается",
              json.dumps(out, ensure_ascii=False))

        out = json.loads(server_mod.tg_mark_read(chat2, 0, 3))
        check(out.get("ok") is True,
              "mark_read: topic_id=0 (General) принимается до max",
              json.dumps(out, ensure_ascii=False))
        out = json.loads(server_mod.tg_mark_read(chat2, 0, 999))
        check(out.get("ok") is False and out.get("error") == "mark_read_id beyond index",
              "mark_read: topic_id=0 граница General тоже держит",
              json.dumps(out, ensure_ascii=False))
    finally:
        cached = server_mod._tls.__dict__.pop("db", None)
        if cached is not None:
            cached.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tg_media_ingest_"))
    try:
        test_sweep_pending(tmp)
        test_mark_read_bound(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
