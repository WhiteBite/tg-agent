#!/usr/bin/env python3
"""Ядро db.py: user_version-гейт инициализации, read-only open, фильтр
deleted=0 в читалках, FTS и удаления, колонки v2, каскад проекта, dirty-ключ,
таблица posts (v5)."""

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

from db import DB, SCHEMA_VERSION

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
             "_migrate_ops_overflow", "_migrate_extract_telemetry", "_migrate_v2_columns",
             "_migrate_status_done", "_migrate_posts")
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

    raw = sqlite3.connect(str(p))
    raw.execute("INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
                "closed_ts, created_ts, updated_ts) "
                "VALUES('services', -1006, 1, 'trial', 'Старый закрытый', 'closed', 5, 1, 5)")
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()
    with DB(p) as db:
        st = db.conn.execute(
            "SELECT status FROM entities WHERE title='Старый закрытый'").fetchone()["status"]
        st_uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check(st == "done" and st_uv == SCHEMA_VERSION, "v2->v3: status closed→done",
          f"{st}/{st_uv}")

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


def test_posts_migration(tmp: Path) -> None:
    print("=== миграция v5: таблица posts ===")
    p = tmp / "posts.sqlite"
    with DB(p) as db:
        db.insert_message(msg(-1010, 1, ts=100, text="до миграции"))
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(posts)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check({"chat_id", "msg_id", "topic_id", "kind", "product", "tpl_hash", "ts"} <= cols,
          "fresh: таблица posts со всеми колонками", str(sorted(cols)))
    check(uv == SCHEMA_VERSION, "fresh: user_version = SCHEMA_VERSION", str(uv))

    raw = sqlite3.connect(str(p))
    raw.execute("DROP TABLE IF EXISTS posts")
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(posts)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
        kept = db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=-1010").fetchone()[0]
    check({"chat_id", "msg_id", "topic_id", "kind", "product", "tpl_hash", "ts"} <= cols,
          "v4->v5: posts создана миграцией", str(sorted(cols)))
    check(uv == SCHEMA_VERSION, "v4->v11: user_version поднят", str(uv))
    check(kept == 1, "v4->v5: строки messages не потеряны", str(kept))


def test_posts_helpers(tmp: Path) -> None:
    print("=== posts: post_add / posts_list / post_get ===")
    p = tmp / "posts_rw.sqlite"
    with DB(p) as db:
        db.post_add(-1011, 10, topic_id=7, kind="release", product="Acme", tpl_hash="abc")
        db.post_add(-1011, 11)
        db.post_add(-1012, 12)
        db.conn.execute("UPDATE posts SET ts=200 WHERE chat_id=-1011 AND msg_id=10")
        db.conn.execute("UPDATE posts SET ts=300 WHERE chat_id=-1011 AND msg_id=11")
        db.conn.execute("UPDATE posts SET ts=250 WHERE chat_id=-1012 AND msg_id=12")
        db.conn.commit()
        got = db.post_get(-1011, 10)
        missing = db.post_get(-1011, 999)
        all_ids = [r["msg_id"] for r in db.posts_list()]
        chat_ids = [r["msg_id"] for r in db.posts_list(chat_id=-1011)]
        db.post_add(-1011, 10, kind="promo", product="Acme2", tpl_hash="zzz")
        replaced = db.post_get(-1011, 10)
    check(got is not None and got["topic_id"] == 7 and got["kind"] == "release"
          and got["product"] == "Acme" and got["tpl_hash"] == "abc" and got["ts"] == 200,
          "post_get: все поля на месте", str(dict(got) if got else None))
    check(missing is None, "post_get: нет строки -> None")
    check(all_ids == [11, 12, 10], "posts_list: без фильтра, ORDER BY ts DESC", str(all_ids))
    check(chat_ids == [11, 10], "posts_list: фильтр по chat_id", str(chat_ids))
    check(replaced is not None and replaced["kind"] == "promo"
          and replaced["product"] == "Acme2" and replaced["tpl_hash"] == "zzz",
          "post_add: повтор по (chat_id, msg_id) перезаписывает",
          str(dict(replaced) if replaced else None))


