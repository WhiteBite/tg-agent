#!/usr/bin/env python3
"""Управление чатами: миграция v4 (created_by_me / my_role у chats, таблица
invite_links), совместимость upsert_chat с новыми kwargs, точечные апдейты
chat_set_manage_fields и инвайт-ссылки; создание групп/супергрупп/каналов/
форумов через manage.create_chat — гейт TG_ALLOW_MANAGE, фейк-клиент,
регистрация созданного в индексе; настройки чатов через manage.set_title /
set_about / set_photo / set_username / toggle_pre_history / toggle_invites /
toggle_signatures / toggle_forum / set_default_rights — диспетчер базовая
группа vs канал, сохранение флагов ChatBannedRights, is_forum в индексе;
темы форума через manage.topic_create / topic_edit — CreateForumTopicRequest
с random_id, topic_id из service-сообщения MessageActionTopicCreated,
EditForumTopicRequest с title/closed, title переживает close-only правку,
closed в индексе, не-форум и гейт TG_ALLOW_MANAGE отклоняются."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB, SCHEMA_VERSION  # noqa: E402
import sync as sync_mod  # noqa: E402

try:
    import manage as manage_mod  # noqa: E402
except ImportError:
    manage_mod = None


class FakeChat(SimpleNamespace):
    pass


class FakeChannel(SimpleNamespace):
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class ChannelForUpdates(SimpleNamespace):
    pass


def _fake_marked_id(ent):
    eid = getattr(ent, "id", 0)
    if eid < 0:
        return eid
    if isinstance(ent, ChannelForUpdates) or type(ent).__name__ == "Channel":
        return -1000000000000 - eid
    return -eid


def _fake_kind_of(ent):
    name = type(ent).__name__
    if name in ("ChannelForUpdates", "Channel"):
        return "channel" if getattr(ent, "broadcast", False) else "supergroup"
    return "group"


@dataclass
class FakeClient:
    responses: list

    def __post_init__(self):
        self.requests = []

    async def __call__(self, req):
        self.requests.append(req)
        return self.responses.pop(0)


def _create(client, title, *, gate=True, **kw):
    saved = os.environ.get("TG_ALLOW_MANAGE")
    if gate:
        os.environ["TG_ALLOW_MANAGE"] = "1"
    else:
        os.environ.pop("TG_ALLOW_MANAGE", None)
    orig = (sync_mod.marked_id, sync_mod.kind_of)
    sync_mod.marked_id, sync_mod.kind_of = _fake_marked_id, _fake_kind_of
    try:
        return asyncio.run(manage_mod.create_chat(client, title, **kw))
    finally:
        sync_mod.marked_id, sync_mod.kind_of = orig
        if saved is None:
            os.environ.pop("TG_ALLOW_MANAGE", None)
        else:
            os.environ["TG_ALLOW_MANAGE"] = saved


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


def test_create_group(tmp: Path) -> None:
    print("=== create: обычная группа ===")
    p = tmp / "create_group.sqlite"
    resp = SimpleNamespace(updates=SimpleNamespace(chats=[
        FakeChat(id=-456, title="TG", username="tg", forum=False,
                 participants_count=10)]))
    client = FakeClient([resp])
    res = _create(client, "TG", kind="group", db_path=str(p))
    row = None
    with DB(p) as db:
        row = db.conn.execute(
            "SELECT title, username, kind, is_forum, members, created_by_me, my_role "
            "FROM chats WHERE id = -456").fetchone()
    check(res.get("ok") is True and res.get("chat_id") == -456
          and res.get("kind") == "group" and res.get("is_forum") is False
          and res.get("registered") is True,
          "create group: ok, chat_id=-456, kind=group", str(res))
    check(len(client.requests) == 1
          and type(client.requests[0]).__name__ == "CreateChatRequest"
          and client.requests[0].title == "TG"
          and client.requests[0].users == [],
          "create group: CreateChatRequest(users=[], title)",
          str([type(r).__name__ for r in client.requests]))
    check(row is not None and row["title"] == "TG" and row["username"] == "tg"
          and row["kind"] == "group" and row["is_forum"] == 0
          and row["members"] == 10 and row["created_by_me"] == 1
          and row["my_role"] == "owner",
          "create group: в индексе created_by_me=1, my_role=owner",
          str(dict(row)) if row is not None else "нет строки")


def test_create_supergroup(tmp: Path) -> None:
    print("=== create: супергруппа ===")
    p = tmp / "create_sg.sqlite"
    resp = SimpleNamespace(chats=[
        ChannelForUpdates(id=-1002000001, title="SG", megagroup=True,
                          broadcast=False, forum=False, participants_count=7)])
    client = FakeClient([resp])
    res = _create(client, "SG", kind="supergroup", db_path=str(p))
    row = None
    with DB(p) as db:
        row = db.conn.execute(
            "SELECT kind, is_forum, created_by_me, my_role FROM chats "
            "WHERE id = -1002000001").fetchone()
    req = client.requests[0] if client.requests else None
    check(res.get("ok") is True and res.get("chat_id") == -1002000001
          and res.get("kind") == "supergroup" and res.get("registered") is True,
          "create supergroup: ok, chat_id=-1002000001, kind=supergroup", str(res))
    check(req is not None and type(req).__name__ == "CreateChannelRequest"
          and req.megagroup is True and req.broadcast is None and req.forum is None
          and req.title == "SG",
          "create supergroup: CreateChannelRequest(megagroup=True)",
          str(type(req).__name__) if req is not None else "нет запроса")
    check(row is not None and row["kind"] == "supergroup" and row["is_forum"] == 0
          and row["created_by_me"] == 1 and row["my_role"] == "owner",
          "create supergroup: в индексе created_by_me=1, my_role=owner",
          str(dict(row)) if row is not None else "нет строки")


def test_create_forum(tmp: Path) -> None:
    print("=== create: форум ===")
    p = tmp / "create_forum.sqlite"
    resp = SimpleNamespace(chats=[
        ChannelForUpdates(id=-1002000001, title="SG", megagroup=True,
                          broadcast=False, forum=True)])
    client = FakeClient([resp])
    res = _create(client, "SG", kind="supergroup", forum=True, db_path=str(p))
    row = None
    with DB(p) as db:
        row = db.conn.execute(
            "SELECT kind, is_forum, created_by_me, my_role FROM chats "
            "WHERE id = -1002000001").fetchone()
    req = client.requests[0] if client.requests else None
    check(res.get("ok") is True and res.get("is_forum") is True,
          "create forum: ok, is_forum=True", str(res))
    check(req is not None and type(req).__name__ == "CreateChannelRequest"
          and req.forum is True,
          "create forum: CreateChannelRequest(forum=True)",
          str(type(req).__name__) if req is not None else "нет запроса")
    check(row is not None and row["is_forum"] == 1 and row["kind"] == "supergroup"
          and row["created_by_me"] == 1 and row["my_role"] == "owner",
          "create forum: в индексе is_forum=1",
          str(dict(row)) if row is not None else "нет строки")


def test_create_channel(tmp: Path) -> None:
    print("=== create: канал ===")
    p = tmp / "create_channel.sqlite"
    resp = SimpleNamespace(chats=[
        ChannelForUpdates(id=-1002000001, title="CH", broadcast=True,
                          megagroup=False, forum=False)])
    client = FakeClient([resp])
    res = _create(client, "CH", kind="channel", db_path=str(p))
    row = None
    with DB(p) as db:
        row = db.conn.execute(
            "SELECT kind, created_by_me, my_role FROM chats "
            "WHERE id = -1002000001").fetchone()
    req = client.requests[0] if client.requests else None
    check(res.get("ok") is True and res.get("kind") == "channel",
          "create channel: ok, kind=channel", str(res))
    check(req is not None and type(req).__name__ == "CreateChannelRequest"
          and req.broadcast is True and req.megagroup is None
          and req.about == "" and req.title == "CH",
          "create channel: broadcast=True, about пустой",
          str(type(req).__name__) if req is not None else "нет запроса")
    check(row is not None and row["kind"] == "channel" and row["created_by_me"] == 1
          and row["my_role"] == "owner",
          "create channel: в индексе created_by_me=1, my_role=owner",
          str(dict(row)) if row is not None else "нет строки")


def test_create_gate_off(tmp: Path) -> None:
    print("=== create: гейт TG_ALLOW_MANAGE выключен ===")
    client = FakeClient([])
    res = _create(client, "X", gate=False)
    check(res == {"ok": False, "error": "manage заблокирован: нужен TG_ALLOW_MANAGE=1"},
          "gate off: операция заблокирована", str(res))
    check(client.requests == [], "gate off: к Telegram ни одного запроса",
          str(client.requests))


def test_create_validation(tmp: Path) -> None:
    print("=== create: валидация входа ===")
    client = FakeClient([])
    bad_kind = _create(client, "X", kind="bogus")
    forum_group = _create(client, "X", kind="group", forum=True)
    empty_title = _create(client, "   ")
    check(bad_kind.get("ok") is False and "kind" in bad_kind.get("error", ""),
          "create: неизвестный kind отклонён", str(bad_kind))
    check(forum_group.get("ok") is False and "forum" in forum_group.get("error", ""),
          "create: forum без supergroup отклонён", str(forum_group))
    check(empty_title.get("ok") is False and "title" in empty_title.get("error", ""),
          "create: пустой title отклонён", str(empty_title))
    check(client.requests == [], "create: валидация до запросов к Telegram",
          str(client.requests))


def test_create_no_db(tmp: Path) -> None:
    print("=== create: без регистрации в индексе ===")
    resp = SimpleNamespace(chats=[
        ChannelForUpdates(id=-1002000001, title="SG", megagroup=True)])
    client = FakeClient([resp])
    res = _create(client, "SG", kind="supergroup")
    check(res.get("ok") is True and res.get("registered") is False
          and res.get("chat_id") == -1002000001,
          "create no-db: ok, registered=False", str(res))


class Chat(SimpleNamespace):
    pass


class Channel(SimpleNamespace):
    pass


@dataclass
class FakeClientEnt(FakeClient):
    entity: Any = None

    async def get_entity(self, ref):
        return self.entity

    async def upload_file(self, path):
        self.uploaded_path = str(path)
        return SimpleNamespace(SUBCLASS_OF_ID=0xE7655F1F)


def _run(fn, client, *args, gate=True, **kw):
    saved = os.environ.get("TG_ALLOW_MANAGE")
    if gate:
        os.environ["TG_ALLOW_MANAGE"] = "1"
    else:
        os.environ.pop("TG_ALLOW_MANAGE", None)
    try:
        return asyncio.run(fn(client, *args, **kw))
    finally:
        if saved is None:
            os.environ.pop("TG_ALLOW_MANAGE", None)
        else:
            os.environ["TG_ALLOW_MANAGE"] = saved


def test_settings_title(tmp: Path) -> None:
    print("=== settings: title ===")
    chat = Chat(id=-456, title="Old")
    client = FakeClientEnt([SimpleNamespace()], entity=chat)
    res = _run(manage_mod.set_title, client, "@chat", "New")
    req = client.requests[0] if client.requests else None
    check(res == {"ok": True} and req is not None
          and type(req).__name__ == "EditChatTitleRequest"
          and req.chat_id == -456 and req.title == "New",
          "set title: Chat -> EditChatTitleRequest(chat_id, title)", str(res))
    ch = Channel(id=-1002000001, title="Old", broadcast=False, megagroup=True)
    client2 = FakeClientEnt([SimpleNamespace()], entity=ch)
    res2 = _run(manage_mod.set_title, client2, "@ch", "New2")
    req2 = client2.requests[0] if client2.requests else None
    check(res2 == {"ok": True} and req2 is not None
          and type(req2).__name__ == "EditTitleRequest"
          and req2.channel is ch and req2.title == "New2",
          "set title: Channel -> channels.EditTitleRequest", str(res2))


def test_settings_about(tmp: Path) -> None:
    print("=== settings: about ===")
    ch = Channel(id=-1002000001, title="SG", broadcast=False, megagroup=True)
    client = FakeClientEnt([SimpleNamespace()], entity=ch)
    res = _run(manage_mod.set_about, client, "@ch", "новое описание")
    req = client.requests[0] if client.requests else None
    check(res == {"ok": True} and req is not None
          and type(req).__name__ == "EditChatAboutRequest"
          and req.peer is ch and req.about == "новое описание",
          "set about: EditChatAboutRequest(peer, about)", str(res))
    chat = Chat(id=-456, title="G")
    client2 = FakeClientEnt([], entity=chat)
    res2 = _run(manage_mod.set_about, client2, "@g", "x")
    check(res2.get("ok") is False and "about" in res2.get("error", "")
          and client2.requests == [],
          "set about: базовая группа -> ошибка", str(res2))


def test_settings_photo(tmp: Path) -> None:
    print("=== settings: photo ===")
    img = tmp / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    ch = Channel(id=-1002000001, title="SG", broadcast=False, megagroup=True)
    client = FakeClientEnt([SimpleNamespace()], entity=ch)
    res = _run(manage_mod.set_photo, client, "@ch", str(img))
    req = client.requests[0] if client.requests else None
    check(res == {"ok": True} and req is not None
          and type(req).__name__ == "EditPhotoRequest"
          and type(req.photo).__name__ == "InputChatUploadedPhoto"
          and client.uploaded_path == str(img),
          "set photo: Channel -> EditPhotoRequest(InputChatUploadedPhoto)", str(res))
    chat = Chat(id=-456, title="G")
    client2 = FakeClientEnt([SimpleNamespace()], entity=chat)
    res2 = _run(manage_mod.set_photo, client2, "@g", str(img))
    req2 = client2.requests[0] if client2.requests else None
    check(res2 == {"ok": True} and req2 is not None
          and type(req2).__name__ == "EditChatPhotoRequest"
          and req2.chat_id == -456
          and type(req2.photo).__name__ == "InputChatUploadedPhoto",
          "set photo: Chat -> EditChatPhotoRequest(chat_id, photo)", str(res2))
    client3 = FakeClientEnt([], entity=ch)
    res3 = _run(manage_mod.set_photo, client3, "@ch", str(tmp / "missing.png"))
    check(res3.get("ok") is False and client3.requests == [],
          "set photo: нет файла -> ошибка без запросов", str(res3))


def test_settings_username(tmp: Path) -> None:
    print("=== settings: username ===")
    ch = Channel(id=-1002000001, title="SG", broadcast=False, megagroup=True)
    client = FakeClientEnt([True, SimpleNamespace()], entity=ch)
    res = _run(manage_mod.set_username, client, "@ch", "newname")
    names = [type(r).__name__ for r in client.requests]
    check(res == {"ok": True}
          and names == ["CheckUsernameRequest", "UpdateUsernameRequest"]
          and client.requests[1].username == "newname",
          "set username: Check + UpdateUsernameRequest", str(res))
    ch2 = Channel(id=-1002000002, title="SG2", broadcast=False, megagroup=True)
    client2 = FakeClientEnt([SimpleNamespace()], entity=ch2)
    res2 = _run(manage_mod.set_username, client2, "@ch", "")
    req2 = client2.requests[0] if client2.requests else None
    check(res2 == {"ok": True} and req2 is not None
          and type(req2).__name__ == "DeactivateAllUsernamesRequest",
          "set username: пустая строка -> DeactivateAllUsernamesRequest", str(res2))
    chat = Chat(id=-456, title="G")
    client3 = FakeClientEnt([], entity=chat)
    res3 = _run(manage_mod.set_username, client3, "@g", "x")
    check(res3.get("ok") is False and "username" in res3.get("error", "")
          and client3.requests == [],
          "set username: базовая группа -> ошибка", str(res3))


def test_settings_toggle_pre_history(tmp: Path) -> None:
    print("=== settings: pre history ===")
    sg = Channel(id=-1002000001, title="SG", broadcast=False, megagroup=True)
    client = FakeClientEnt([SimpleNamespace(), SimpleNamespace()], entity=sg)
    res = _run(manage_mod.toggle_pre_history, client, "@sg", True)
    res2 = _run(manage_mod.toggle_pre_history, client, "@sg", False)
    reqs = client.requests
    check(res == {"ok": True} and res2 == {"ok": True} and len(reqs) == 2
          and all(type(r).__name__ == "TogglePreHistoryHiddenRequest" for r in reqs)
          and reqs[0].enabled is True and reqs[1].enabled is False,
          "toggle pre history: TogglePreHistoryHiddenRequest(True/False)", str(res))
    chat = Chat(id=-456, title="G")
    client2 = FakeClientEnt([], entity=chat)
    res3 = _run(manage_mod.toggle_pre_history, client2, "@g", True)
    check(res3.get("ok") is False and client2.requests == [],
          "toggle pre history: базовая группа -> ошибка", str(res3))
    bc = Channel(id=-1002000003, title="BC", broadcast=True, megagroup=False)
    client3 = FakeClientEnt([], entity=bc)
    res4 = _run(manage_mod.toggle_pre_history, client3, "@bc", True)
    check(res4.get("ok") is False and client3.requests == [],
          "toggle pre history: broadcast-канал -> ошибка", str(res4))


def test_settings_toggle_invites(tmp: Path) -> None:
    print("=== settings: invites ===")
    from telethon.tl.types import ChatBannedRights
    cur = ChatBannedRights(None, send_messages=True, change_info=True)
    sg = Channel(id=-1002000001, title="SG", broadcast=False, megagroup=True,
                 default_banned_rights=cur)
    client = FakeClientEnt([SimpleNamespace(), SimpleNamespace()], entity=sg)
    res = _run(manage_mod.toggle_invites, client, "@sg", False)
    res2 = _run(manage_mod.toggle_invites, client, "@sg", True)
    r0 = client.requests[0].banned_rights
    r1 = client.requests[1].banned_rights
    check(res == {"ok": True} and res2 == {"ok": True}
          and type(client.requests[0]).__name__ == "EditChatDefaultBannedRightsRequest"
          and r0.invite_users is True and r0.send_messages is True
          and r0.change_info is True,
          "toggle invites off: invite_users=True (запрет), остальные сохранены",
          str(res))
    check(r1.invite_users is False and r1.send_messages is True
          and r1.change_info is True,
          "toggle invites on: invite_users=False, остальные сохранены")
    plain = Channel(id=-1002000002, title="SG2", broadcast=False, megagroup=True)
    client2 = FakeClientEnt([SimpleNamespace()], entity=plain)
    res3 = _run(manage_mod.toggle_invites, client2, "@sg2", False)
    r2 = client2.requests[0].banned_rights
    check(res3 == {"ok": True} and r2.invite_users is True
          and not any(getattr(r2, f) for f in
                      ("send_messages", "change_info", "pin_messages")),
          "toggle invites: default_banned_rights=None -> все разрешены + запрет инвайтов",
          str(res3))


def test_settings_toggle_forum(tmp: Path) -> None:
    print("=== settings: forum ===")
    p = tmp / "forum.sqlite"
    with DB(p) as db:
        db.upsert_chat(-1002000001, "SG", "sg", "supergroup", False, 5)
    sg = Channel(id=-1002000001, title="SG", username="sg", broadcast=False,
                 megagroup=True, participants_count=5)
    client = FakeClientEnt([SimpleNamespace(), SimpleNamespace()], entity=sg)
    res = _run(manage_mod.toggle_forum, client, "@sg", True, db_path=str(p))
    row1 = None
    with DB(p) as db:
        row1 = db.conn.execute(
            "SELECT is_forum FROM chats WHERE id = -1002000001").fetchone()
    res2 = _run(manage_mod.toggle_forum, client, "@sg", False, db_path=str(p))
    row2 = None
    with DB(p) as db:
        row2 = db.conn.execute(
            "SELECT is_forum, title, kind FROM chats WHERE id = -1002000001").fetchone()
    reqs = client.requests
    check(res == {"ok": True} and res2 == {"ok": True} and len(reqs) == 2
          and all(type(r).__name__ == "ToggleForumRequest" for r in reqs)
          and reqs[0].enabled is True and reqs[0].tabs is False
          and reqs[1].enabled is False,
          "toggle forum: ToggleForumRequest(enabled, tabs=False)", str(res))
    check(row1 is not None and row1["is_forum"] == 1,
          "toggle forum on: is_forum=1 в индексе",
          str(dict(row1)) if row1 is not None else "нет строки")
    check(row2 is not None and row2["is_forum"] == 0 and row2["title"] == "SG"
          and row2["kind"] == "supergroup",
          "toggle forum off: is_forum=0, строка не побита",
          str(dict(row2)) if row2 is not None else "нет строки")
    chat = Chat(id=-456, title="G")
    client2 = FakeClientEnt([], entity=chat)
    res3 = _run(manage_mod.toggle_forum, client2, "@g", True)
    check(res3.get("ok") is False and client2.requests == [],
          "toggle forum: базовая группа -> ошибка", str(res3))


def test_settings_toggle_signatures(tmp: Path) -> None:
    print("=== settings: signatures ===")
    ch = Channel(id=-1002000001, title="CH", broadcast=True, megagroup=False)
    client = FakeClientEnt([SimpleNamespace()], entity=ch)
    res = _run(manage_mod.toggle_signatures, client, "@ch", True)
    req = client.requests[0] if client.requests else None
    check(res == {"ok": True} and req is not None
          and type(req).__name__ == "ToggleSignaturesRequest"
          and req.signatures_enabled is True,
          "toggle signatures: ToggleSignaturesRequest(signatures_enabled)", str(res))
    chat = Chat(id=-456, title="G")
    client2 = FakeClientEnt([], entity=chat)
    res2 = _run(manage_mod.toggle_signatures, client2, "@g", True)
    check(res2.get("ok") is False and client2.requests == [],
          "toggle signatures: базовая группа -> ошибка", str(res2))


def test_settings_rights(tmp: Path) -> None:
    print("=== settings: rights ===")
    sg = Channel(id=-1002000001, title="SG", broadcast=False, megagroup=True)
    client = FakeClientEnt([SimpleNamespace()], entity=sg)
    res = _run(manage_mod.set_default_rights, client, "@sg",
               {"send_messages": True, "pin_messages": True, "invite_users": False})
    req = client.requests[0] if client.requests else None
    check(res == {"ok": True} and req is not None
          and type(req).__name__ == "EditChatDefaultBannedRightsRequest"
          and req.banned_rights.send_messages is True
          and req.banned_rights.pin_messages is True
          and req.banned_rights.invite_users is False
          and req.banned_rights.view_messages is None,
          "rights: заданные флаги в ChatBannedRights, незаданные не тронуты",
          str(res))
    client2 = FakeClientEnt([], entity=sg)
    res2 = _run(manage_mod.set_default_rights, client2, "@sg", {"bogus_flag": True})
    check(res2.get("ok") is False and client2.requests == [],
          "rights: неизвестный флаг -> ошибка без запросов", str(res2))


def test_settings_gate_off(tmp: Path) -> None:
    print("=== settings: гейт TG_ALLOW_MANAGE выключен ===")
    ch = Channel(id=-1002000001, title="SG", broadcast=False, megagroup=True)
    ops = (
        (manage_mod.set_title, ("@ch", "T")),
        (manage_mod.set_about, ("@ch", "A")),
        (manage_mod.set_photo, ("@ch", "x.png")),
        (manage_mod.set_username, ("@ch", "u")),
        (manage_mod.toggle_pre_history, ("@ch", True)),
        (manage_mod.toggle_invites, ("@ch", True)),
        (manage_mod.toggle_signatures, ("@ch", True)),
        (manage_mod.toggle_forum, ("@ch", True)),
        (manage_mod.set_default_rights, ("@ch", {"send_messages": True})),
    )
    blocked = {"ok": False, "error": "manage заблокирован: нужен TG_ALLOW_MANAGE=1"}
    all_blocked = True
    for fn, args in ops:
        client = FakeClientEnt([], entity=ch)
        res = _run(fn, client, *args, gate=False)
        if res != blocked or client.requests != []:
            all_blocked = False
    check(all_blocked, "gate off: все девять операций заблокированы без запросов")


class MessageActionTopicCreated:
    pass


def test_topic_create(tmp: Path) -> None:
    print("=== topics: создание ===")
    p = tmp / "topic_create.sqlite"
    resp = SimpleNamespace(updates=[
        SimpleNamespace(message=SimpleNamespace(
            id=42, action=MessageActionTopicCreated()))])
    forum = Channel(id=-1002000001, title="F", broadcast=False, megagroup=True,
                    forum=True)
    client = FakeClientEnt([resp], entity=forum)
    res = _run(manage_mod.topic_create, client, "@f", "Тема", db_path=str(p))
    req = client.requests[0] if client.requests else None
    row = None
    with DB(p) as db:
        row = db.conn.execute(
            "SELECT title, closed FROM topics WHERE chat_id = -1002000001 "
            "AND topic_id = 42").fetchone()
    check(res.get("ok") is True and res.get("chat_id") == -1002000001
          and res.get("topic_id") == 42 and res.get("title") == "Тема",
          "topic create: ok, topic_id=42 из service-сообщения", str(res))
    check(req is not None and type(req).__name__ == "CreateForumTopicRequest"
          and req.peer is forum and req.title == "Тема"
          and req.icon_color is None and req.random_id > 0,
          "topic create: CreateForumTopicRequest(peer, title, random_id)",
          str(type(req).__name__) if req is not None else "нет запроса")
    check(row is not None and row["title"] == "Тема" and row["closed"] == 0,
          "topic create: в индексе title и closed=0",
          str(dict(row)) if row is not None else "нет строки")


def test_topic_create_not_forum(tmp: Path) -> None:
    print("=== topics: создание не в форуме ===")
    sg = Channel(id=-1002000002, title="SG", broadcast=False, megagroup=True,
                 forum=False)
    client = FakeClientEnt([], entity=sg)
    res = _run(manage_mod.topic_create, client, "@sg", "Тема")
    check(res.get("ok") is False and client.requests == [],
          "topic create: не-форум -> ошибка без запросов", str(res))


def test_topic_edit(tmp: Path) -> None:
    print("=== topics: правка и закрытие ===")
    p = tmp / "topic_edit.sqlite"
    forum = Channel(id=-1002000001, title="F", broadcast=False, megagroup=True,
                    forum=True)
    client = FakeClientEnt([SimpleNamespace(), SimpleNamespace()], entity=forum)
    res = _run(manage_mod.topic_edit, client, "@f", 42,
               title="Новое имя", closed=True, db_path=str(p))
    req = client.requests[0] if client.requests else None
    row1 = None
    with DB(p) as db:
        row1 = db.conn.execute(
            "SELECT title, closed FROM topics WHERE chat_id = -1002000001 "
            "AND topic_id = 42").fetchone()
    res2 = _run(manage_mod.topic_edit, client, "@f", 42,
                closed=False, db_path=str(p))
    req2 = client.requests[1] if len(client.requests) > 1 else None
    row2 = None
    with DB(p) as db:
        row2 = db.conn.execute(
            "SELECT title, closed FROM topics WHERE chat_id = -1002000001 "
            "AND topic_id = 42").fetchone()
    client2 = FakeClientEnt([], entity=forum)
    res3 = _run(manage_mod.topic_edit, client2, "@f", 42)
    check(res == {"ok": True} and req is not None
          and type(req).__name__ == "EditForumTopicRequest"
          and req.peer is forum and req.topic_id == 42
          and req.title == "Новое имя" and req.closed is True,
          "topic edit: EditForumTopicRequest(peer, topic_id, title, closed)",
          str(res))
    check(row1 is not None and row1["title"] == "Новое имя"
          and row1["closed"] == 1,
          "topic edit: в индексе title и closed=1",
          str(dict(row1)) if row1 is not None else "нет строки")
    check(res2 == {"ok": True} and req2 is not None
          and req2.title is None and req2.closed is False,
          "topic close: EditForumTopicRequest(closed=False) без title", str(res2))
    check(row2 is not None and row2["title"] == "Новое имя"
          and row2["closed"] == 0,
          "topic close: title пережил close-only правку, closed=0",
          str(dict(row2)) if row2 is not None else "нет строки")
    check(res3.get("ok") is False and client2.requests == [],
          "topic edit: без title и closed -> ошибка без запросов", str(res3))


def test_topic_gate_off(tmp: Path) -> None:
    print("=== topics: гейт TG_ALLOW_MANAGE выключен ===")
    forum = Channel(id=-1002000001, title="F", broadcast=False, megagroup=True,
                    forum=True)
    blocked = {"ok": False, "error": "manage заблокирован: нужен TG_ALLOW_MANAGE=1"}
    client = FakeClientEnt([], entity=forum)
    res = _run(manage_mod.topic_create, client, "@f", "Т", gate=False)
    client2 = FakeClientEnt([], entity=forum)
    res2 = _run(manage_mod.topic_edit, client2, "@f", 42, title="Т", gate=False)
    check(res == blocked and res2 == blocked
          and client.requests == [] and client2.requests == [],
          "gate off: topic_create и topic_edit заблокированы без запросов")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tg_manage_"))
    tests = (test_fresh_schema, test_v3_migration, test_second_open,
             test_readonly_no_migrate, test_upsert_compat, test_upsert_new_kwargs,
             test_manage_fields, test_invite_links,
             test_create_group, test_create_supergroup, test_create_forum,
             test_create_channel, test_create_gate_off, test_create_validation,
             test_create_no_db,
             test_settings_title, test_settings_about, test_settings_photo,
             test_settings_username, test_settings_toggle_pre_history,
             test_settings_toggle_invites, test_settings_toggle_forum,
              test_settings_toggle_signatures, test_settings_rights,
              test_settings_gate_off,
              test_topic_create, test_topic_create_not_forum,
              test_topic_edit, test_topic_gate_off)
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
