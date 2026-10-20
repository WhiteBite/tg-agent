#!/usr/bin/env python3
"""Управление чатами: миграция v4 (created_by_me / my_role у chats, таблица
invite_links), совместимость upsert_chat с новыми kwargs, точечные апдейты
chat_set_manage_fields и инвайт-ссылки."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB, SCHEMA_VERSION  # noqa: E402

FAILURES: list[str] = []

OLD_CHATS = """
CREATE TABLE chats (
    id          INTEGER PRIMARY KEY,
    title       TEXT,
    username    TEXT,
    kind        TEXT,
    is_forum    INTEGER DEFAULT 0,
    members     INTEGER,
    synced_at   INTEGER
);
"""


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def schema_version(p: Path) -> int:
    raw = sqlite3.connect(str(p))
    try:
        return raw.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        raw.close()


def test_fresh_schema(tmp: Path) -> None:
    print("=== v4: свежая база ===")
    p = tmp / "fresh.sqlite"
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(chats)")}
        try:
            db.conn.execute("SELECT created_by_me, my_role FROM chats").fetchone()
            sel_err = None
        except sqlite3.OperationalError as e:
            sel_err = str(e)
        try:
            db.conn.execute("SELECT * FROM invite_links").fetchall()
            tbl_err = None
        except sqlite3.OperationalError as e:
            tbl_err = str(e)
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check(SCHEMA_VERSION == 4, "fresh: SCHEMA_VERSION = 4", str(SCHEMA_VERSION))
    check({"created_by_me", "my_role"} <= cols, "fresh: колонки chats есть",
          str(sorted(cols)))
    check(sel_err is None, "fresh: SELECT created_by_me, my_role работает", str(sel_err))
    check(tbl_err is None, "fresh: таблица invite_links есть", str(tbl_err))
    check(uv == 4, "fresh: user_version = 4", str(uv))


def test_v3_migration(tmp: Path) -> None:
    print("=== v4: миграция v3 -> v4 ===")
    p = tmp / "v3.sqlite"
    conn = sqlite3.connect(str(p))
    conn.executescript(OLD_CHATS)
    conn.execute(
        "INSERT INTO chats(id, title, username, kind, is_forum, members, synced_at) "
        "VALUES(-1002000001, 'Старая группа', 'oldgroup', 'supergroup', 0, 42, 100)")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(chats)")}
        try:
            row = db.conn.execute(
                "SELECT title, members, created_by_me, my_role FROM chats "
                "WHERE id = -1002000001").fetchone()
            sel_err = None
        except sqlite3.OperationalError as e:
            row, sel_err = None, str(e)
        try:
            db.conn.execute("SELECT * FROM invite_links").fetchall()
            tbl_err = None
        except sqlite3.OperationalError as e:
            tbl_err = str(e)
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check(sel_err is None and row is not None and row["title"] == "Старая группа"
          and row["members"] == 42 and row["created_by_me"] == 0 and row["my_role"] is None,
          "v3->v4: строка пережила миграцию (created_by_me=0, my_role=NULL)",
          sel_err or (str(dict(row)) if row is not None else "нет строки"))
    check({"created_by_me", "my_role"} <= cols, "v3->v4: колонки добавлены",
          str(sorted(cols)))
    check(tbl_err is None, "v3->v4: invite_links создана", str(tbl_err))
    check(uv == 4, "v3->v4: user_version = 4", str(uv))


def test_second_open(tmp: Path) -> None:
    print("=== v4: повторное открытие без DDL ===")
    p = tmp / "idem.sqlite"
    with DB(p) as db:
        db.upsert_chat(-1002000002, "Группа", "grp", "group", False, 10)
    sv1 = schema_version(p)
    with DB(p) as db:
        links = db.invite_links_list()
    sv2 = schema_version(p)
    check(sv1 == sv2, "v4: второе открытие не делает DDL (schema_version не изменился)",
          f"{sv1}/{sv2}")
    check(links == [], "v4: invite_links читается пустой", str(links))


def test_readonly_no_migrate(tmp: Path) -> None:
    print("=== v4: read-only open не мигрирует ===")
    p = tmp / "ro.sqlite"
    conn = sqlite3.connect(str(p))
    conn.executescript(OLD_CHATS)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    ro = DB(p, readonly=True)
    try:
        cols = {r[1] for r in ro.conn.execute("PRAGMA table_info(chats)")}
        uv = ro.conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        ro.close()
    check("created_by_me" not in cols and uv == 3,
          "readonly: v3 не мигрирует (колонок нет, user_version=3)",
          f"{uv}/{sorted(cols)}")


def test_upsert_compat(tmp: Path) -> None:
    print("=== upsert_chat: совместимость и новые kwargs ===")
    p = tmp / "upsert.sqlite"
    with DB(p) as db:
        db.upsert_chat(-1002000003, "Первая", "one", "group", False, 5,
                       created_by_me=1, my_role="owner")
        db.upsert_chat(-1002000003, "Вторая", "two", "group", True, 7)
        row = db.conn.execute(
            "SELECT title, is_forum, members, created_by_me, my_role FROM chats "
            "WHERE id = -1002000003").fetchone()
        db.upsert_chat(-1002000004, "Без полей", None, "channel", False, None)
        row2 = db.conn.execute(
            "SELECT created_by_me, my_role FROM chats WHERE id = -1002000004").fetchone()
    check(row["title"] == "Вторая" and row["is_forum"] == 1 and row["members"] == 7,
          "upsert_chat: обычные поля обновились", str(dict(row)))
    check(row["created_by_me"] == 1 and row["my_role"] == "owner",
          "upsert_chat: без новых kwargs флаги пережили апдейт", str(dict(row)))
    check(row2["created_by_me"] == 0 and row2["my_role"] is None,
          "upsert_chat: свежая вставка без kwargs -> created_by_me=0", str(dict(row2)))


def test_upsert_new_kwargs(tmp: Path) -> None:
    print("=== upsert_chat: новые kwargs пишутся ===")
    p = tmp / "upsert2.sqlite"
    with DB(p) as db:
        db.upsert_chat(-1002000005, "Группа", "g", "group", False, 3)
        db.upsert_chat(-1002000005, "Группа", "g", "group", False, 4, my_role="admin")
        row = db.conn.execute(
            "SELECT my_role, created_by_me FROM chats WHERE id = -1002000005").fetchone()
    check(row["my_role"] == "admin", "upsert_chat: my_role записан", str(dict(row)))
    check(row["created_by_me"] == 0,
          "upsert_chat: created_by_me не задан -> остался 0", str(dict(row)))


def test_manage_fields(tmp: Path) -> None:
    print("=== chat_set_manage_fields ===")
    p = tmp / "manage.sqlite"
    with DB(p) as db:
        db.upsert_chat(-1002000006, "Управляемая", "mg", "group", False, 5)
        ok = db.chat_set_manage_fields(-1002000006, created_by_me=1)
        row = db.conn.execute(
            "SELECT created_by_me, my_role FROM chats WHERE id = -1002000006").fetchone()
        ok2 = db.chat_set_manage_fields(-1002000006, my_role="owner")
        row2 = db.conn.execute(
            "SELECT created_by_me, my_role FROM chats WHERE id = -1002000006").fetchone()
        missing = db.chat_set_manage_fields(-1002099999, my_role="x")
        empty = db.chat_set_manage_fields(-1002000006)
    check(ok and row["created_by_me"] == 1 and row["my_role"] is None,
          "chat_set_manage_fields: created_by_me обновлён, my_role не тронут",
          str(dict(row)))
    check(ok2 and row2["created_by_me"] == 1 and row2["my_role"] == "owner",
          "chat_set_manage_fields: частичный апдейт my_role", str(dict(row2)))
    check(missing is False, "chat_set_manage_fields: неизвестный id -> False")
    check(empty is False, "chat_set_manage_fields: без полей -> False")


def test_invite_links(tmp: Path) -> None:
    print("=== invite_links: add / list / REPLACE ===")
    p = tmp / "invites.sqlite"
    with DB(p) as db:
        db.upsert_chat(-1002000007, "Группа", "inv", "group", False, 9)
        db.invite_link_add(-1002000007, "https://t.me/inv1", title="Первый",
                           expires_at=1000, usage_limit=5, request_needed=True)
        db.invite_link_add(-1002000007, "https://t.me/inv2", title="Второй")
        db.invite_link_add(-1002000007, "https://t.me/inv3", title="Третий", revoked=True)
        db.invite_link_add(-1002000007, "https://t.me/inv3", title="Третий (обновлён)")
        rows = db.invite_links_list(-1002000007)
        all_rows = db.invite_links_list()
        other = db.invite_links_list(-1002000008)
    by_link = {r["link"]: r for r in rows}
    check(len(rows) == 3, "invite_links_list: по chat_id три строки (REPLACE по ключу)",
          str(sorted(by_link)))
    check(by_link["https://t.me/inv1"]["title"] == "Первый"
          and by_link["https://t.me/inv1"]["expires_at"] == 1000
          and by_link["https://t.me/inv1"]["usage_limit"] == 5
          and by_link["https://t.me/inv1"]["request_needed"] == 1
          and by_link["https://t.me/inv1"]["revoked"] == 0
          and isinstance(by_link["https://t.me/inv1"]["created_at"], int),
          "invite_link_add: поля записаны", str(by_link["https://t.me/inv1"]))
    check(by_link["https://t.me/inv2"]["request_needed"] == 0
          and by_link["https://t.me/inv2"]["revoked"] == 0,
          "invite_link_add: дефолты request_needed/revoked", str(by_link["https://t.me/inv2"]))
    check(by_link["https://t.me/inv3"]["title"] == "Третий (обновлён)"
          and by_link["https://t.me/inv3"]["revoked"] == 0,
          "invite_link_add: REPLACE по (chat_id, link) перезаписал поля",
          str(by_link["https://t.me/inv3"]))
    check(len(all_rows) == 3, "invite_links_list: без фильтра все строки", str(len(all_rows)))
    check(other == [], "invite_links_list: чужой chat_id -> пусто", str(other))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tg_manage_"))
    tests = (test_fresh_schema, test_v3_migration, test_second_open,
             test_readonly_no_migrate, test_upsert_compat, test_upsert_new_kwargs,
             test_manage_fields, test_invite_links)
    try:
        for fn in tests:
            try:
                fn(tmp)
            except (AttributeError, TypeError) as e:
                check(False, f"{fn.__name__}: {type(e).__name__}", str(e))
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