def test_actor_migration(tmp: Path) -> None:
    print("=== миграция v11: entity_events.actor ===")
    p = tmp / "actor.sqlite"
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(entity_events)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check("actor" in cols, "fresh: entity_events.actor есть", str(sorted(cols)))
    check(uv == SCHEMA_VERSION, "fresh: user_version = SCHEMA_VERSION", str(uv))

    raw = sqlite3.connect(str(p))
    raw.execute("INSERT INTO entity_events(entity_id, ts, op, field, to_val) "
                "VALUES(1, 100, 'update', 'status', 'done')")
    raw.execute("ALTER TABLE entity_events DROP COLUMN actor")
    raw.execute("PRAGMA user_version = 10")
    raw.commit()
    raw.close()
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(entity_events)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
        kept = db.conn.execute(
            "SELECT COUNT(*) FROM entity_events WHERE entity_id=1").fetchone()[0]
    check("actor" in cols, "v10->v11: колонка actor добавлена", str(sorted(cols)))
    check(uv == SCHEMA_VERSION, "v10->v11: user_version поднят", str(uv))
    check(kept == 1, "v10->v11: строки entity_events сохранены", str(kept))

    raw = sqlite3.connect(str(p))
    raw.execute("PRAGMA user_version = 10")
    raw.commit()
    raw.close()
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(entity_events)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
        kept = db.conn.execute(
            "SELECT COUNT(*) FROM entity_events WHERE entity_id=1").fetchone()[0]
    check("actor" in cols and uv == SCHEMA_VERSION and kept == 1,
          "v11: повторный прогон миграции идемпотентен", f"{uv}/{len(cols)}/{kept}")


