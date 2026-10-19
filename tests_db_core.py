#!/usr/bin/env python3
"""Ядро db.py: user_version-гейт инициализации, read-only open, фильтр
deleted=0 в читалках, FTS и удаления, колонки v2, каскад проекта, dirty-ключ."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB, SCHEMA_VERSION  # noqa: E402

FAILURES: list[str] = []

OLD_MESSAGES = """
CREATE TABLE messages (
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    topic_id    INTEGER,
    ts          INTEGER,
    date_iso    TEXT,
    sender_id   INTEGER,
    sender_name TEXT,
    is_out      INTEGER DEFAULT 0,
    is_service  INTEGER DEFAULT 0,
    text        TEXT,
    media       TEXT,
    reply_to    INTEGER,
    views       INTEGER,
    forwards    INTEGER,
    reactions   TEXT,
    raw_json    TEXT,
    PRIMARY KEY (chat_id, msg_id)
);
"""


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def msg(chat: int, mid: int, topic: int = 1, ts: int = 100, text: str = "текст") -> dict[str, Any]:
    return {"chat_id": chat, "msg_id": mid, "topic_id": topic, "ts": ts, "text": text}


def schema_version(p: Path) -> int:
    raw = sqlite3.connect(str(p))
    try:
        return raw.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        raw.close()


def test_user_version_gate(tmp: Path) -> None:
    print("=== init: user_version-гейт ===")
    p = tmp / "gate.sqlite"
    with DB(p) as db:
        db.insert_message(msg(-1001, 1, text="привет"))
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check(uv == SCHEMA_VERSION, "init: user_version = SCHEMA_VERSION после первого открытия", str(uv))

    sv1 = schema_version(p)
    spied: list[str] = []
    names = ("_migrate_message_edits", "_migrate_marked_ids", "_migrate_global_dedup",
             "_migrate_watch_project", "_migrate_entity_status", "_migrate_topic_ids",
             "_migrate_ops_overflow", "_migrate_extract_telemetry", "_migrate_v2_columns")
    saved = {n: getattr(DB, n) for n in names}
    try:
        for n in names:
            setattr(DB, n, lambda self, _n=n: spied.append(_n))
        with DB(p) as db:
            rows = db.history(-1001)
    finally:
        for n, fn in saved.items():
            setattr(DB, n, fn)
    sv2 = schema_version(p)
    check(not spied, "init: второе открытие не гоняет миграции", str(spied))
    check(len(rows) == 1, "init: второе открытие читает данные", str(len(rows)))
    check(sv1 == sv2, "init: второе открытие не делает DDL (schema_version не изменился)",
          f"{sv1}/{sv2}")


def test_readonly(tmp: Path) -> None:
    print("=== init: read-only open ===")
    p = tmp / "ro.sqlite"
    with DB(p) as db:
        db.insert_message(msg(-1002, 1, text="данные"))
        db.watch_add(-1002, 1, alias="ro")
    ro = DB(p, readonly=True)
    try:
        qo = ro.conn.execute("PRAGMA query_only").fetchone()[0]
        rows = ro.history(-1002)
        wn = ro.watch_new(-1002, 1)
        uv = ro.conn.execute("PRAGMA user_version").fetchone()[0]
        try:
            ro.conn.execute("INSERT INTO meta(key, value) VALUES('x', 'y')")
            wrote = False
        except sqlite3.OperationalError:
            wrote = True
        fts_flag = ro.fts
    finally:
        ro.close()
    check(qo == 1, "readonly: PRAGMA query_only=ON", str(qo))
    check(fts_flag is True, "readonly: fts пробится селектом")
    check(len(rows) == 1 and wn["returned"] == 1, "readonly: история и watch_new читаются")
    check(uv == SCHEMA_VERSION, "readonly: user_version не трогается", str(uv))
    check(wrote, "readonly: запись отклоняется")

    p2 = tmp / "empty.sqlite"
    sqlite3.connect(str(p2)).close()
    ro2 = DB(p2, readonly=True)
    try:
        check(ro2.fts is False, "readonly: без messages_fts fts=False")
    finally:
        ro2.close()

    p3 = tmp / "old_ro.sqlite"
    conn = sqlite3.connect(str(p3))
    conn.executescript(OLD_MESSAGES)
    conn.execute("INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) "
                 "VALUES(-1003, 1, 1, 100, 'старое')")
    conn.commit()
    conn.close()
    ro3 = DB(p3, readonly=True)
    try:
        cols = {r[1] for r in ro3.conn.execute("PRAGMA table_info(messages)")}
        uv3 = ro3.conn.execute("PRAGMA user_version").fetchone()[0]
        row = ro3.conn.execute("SELECT text FROM messages WHERE msg_id=1").fetchone()
    finally:
        ro3.close()
    check("edited_ts" not in cols and uv3 == 0,
          "readonly: миграции не запускаются (v0 остаётся v0)", f"{uv3}/{len(cols)}")
    check(row is not None and row["text"] == "старое", "readonly: данные читаются")


def test_deleted_filter(tmp: Path) -> None:
    print("=== читалки: deleted=0 ===")
    p = tmp / "deleted.sqlite"
    with DB(p) as db:
        db.insert_message(msg(-1004, 1, ts=100, text="живой один"))
        db.insert_message(msg(-1004, 2, ts=101, text="удалённый секрет"))
        db.insert_message(msg(-1004, 3, ts=102, text="живой два"))
        db.watch_add(-1004, 1, alias="t")
        db.mark_deleted(-1004, 2)
        hist = db.history(-1004)
        wn = db.watch_new(-1004, 1)
        fts_hit = db.search_fts("секрет", chat_id=-1004)
        fts_row = db.conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE text='удалённый секрет'").fetchone()[0]
        db.fts = False
        like_hit = db.search_fts("секрет", chat_id=-1004)
        db.fts = True
        ctx = db.search("живой два", chat_id=-1004, context=2)
    ids = [m["msg_id"] for m in hist]
    wn_ids = [m["msg_id"] for m in wn["messages"]]
    before_ids = [m["msg_id"] for m in ctx[0]["context_before"]] if ctx else []
    check(ids == [1, 3], "history: deleted исключён", str(ids))
    check(wn_ids == [1, 3] and wn["pending_total"] == 2,
          "watch_new: deleted исключён (включая pending)", str(wn_ids))
    check(fts_hit["count"] == 0, "search_fts: удалённый текст не ищется", str(fts_hit["count"]))
    check(fts_row == 0, "mark_deleted: строки нет в messages_fts", str(fts_row))
    check(like_hit["engine"] == "like" and like_hit["count"] == 0,
          "LIKE-фолбэк: deleted исключён", str(like_hit))
    check(before_ids == [1], "_around: deleted исключён из контекста", str(before_ids))


def test_fts_edit_delete(tmp: Path) -> None:
    print("=== FTS: правки ищутся, удаления выпадают ===")
    p = tmp / "fts.sqlite"
    with DB(p) as db:
        db.insert_message(msg(-1005, 1, ts=100, text="старая версия"))
        db.update_message({"chat_id": -1005, "msg_id": 1, "topic_id": 1,
                           "text": "новая версия", "edited_ts": 150})
        new = db.search_fts("новая версия", chat_id=-1005)
        old = db.search_fts("старая версия", chat_id=-1005)
        fts_old_cnt = db.conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE text='старая версия'").fetchone()[0]
        db.mark_deleted(-1005, 1)
        fts_cnt = db.conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE text='новая версия'").fetchone()[0]
        after = db.search_fts("новая версия", chat_id=-1005)
    check(new["count"] == 1 and old["count"] == 0,
          "FTS: после правки ищется новый текст, старый нет",
          f"{new['count']}/{old['count']}")
    check(fts_old_cnt == 0, "FTS: триггер UPDATE OF text не оставляет старый текст",
          str(fts_old_cnt))
    check(fts_cnt == 0 and after["count"] == 0, "mark_deleted: вычищает строку из messages_fts",
          f"{fts_cnt}/{after['count']}")


def test_new_columns(tmp: Path) -> None:
    print("=== миграция v2: rejects_json / external_id ===")
    p = tmp / "cols.sqlite"
    with DB(p) as db:
        er = {r[1] for r in db.conn.execute("PRAGMA table_info(extraction_runs)")}
        ig = {r[1] for r in db.conn.execute("PRAGMA table_info(integrations)")}
    check("rejects_json" in er, "fresh: extraction_runs.rejects_json")
    check("external_id" in ig, "fresh: integrations.external_id")

    raw = sqlite3.connect(str(p))
    raw.execute("ALTER TABLE extraction_runs DROP COLUMN rejects_json")
    raw.execute("ALTER TABLE integrations DROP COLUMN external_id")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    with DB(p) as db:
        er = {r[1] for r in db.conn.execute("PRAGMA table_info(extraction_runs)")}
        ig = {r[1] for r in db.conn.execute("PRAGMA table_info(integrations)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check("rejects_json" in er and "external_id" in ig, "v1->v2: колонки добавлены")
    check(uv == SCHEMA_VERSION, "v1->v2: user_version поднят", str(uv))

    p2 = tmp / "old_v0.sqlite"
    conn = sqlite3.connect(str(p2))
    conn.executescript(OLD_MESSAGES)
    conn.execute("INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) "
                 "VALUES(-1006, 7, 1, 100, 'легаси')")
    conn.commit()
    conn.close()
    with DB(p2) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(messages)")}
        row = db.conn.execute("SELECT text, deleted FROM messages WHERE msg_id=7").fetchone()
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
        er = {r[1] for r in db.conn.execute("PRAGMA table_info(extraction_runs)")}
    check({"edited_ts", "deleted"} <= cols, "v0: колонки правок добавлены")
    check(row is not None and row["text"] == "легаси" and row["deleted"] == 0,
          "v0: данные целы")
    check(uv == SCHEMA_VERSION and "rejects_json" in er, "v0: доведён до актуальной версии",
          str(uv))


def test_project_cascade(tmp: Path) -> None:
    print("=== watch_set_project: каскад в entities ===")
    p = tmp / "proj.sqlite"
    with DB(p) as db:
        db.watch_add(-1007, 5, alias="a", project="старый")
        for title, closed in (("открытая", None), ("закрытая", 2)):
            db.conn.execute(
                "INSERT INTO entities(pack, chat_id, topic_id, kind, title, project, "
                "created_ts, closed_ts) VALUES('services', -1007, 5, 'service', ?, "
                "'старый', 1, ?)", (title, closed))
        db.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, project, created_ts) "
            "VALUES('services', -1007, 6, 'service', 'другая тема', 'старый', 1)")
        db.conn.commit()
        ok = db.watch_set_project(-1007, 5, "новый")
        w = db.conn.execute(
            "SELECT project FROM watch WHERE chat_id=-1007 AND topic_id=5").fetchone()
        ents = {r["title"]: r["project"] for r in db.conn.execute(
            "SELECT title, project FROM entities")}
        missing = db.watch_set_project(-1007, 999, "x")
        db.conn.execute("UPDATE entities SET project='ручной' WHERE title='открытая'")
        db.conn.commit()
        db.watch_set_project(-1007, 5, "новый")
        ent = db.conn.execute(
            "SELECT project FROM entities WHERE title='открытая'").fetchone()
    check(ok and w["project"] == "новый", "каскад: watch обновлён")
    check(ents["открытая"] == "новый", "каскад: открытая сущность темы переведена")
    check(ents["закрытая"] == "старый", "каскад: closed_ts IS NOT NULL не трогается")
    check(ents["другая тема"] == "старый", "каскад: другие темы не затронуты")
    check(missing is False, "каскад: нет watch-строки -> False")
    check(ent["project"] == "ручной", "каскад: при том же значении не перезаписывает")


def test_dirty_recorder(tmp: Path) -> None:
    print("=== dirty: накопление, дедуп, cap 200 ===")
    p = tmp / "dirty.sqlite"
    chat = -1008
    with DB(p) as db:
        db.insert_message(msg(chat, 1, topic=5, text="a"))
        db.insert_message(msg(chat, 2, topic=5, text="b"))
        db.insert_message(msg(chat, 3, topic=5, text="c"))
        db.update_message({"chat_id": chat, "msg_id": 1, "topic_id": 5,
                           "text": "a2", "edited_ts": 10})
        d1 = db.get_meta("dirty:-1008:5")
        db.update_message({"chat_id": chat, "msg_id": 1, "topic_id": 5,
                           "text": "a3", "edited_ts": 20})
        d2 = db.get_meta("dirty:-1008:5")
        db.update_message({"chat_id": chat, "msg_id": 2, "topic_id": 5,
                           "text": "b2", "edited_ts": 30})
        d3 = db.get_meta("dirty:-1008:5")
        db.mark_deleted(chat, 3)
        d4 = db.get_meta("dirty:-1008:5")
        db.update_message({"chat_id": chat, "msg_id": 1, "topic_id": 5,
                           "text": "a4", "edited_ts": 40})
        d5 = db.get_meta("dirty:-1008:5")
    check(d1 == json.dumps([1]), "dirty: правка записана", str(d1))
    check(d2 == json.dumps([1]), "dirty: повторная правка не дублирует", str(d2))
    check(json.loads(d3) == [1, 2], "dirty: накопление", str(d3))
    check(json.loads(d4) == [1, 2, 3], "dirty: mark_deleted добавляет id", str(d4))
    check(json.loads(d5) == [2, 3, 1], "dirty: переперезапись ставит id в конец", str(d5))

    p2 = tmp / "cap.sqlite"
    chat2 = -1009
    with DB(p2) as db:
        for i in range(205):
            db.insert_message(msg(chat2, i, topic=7, text=f"t{i}"))
        for i in range(205):
            db.update_message({"chat_id": chat2, "msg_id": i, "topic_id": 7,
                               "text": f"t{i}v2", "edited_ts": 1000 + i})
        cap = json.loads(db.get_meta("dirty:-1009:7"))
    check(len(cap) == 200 and cap[0] == 5 and cap[-1] == 204,
          "dirty: cap 200, старейшие выпадают", f"{len(cap)}/{cap[0]}..{cap[-1]}")

    with DB(p) as db:
        gone = db.mark_deleted(chat, 999)
        key = db.get_meta("dirty:-1008:999")
        db.insert_message(msg(chat, 10, topic=9, text="x"))
        db.update_message({"chat_id": chat, "msg_id": 10, "text": "x2", "edited_ts": 5})
        keys = [r["key"] for r in db.conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'dirty:%' ORDER BY key")]
    check(gone is False and key is None, "dirty: неизвестная строка не пишется")
    check(keys == ["dirty:-1008:5"], "dirty: без topic_id в msg ключ не пишется", str(keys))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tg_db_core_"))
    try:
        test_user_version_gate(tmp)
        test_readonly(tmp)
        test_deleted_filter(tmp)
        test_fts_edit_delete(tmp)
        test_new_columns(tmp)
        test_project_cascade(tmp)
        test_dirty_recorder(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"провалов: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("всё зелёное")
    return 0


if __name__ == "__main__":
    sys.exit(main())