def test_entity_links_migration(tmp: Path) -> None:
    print("=== миграция v12: entity_links ===")
    p = tmp / "elinks.sqlite"
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(entity_links)")}
        idx = {r[1] for r in db.conn.execute("PRAGMA index_list(entity_links)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check({"id", "from_entity_id", "to_entity_id", "link_kind", "run_id",
           "chat_id", "msg_id", "quote", "created_ts", "updated_ts"} <= cols,
          "fresh: entity_links со всеми колонками", str(sorted(cols)))
    check({"ux_elink", "ix_elink_to"} <= idx,
          "fresh: индексы entity_links созданы", str(sorted(idx)))
    check(uv == SCHEMA_VERSION, "fresh: user_version = SCHEMA_VERSION", str(uv))

    raw = sqlite3.connect(str(p))
    raw.execute("INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) "
                "VALUES(-1012, 1, 1, 100, 'до миграции')")
    raw.execute("DROP TABLE IF EXISTS entity_links")
    raw.execute("PRAGMA user_version = 11")
    raw.commit()
    raw.close()
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(entity_links)")}
        idx = {r[1] for r in db.conn.execute("PRAGMA index_list(entity_links)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
        kept = db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=-1012").fetchone()[0]
    check({"id", "from_entity_id", "to_entity_id", "link_kind", "run_id",
           "chat_id", "msg_id", "quote", "created_ts", "updated_ts"} <= cols,
          "v11->v12: entity_links создана миграцией", str(sorted(cols)))
    check({"ux_elink", "ix_elink_to"} <= idx,
          "v11->v12: индексы entity_links созданы", str(sorted(idx)))
    check(uv == SCHEMA_VERSION, "v11->v12: user_version поднят", str(uv))
    check(kept == 1, "v11->v12: строки messages сохранены", str(kept))


def test_via_migration(tmp: Path) -> None:
    print("=== миграция v13: messages.via ===")
    p = tmp / "via.sqlite"
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(messages)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check("via" in cols, "fresh: messages.via есть", str(sorted(cols)))
    check(uv == SCHEMA_VERSION, "fresh: user_version = SCHEMA_VERSION", str(uv))

    raw = sqlite3.connect(str(p))
    raw.execute("INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) "
                "VALUES(-1013, 1, 1, 100, 'до миграции')")
    raw.execute("ALTER TABLE messages DROP COLUMN via")
    raw.execute("PRAGMA user_version = 12")
    raw.commit()
    raw.close()
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(messages)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
        kept = db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=-1013").fetchone()[0]
    check("via" in cols, "v12->v13: колонка via добавлена", str(sorted(cols)))
    check(uv == SCHEMA_VERSION, "v12->v13: user_version поднят", str(uv))
    check(kept == 1, "v12->v13: строки messages сохранены", str(kept))


def test_memory_migrations(tmp: Path) -> None:
    print("=== миграции v14/v15/v16: entities_fts, entity_vec, memory_candidates ===")
    p = tmp / "memory.sqlite"
    with DB(p) as db:
        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        triggers = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        mc_idx = {r[1] for r in db.conn.execute("PRAGMA index_list(memory_candidates)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check("entities_fts" in tables, "fresh: entities_fts создана", str(sorted(tables)))
    check({"trg_ent_ai", "trg_ent_ad", "trg_ent_au"} <= triggers,
          "fresh: триггеры entities_fts созданы", str(sorted(triggers)))
    check("entity_vec" in tables, "fresh: entity_vec создана")
    check("memory_candidates" in tables, "fresh: memory_candidates создана")
    check({"ux_memcand", "ix_memcand_status"} <= mc_idx,
          "fresh: индексы memory_candidates созданы", str(sorted(mc_idx)))
    check(uv == SCHEMA_VERSION, "fresh: user_version = SCHEMA_VERSION", str(uv))

    p2 = tmp / "memory_backfill.sqlite"
    with DB(p2) as db:
        db.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, body, status, "
            "created_ts) VALUES('services', -1020, 1, 'service', 'Старый сервис', "
            "'тело', 'open', 1)")
        db.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, created_ts) "
            "VALUES('services', -1020, 1, 'service', 'Без тела', 'open', 1)")
        db.conn.commit()
    raw = sqlite3.connect(str(p2))
    raw.execute("DROP TABLE entities_fts")
    raw.execute("PRAGMA user_version = 13")
    raw.commit()
    raw.close()
    with DB(p2) as db:
        cnt = db.conn.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
        hit = db.conn.execute(
            "SELECT COUNT(*) FROM entities_fts WHERE entities_fts MATCH 'сервис'").fetchone()[0]
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check(cnt == 2, "v13->v14: backfill проиндексировал существующие сущности", str(cnt))
    check(hit == 1, "v13->v14: backfill ищется по title", str(hit))
    check(uv == SCHEMA_VERSION, "v13->v14: user_version поднят", str(uv))

    p3 = tmp / "memory_merge.sqlite"
    with DB(p3) as db:
        db.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, body, status, "
            "created_ts, updated_ts) VALUES('services', -1021, 1, 'service', 'Дубль', "
            "'тело кипера', 'open', 1, 1)")
        keeper = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, body, status, "
            "created_ts, updated_ts) VALUES('services', -1022, 1, 'service', 'Дубль', "
            "'тело лузера', 'open', 2, 2)")
        loser = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.conn.commit()
        before = db.conn.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
        db.merge_entities(keeper, [loser], "test", 100)
        db.conn.commit()
        keeper_row = db.conn.execute(
            "SELECT COUNT(*) FROM entities_fts WHERE rowid=?", (keeper,)).fetchone()[0]
        loser_row = db.conn.execute(
            "SELECT COUNT(*) FROM entities_fts WHERE rowid=?", (loser,)).fetchone()[0]
        total = db.conn.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
    check(before == 2, "merge: обе сущности проиндексированы до слияния", str(before))
    check(keeper_row == 1, "merge: строка кипера осталась в entities_fts", str(keeper_row))
    check(loser_row == 0, "merge: строка лузера вычищена из entities_fts", str(loser_row))
    check(total == 1, "merge: в entities_fts ровно одна строка", str(total))

    with DB(p3) as db:
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
        total2 = db.conn.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
    check(uv == SCHEMA_VERSION and total2 == 1,
          "reopen: идемпотентно, entities_fts не дублируется", f"{uv}/{total2}")


def test_merge_reparent(tmp: Path) -> None:
    print("=== merge_entities: репарент links/vec/candidates ===")
    p = tmp / "merge_reparent.sqlite"
    with DB(p) as db:
        def add_entity(title: str, ts: int) -> int:
            db.conn.execute(
                "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
                "created_ts, updated_ts) VALUES('services', -1030, 1, 'service', ?, "
                "'open', ?, ?)", (title, ts, ts))
            return db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        keeper = add_entity("Кипер", 1)
        loser = add_entity("Лузер", 2)
        other = add_entity("Другой", 3)
        db.conn.commit()

        db.conn.execute("INSERT INTO entity_links(from_entity_id, to_entity_id, link_kind) "
                        "VALUES(?,?, 'blocks')", (loser, other))
        db.conn.execute("INSERT INTO entity_links(from_entity_id, to_entity_id, link_kind) "
                        "VALUES(?,?, 'blocks')", (other, loser))
        db.conn.execute("INSERT INTO entity_links(from_entity_id, to_entity_id, link_kind) "
                        "VALUES(?,?, 'dup')", (keeper, other))
        db.conn.execute("INSERT INTO entity_links(from_entity_id, to_entity_id, link_kind) "
                        "VALUES(?,?, 'dup')", (loser, other))
        db.conn.execute("INSERT INTO entity_links(from_entity_id, to_entity_id, link_kind) "
                        "VALUES(?,?, 'dup2')", (other, keeper))
        db.conn.execute("INSERT INTO entity_links(from_entity_id, to_entity_id, link_kind) "
                        "VALUES(?,?, 'dup2')", (other, loser))

        db.conn.execute("INSERT INTO entity_vec(entity_id, model, dim, vec, updated_ts) "
                        "VALUES(?, 'm', 2, X'0000', 5)", (loser,))

        db.conn.execute("INSERT INTO memory_candidates(new_entity_id, candidate_entity_id, "
                        "status) VALUES(?,?, 'pending')", (loser, other))
        db.conn.execute("INSERT INTO memory_candidates(new_entity_id, candidate_entity_id, "
                        "status) VALUES(?,?, 'pending')", (other, loser))
        db.conn.execute("INSERT INTO memory_candidates(new_entity_id, candidate_entity_id, "
                        "status) VALUES(?,?, 'pending')", (keeper, other))
        db.conn.execute("INSERT INTO memory_candidates(new_entity_id, candidate_entity_id, "
                        "status) VALUES(?,?, 'pending')", (loser, keeper))
        db.conn.execute("INSERT INTO memory_candidates(new_entity_id, candidate_entity_id, "
                        "status) VALUES(?,?, 'pending')", (keeper, loser))
        db.conn.commit()

        db.merge_entities(keeper, [loser], "test", 100)
        db.conn.commit()

        links = sorted(tuple(r) for r in db.conn.execute(
            "SELECT from_entity_id, to_entity_id, link_kind FROM entity_links"))
        vec_ids = [r["entity_id"] for r in db.conn.execute(
            "SELECT entity_id FROM entity_vec ORDER BY entity_id")]
        cands = sorted(tuple(r) for r in db.conn.execute(
            "SELECT new_entity_id, candidate_entity_id FROM memory_candidates"))
        loser_links = db.conn.execute(
            "SELECT COUNT(*) FROM entity_links WHERE from_entity_id=? OR to_entity_id=?",
            (loser, loser)).fetchone()[0]
        loser_vec = db.conn.execute(
            "SELECT COUNT(*) FROM entity_vec WHERE entity_id=?", (loser,)).fetchone()[0]
        loser_cands = db.conn.execute(
            "SELECT COUNT(*) FROM memory_candidates "
            "WHERE new_entity_id=? OR candidate_entity_id=?", (loser, loser)).fetchone()[0]
        self_pairs = db.conn.execute(
            "SELECT COUNT(*) FROM memory_candidates "
            "WHERE new_entity_id=candidate_entity_id").fetchone()[0]

    check(loser_links == 0, "merge: нет entity_links на лузера", str(loser_links))
    check(loser_vec == 0, "merge: нет entity_vec на лузера", str(loser_vec))
    check(loser_cands == 0, "merge: нет memory_candidates на лузера", str(loser_cands))
    check(self_pairs == 0, "merge: self-pair кандидаты удалены", str(self_pairs))
    check(links == [(keeper, other, "blocks"), (keeper, other, "dup"),
                    (other, keeper, "blocks"), (other, keeper, "dup2")],
          "merge: links репарентнуты, коллизии схлопнуты", str(links))
    check(vec_ids == [keeper], "merge: vec лузера перешёл киперу", str(vec_ids))
    check(cands == [(keeper, other), (other, keeper)],
          "merge: кандидаты репарентнуты, self-pair убраны", str(cands))

    p2 = tmp / "merge_vec_keep.sqlite"
    with DB(p2) as db:
        def add2(title: str, ts: int) -> int:
            db.conn.execute(
                "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
                "created_ts, updated_ts) VALUES('services', -1031, 1, 'service', ?, "
                "'open', ?, ?)", (title, ts, ts))
            return db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        k2 = add2("Кипер2", 1)
        l2 = add2("Лузер2", 2)
        db.conn.execute("INSERT INTO entity_vec(entity_id, model, dim, vec, updated_ts) "
                        "VALUES(?, 'keep', 2, X'1111', 9)", (k2,))
        db.conn.execute("INSERT INTO entity_vec(entity_id, model, dim, vec, updated_ts) "
                        "VALUES(?, 'lose', 2, X'2222', 8)", (l2,))
        db.conn.commit()
        db.merge_entities(k2, [l2], "test", 100)
        db.conn.commit()
        vec2 = [tuple(r) for r in db.conn.execute(
            "SELECT entity_id, model FROM entity_vec ORDER BY entity_id")]
    check(vec2 == [(k2, "keep")], "merge: vec кипера сохранён, vec лузера отброшен", str(vec2))


def test_fts_probe_independent(tmp: Path) -> None:
    print("=== FTS: messages_fts и entities_fts пробятся независимо ===")
    p = tmp / "fts_probe.sqlite"
    with DB(p) as db:
        db.insert_message(msg(-1040, 1, text="x"))
        both = (db.fts, db.fts_entities)
    check(both == (True, True), "fresh: обе FTS-таблицы True", str(both))

    raw = sqlite3.connect(str(p))
    raw.execute("DROP TABLE entities_fts")
    raw.commit()
    raw.close()
    ro = DB(p, readonly=True)
    try:
        ro_flags = (ro.fts, ro.fts_entities)
    finally:
        ro.close()
    check(ro_flags == (True, False),
          "readonly: только messages_fts -> fts=True, fts_entities=False", str(ro_flags))

    rw = DB(p)
    try:
        rw_flags = (rw.fts, rw.fts_entities)
    finally:
        rw.close()
    check(rw_flags == (True, False),
          "version>=SCHEMA_VERSION: только messages_fts -> fts=True, fts_entities=False",
          str(rw_flags))

    p2 = tmp / "fts_probe_ent.sqlite"
    with DB(p2) as db:
        db.insert_message(msg(-1041, 1, text="y"))
    raw = sqlite3.connect(str(p2))
    raw.execute("DROP TABLE messages_fts")
    raw.commit()
    raw.close()
    ro2 = DB(p2, readonly=True)
    try:
        ro2_flags = (ro2.fts, ro2.fts_entities)
    finally:
        ro2.close()
    check(ro2_flags == (False, True),
          "readonly: только entities_fts -> fts=False, fts_entities=True", str(ro2_flags))

    p3 = tmp / "fts_probe_mig.sqlite"
    with DB(p3) as db:
        db.insert_message(msg(-1042, 1, text="z"))
    raw = sqlite3.connect(str(p3))
    raw.execute("DROP TABLE entities_fts")
    raw.execute("PRAGMA user_version = 13")
    raw.commit()
    raw.close()
    with DB(p3) as db:
        mig_flags = (db.fts, db.fts_entities)
    check(mig_flags == (True, True),
          "миграция v13->v14: entities_fts создана, fts_entities=True", str(mig_flags))

    p4 = tmp / "fts_probe_fail.sqlite"
    with DB(p4) as db:
        db.insert_message(msg(-1043, 1, text="w"))
    raw = sqlite3.connect(str(p4))
    raw.execute("DROP TABLE entities_fts")
    raw.execute("CREATE TABLE entities_fts (bogus INTEGER)")
    raw.execute("PRAGMA user_version = 13")
    raw.commit()
    raw.close()
    with DB(p4) as db:
        fail_flags = (db.fts, db.fts_entities)
    check(fail_flags == (True, False),
          "миграция v14 сбой: fts=True, fts_entities=False", str(fail_flags))


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
        test_posts_migration(tmp)
        test_posts_helpers(tmp)
        test_actor_migration(tmp)
        test_entity_links_migration(tmp)
        test_via_migration(tmp)
        test_memory_migrations(tmp)
        test_merge_reparent(tmp)
        test_fts_probe_independent(tmp)
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
