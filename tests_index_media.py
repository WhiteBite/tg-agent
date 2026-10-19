#!/usr/bin/env python3
"""Правки/удаления сообщений в индексе + аудио-гейт очереди медиа.

Что реально исполняется:
  1. db.py: колонки edited_ts/deleted, миграция старых баз, update_message,
     mark_deleted, FTS-триггер на UPDATE
  2. sync.py: message_row с edit_date, подтягивание правок при рескане
     (pull_chat / backfill / backfill_window), в очередь медиа — только аудио
  3. live.py: handle_edit / handle_delete
  4. media.py: is_transcribable
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB  # noqa: E402
import sync as sync_mod  # noqa: E402
import media as media_mod  # noqa: E402
import live as live_mod  # noqa: E402

NOW = datetime.now(timezone.utc)
FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------- fakes

class User:
    def __init__(self, uid: int, first_name: str):
        self.id, self.first_name = uid, first_name
        self.last_name, self.username = "", ""


class Channel:
    def __init__(self, cid: int, title: str, forum: bool = False):
        self.id, self.title, self.username = cid, title, None
        self.megagroup, self.broadcast, self.forum = True, False, forum
        self.participants_count = 10


class Dialog:
    def __init__(self, entity, name):
        self.entity, self.name = entity, name


class Topic:
    def __init__(self, tid: int, title: str, top_message: int):
        self.id, self.top_message, self.closed = tid, top_message, False
        self.title = title
        self.notify_date = int(NOW.timestamp())


class ForumTopicsRes:
    def __init__(self, topics):
        self.topics = topics


class Msg:
    def __init__(self, mid, date, sender, text, media=None, reply_to=None,
                 edit_date=None, views=None, forwards=None, reactions=None):
        self.id, self.date, self.message, self.media = mid, date, text, media
        self.sender_id = sender.id if sender else None
        self._sender = sender
        self.reply_to, self.edit_date = reply_to, edit_date
        self.out, self.action = False, None
        self.views, self.forwards = views, forwards
        self.reactions = reactions
        self.pinned, self.fwd_from = False, None


class DocumentAttributeVoice:
    def __init__(self, duration=5):
        self.duration = duration


class DocumentAttributeAudio:
    def __init__(self, duration=5):
        self.duration = duration


class Document:
    def __init__(self, attributes, mime_type="", size=1000):
        self.attributes, self.mime_type, self.size = attributes, mime_type, size


class MessageMediaDocument:
    def __init__(self, document):
        self.document = document


class MessageMediaVoice:  # как в test_offline: голосовое — document с Voice-атрибутом
    def __init__(self):
        self.document = Document([DocumentAttributeVoice()], "audio/ogg")


class MessageMediaPhoto:
    pass


class FakeClient:
    def __init__(self, groups: dict[int, dict]):
        self.groups = groups
        self.flood_mid = 0

    def _group(self, ref: Any) -> dict:
        k = int(getattr(ref, "id", ref))
        if k in self.groups:
            return self.groups[k]
        alt = {-1000000000000 - k, -k} if k > 0 else (-k - 1000000000000,)
        for a in alt:
            if a in self.groups:
                return self.groups[a]
        raise KeyError(k)

    async def get_dialogs(self):
        return [Dialog(g["entity"], g["entity"].title) for g in self.groups.values()]

    async def get_entity(self, peer):
        return self._group(peer)["entity"]

    async def iter_messages(self, entity, limit=None, reply_to=None,
                            offset_id=0, reverse=False, offset_date=None, **kw):
        msgs = self._group(entity)["messages"]
        if offset_date is not None:
            msgs = [m for m in msgs if m.date > offset_date]
        elif offset_id:
            msgs = [m for m in msgs if m.id > offset_id]
        msgs = sorted(msgs, key=lambda m: m.id, reverse=not reverse)
        if limit:
            msgs = msgs[:limit]
        for i, m in enumerate(msgs):
            if self.flood_mid and i == self.flood_mid:
                self.flood_mid = 0
                from telethon.errors import FloodWaitError
                e = FloodWaitError(None, 0)
                e.seconds = -1  # _flood_sleep ждёт seconds+1: -1 -> без паузы
                raise e
            yield m

    async def get_messages(self, entity, limit=None, filter=None, ids=None, **kw):
        msgs = self._group(entity)["messages"]
        if ids is not None:
            by_id = {m.id: m for m in msgs}
            seq = ids if isinstance(ids, (list, tuple)) else [ids]
            return [by_id.get(i) for i in seq]
        if filter is not None:
            msgs = [m for m in msgs if getattr(m, "pinned", False)]
        return msgs

    async def __call__(self, req):
        name = type(req).__name__
        if name == "GetDialogFiltersRequest":
            return []
        assert name == "GetForumTopicsRequest", name
        peer = getattr(req, "peer", None) or getattr(req, "channel", None)
        gid = int(getattr(peer, "id", peer))
        return ForumTopicsRes(self.groups[gid]["topics"])


OLD_MESSAGES = """
CREATE TABLE messages (
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    topic_id    INTEGER NOT NULL DEFAULT 1,
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


# ---------------------------------------------------------------- db

def test_db_edits(tmp: Path) -> None:
    print("=== db: edited_ts/deleted, update_message, mark_deleted, FTS ===")
    p = tmp / "db_edits.sqlite"
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(messages)")}
        db.insert_message({"chat_id": -100800, "msg_id": 1, "topic_id": 1,
                           "ts": 100, "text": "без правки"})
        db.insert_message({"chat_id": -100800, "msg_id": 2, "topic_id": 1,
                           "ts": 100, "text": "уже с правкой", "edited_ts": 111})
        r1 = db.conn.execute(
            "SELECT edited_ts, deleted FROM messages WHERE msg_id=1").fetchone()
        r2 = db.conn.execute(
            "SELECT edited_ts, deleted FROM messages WHERE msg_id=2").fetchone()
    check({"edited_ts", "deleted"} <= cols, "db: колонки edited_ts/deleted в схеме")
    check(r1["edited_ts"] is None and r1["deleted"] == 0,
          "db: вставка без правки — edited_ts NULL, deleted 0")
    check(r2["edited_ts"] == 111 and r2["deleted"] == 0,
          "db: вставка с edited_ts сохраняет его")

    with DB(p) as db:
        ok = db.update_message({"chat_id": -100800, "msg_id": 1, "topic_id": 3,
                                "ts": 999, "sender_id": 42, "sender_name": "X",
                                "text": "v2", "media": "document", "views": 10,
                                "forwards": 2, "reactions": [{"emoji": "🔥", "count": 3}],
                                "edited_ts": 150})
        row = db.conn.execute(
            "SELECT * FROM messages WHERE chat_id=-100800 AND msg_id=1").fetchone()
    check(ok, "db.update_message: применилась")
    check(row["text"] == "v2" and row["media"] == "document" and row["views"] == 10
          and row["forwards"] == 2 and row["edited_ts"] == 150
          and json.loads(row["reactions"])[0]["count"] == 3,
          "db.update_message: контент обновлён", str(dict(row)))
    check(row["topic_id"] == 1 and row["ts"] == 100 and row["sender_id"] is None
          and row["sender_name"] is None and row["deleted"] == 0,
          "db.update_message: identity-поля не тронуты")

    with DB(p) as db:
        same = db.update_message({"chat_id": -100800, "msg_id": 1, "text": "v2",
                                  "edited_ts": 150})
        newer = db.update_message({"chat_id": -100800, "msg_id": 1, "text": "v3",
                                   "edited_ts": 200})
        older = db.update_message({"chat_id": -100800, "msg_id": 1, "text": "v2",
                                   "edited_ts": 150})
        text = db.conn.execute(
            "SELECT text FROM messages WHERE msg_id=1").fetchone()["text"]
        missing = db.update_message({"chat_id": -100800, "msg_id": 999,
                                     "text": "x", "edited_ts": 1})
    check(same is False, "db.update_message: та же правка повторно — False")
    check(newer is True and older is False and text == "v3",
          "db.update_message: новее применяется, старее не откатывает", text)
    check(missing is False, "db.update_message: несуществующая строка — False")

    with DB(p) as db:
        d1 = db.mark_deleted(-100800, 1)
        d2 = db.mark_deleted(-100800, 1)
        d3 = db.mark_deleted(-100800, 999)
        deleted = db.conn.execute(
            "SELECT deleted FROM messages WHERE msg_id=1").fetchone()["deleted"]
    check(d1 and not d2 and not d3 and deleted == 1,
          "db.mark_deleted: ставит флаг, повтор и чужой id — False",
          f"{d1}/{d2}/{d3}/{deleted}")

    p2 = tmp / "db_fts.sqlite"
    with DB(p2) as db:
        db.insert_message({"chat_id": -100800, "msg_id": 1, "topic_id": 1,
                           "ts": 100, "text": "старыйтекст"})
        db.update_message({"chat_id": -100800, "msg_id": 1, "text": "новыйтекст",
                           "edited_ts": 150})
        res = db.search_fts("новыйтекст", chat_id=-100800)
        res_old = db.search_fts("старыйтекст", chat_id=-100800)
        fts_new = fts_old = fts_after = -1
        if db.fts:
            fts_new = db.conn.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE text='новыйтекст'").fetchone()[0]
            fts_old = db.conn.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE text='старыйтекст'").fetchone()[0]
            db.mark_deleted(-100800, 1)
            fts_after = db.conn.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE text='новыйтекст'").fetchone()[0]
    check(res["count"] == 1 and res_old["count"] == 0,
          "db: после правки поиск находит новый текст, старый — нет",
          f"{res['count']}/{res_old['count']}")
    check(db.fts and fts_new == 1 and fts_old == 0 and fts_after == 0,
          "db: FTS-триггер UPDATE синхронизирует индекс, mark_deleted вычищает строку",
          f"{fts_new}/{fts_old}/{fts_after}")


def test_migration(tmp: Path) -> None:
    print("=== db: миграция старых баз ===")
    p = tmp / "old_plain.sqlite"
    conn = sqlite3.connect(str(p))
    conn.executescript(OLD_MESSAGES)
    conn.execute("INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) "
                 "VALUES(-100900, 7, 1, 100, 'старое')")
    conn.commit()
    conn.close()
    with DB(p) as db:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(messages)")}
        row = db.conn.execute(
            "SELECT text, edited_ts, deleted FROM messages "
            "WHERE chat_id=-100900 AND msg_id=7").fetchone()
    check({"edited_ts", "deleted"} <= cols, "миграция: колонки добавлены в старую базу")
    check(row is not None and row["text"] == "старое" and row["edited_ts"] is None
          and row["deleted"] == 0, "миграция: данные старой базы целы",
          str(dict(row) if row else None))

    p2 = tmp / "old_marked.sqlite"
    conn = sqlite3.connect(str(p2))
    conn.executescript(OLD_MESSAGES)
    conn.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY, title TEXT, "
                 "username TEXT, kind TEXT, is_forum INTEGER, members INTEGER, "
                 "synced_at INTEGER)")
    conn.execute("INSERT INTO chats(id, title, kind) VALUES(12345, 'Старая', 'supergroup')")
    conn.execute("INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) "
                 "VALUES(12345, 1, 1, 100, 'до миграции')")
    conn.commit()
    conn.close()
    with DB(p2) as db:
        row = db.conn.execute(
            "SELECT text, edited_ts, deleted FROM messages "
            "WHERE chat_id=? AND msg_id=1",
            (-1000000000000 - 12345,)).fetchone()
    check(row is not None and row["text"] == "до миграции"
          and row["edited_ts"] is None and row["deleted"] == 0,
          "миграция: marked-id перенос несёт новые колонки",
          str(dict(row) if row else None))


# ---------------------------------------------------------------- sync

def test_message_row() -> None:
    print("=== sync.message_row: edit_date ===")
    users = User(1, "Аня")
    edited_at = NOW + timedelta(minutes=5)
    m = Msg(7, NOW, users, "текст", edit_date=edited_at)
    row = sync_mod.message_row(m, -100730, 1, None)
    check(row["edited_ts"] == int(edited_at.timestamp()),
          "message_row: edit_date -> edited_ts", str(row["edited_ts"]))
    check(row["deleted"] is False, "message_row: deleted=False")
    row2 = sync_mod.message_row(Msg(8, NOW, users, "текст"), -100730, 1, None)
    check(row2["edited_ts"] is None, "message_row: без правки edited_ts=None")


def test_sync_media_gate(tmp: Path) -> None:
    print("=== sync: в очередь медиа попадает только аудио ===")
    p = tmp / "gate.sqlite"
    users = User(1, "Валя")
    group = Channel(-100720, "Гейт", forum=False)
    msgs = [
        Msg(1, NOW - timedelta(minutes=10), users, "голосовое",
            media=MessageMediaVoice()),
        Msg(2, NOW - timedelta(minutes=9), users, "pdf",
            media=MessageMediaDocument(Document([], "application/pdf"))),
        Msg(3, NOW - timedelta(minutes=8), users, "аудио",
            media=MessageMediaDocument(Document([DocumentAttributeAudio(120)], "audio/mpeg"))),
        Msg(4, NOW - timedelta(minutes=7), users, "фото", media=MessageMediaPhoto()),
        Msg(5, NOW - timedelta(minutes=6), users, "zip",
            media=MessageMediaDocument(Document([], "application/zip", 50 * 1024 * 1024))),
    ]
    client = FakeClient({-100720: {"entity": group, "messages": msgs, "topics": []}})
    asyncio.run(sync_mod.run(db_path=str(p), client=client, names=["-100720"]))
    with DB(p) as db:
        queued = {r["msg_id"]: r["kind"] for r in db.conn.execute(
            "SELECT msg_id, kind FROM media_assets")}
        nmsg = db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    check(queued == {1: "voice", 3: "document"},
          "sync: enqueue только аудио (voice + audio-document)", str(queued))
    check(nmsg == 5, "sync: все 5 сообщений записаны", str(nmsg))


def test_sync_edit_pickup(tmp: Path) -> None:
    print("=== sync: правки при рескане (pull_chat) ===")
    p = tmp / "edits.sqlite"
    users = User(1, "Гоша")
    group = Channel(-100700, "Правки", forum=False)
    msgs = [Msg(1, NOW - timedelta(hours=3), users, "первое"),
            Msg(2, NOW - timedelta(hours=2), users, "второе"),
            Msg(3, NOW - timedelta(hours=1), users, "третье")]
    client = FakeClient({-100700: {"entity": group, "messages": msgs, "topics": []}})
    rep1 = asyncio.run(sync_mod.run(db_path=str(p), client=client, names=["-100700"]))
    check(sum(c["new_messages"] for c in rep1["chats"]) == 3,
          "sync: первый прогон — 3 новых")

    m4 = Msg(4, NOW - timedelta(minutes=30), users, "четвёртое")
    m5 = Msg(5, NOW - timedelta(minutes=25), users, "пятое")
    msgs.append(m4)
    msgs.append(m5)
    with DB(p) as db:
        live_mod.handle_message(db, m4, -100700, False)
    m4.message = "четвёртое (исправлено) https://fix.test/a"
    m4.edit_date = NOW

    rep2 = asyncio.run(sync_mod.run(db_path=str(p), client=client, names=["-100700"]))
    r2 = rep2["chats"][0]
    with DB(p) as db:
        row = db.conn.execute(
            "SELECT text, edited_ts FROM messages "
            "WHERE chat_id=-100700 AND msg_id=4").fetchone()
        refs = db.refs_summary(chat_id=-100700).get("url", [])
    check(r2["new_messages"] == 1 and r2["edited_messages"] == 1,
          "sync: рескан — новое и правка считаются раздельно",
          str({k: r2[k] for k in ("new_messages", "edited_messages")}))
    check(row["text"] == "четвёртое (исправлено) https://fix.test/a"
          and row["edited_ts"] == int(NOW.timestamp()),
          "sync: правка записана в строку", str(dict(row)))
    check(any(r["value"] == "https://fix.test/a" for r in refs),
          "sync: refs из правки записаны", str(refs))

    rep3 = asyncio.run(sync_mod.run(db_path=str(p), client=client, names=["-100700"]))
    r3 = rep3["chats"][0]
    check(r3["new_messages"] == 0 and r3["edited_messages"] == 0,
          "sync: третий прогон идемпотентен")

    m4.message = "четвёртое (ещё раз исправлено)"
    m4.edit_date = NOW + timedelta(minutes=1)
    with DB(p) as db:
        asyncio.run(sync_mod.backfill_window(client, db, days=30))
        text4 = db.conn.execute(
            "SELECT text FROM messages WHERE chat_id=-100700 AND msg_id=4").fetchone()["text"]
    check(text4 == "четвёртое (ещё раз исправлено)",
          "sync: более новая правка перезаписывает старую (окно)", text4)

    p2 = tmp / "flood_edits.sqlite"
    msgs2 = [Msg(1, NOW - timedelta(hours=3), users, "первое"),
             Msg(2, NOW - timedelta(hours=2), users, "второе"),
             Msg(3, NOW - timedelta(hours=1), users, "третье")]
    client2 = FakeClient({-100700: {"entity": Channel(-100700, "Правки2", forum=False),
                                     "messages": msgs2, "topics": []}})
    asyncio.run(sync_mod.run(db_path=str(p2), client=client2, names=["-100700"]))
    m4b = Msg(4, NOW - timedelta(minutes=30), users, "четвёртое")
    msgs2.append(m4b)
    with DB(p2) as db:
        live_mod.handle_message(db, m4b, -100700, False)
    m4b.message = "правка после флуда"
    m4b.edit_date = NOW
    client2.flood_mid = 1
    rep5 = asyncio.run(sync_mod.run(db_path=str(p2), client=client2, names=["-100700"]))
    with DB(p2) as db:
        text = db.conn.execute(
            "SELECT text FROM messages WHERE chat_id=-100700 AND msg_id=4").fetchone()["text"]
    check(rep5["chats"][0]["edited_messages"] == 1 and text == "правка после флуда",
          "sync: FloodWait-ретрай не двоит счётчик правок",
          str(rep5["chats"][0]["edited_messages"]))


def test_backfill_edits(tmp: Path) -> None:
    print("=== sync: правки через backfill (ниже курсора) ===")
    p = tmp / "bf.sqlite"
    users = User(1, "Дима")
    group = Channel(-100710, "Бэкфилл", forum=False)
    msgs = [Msg(1, NOW - timedelta(days=1), users, "первое"),
            Msg(2, NOW - timedelta(days=1), users, "второе")]
    client = FakeClient({-100710: {"entity": group, "messages": msgs, "topics": []}})
    asyncio.run(sync_mod.run(db_path=str(p), client=client, names=["-100710"]))
    msgs[0].message = "первое (исправлено)"
    msgs[0].edit_date = NOW
    with DB(p) as db:
        asyncio.run(sync_mod.backfill(client, db, -100710, forum=False, sleep=0))
        row = db.conn.execute(
            "SELECT text, edited_ts FROM messages "
            "WHERE chat_id=-100710 AND msg_id=1").fetchone()
    check(row["text"] == "первое (исправлено)"
          and row["edited_ts"] == int(NOW.timestamp()),
          "backfill: правка ниже курсора подхвачена", str(dict(row)))


def test_backfill_window_edits(tmp: Path) -> None:
    print("=== sync: правки через backfill_window ===")
    p = tmp / "window.sqlite"
    users = User(1, "Еля")
    group = Channel(-100715, "Окно", forum=False)
    msgs = [Msg(1, NOW - timedelta(days=1), users, "первое"),
            Msg(2, NOW - timedelta(days=1), users, "второе")]
    client = FakeClient({-100715: {"entity": group, "messages": msgs, "topics": []}})
    asyncio.run(sync_mod.run(db_path=str(p), client=client, names=["-100715"]))
    msgs[1].message = "второе (исправлено)"
    msgs[1].edit_date = NOW
    with DB(p) as db:
        asyncio.run(sync_mod.backfill_window(client, db, days=30))
        row = db.conn.execute(
            "SELECT text, edited_ts FROM messages "
            "WHERE chat_id=-100715 AND msg_id=2").fetchone()
    check(row["text"] == "второе (исправлено)"
          and row["edited_ts"] == int(NOW.timestamp()),
          "backfill_window: правка ниже курсора подхвачена", str(dict(row)))


# ---------------------------------------------------------------- live

def test_live_edit_delete(tmp: Path) -> None:
    print("=== live: handle_edit / handle_delete ===")
    p = tmp / "live.sqlite"
    users = User(1, "Аня")
    base = Msg(10, NOW - timedelta(hours=1), users, "исходный текст")
    with DB(p) as db:
        ok_ins = live_mod.handle_message(db, base, -100700, False)
        edited = Msg(10, NOW - timedelta(hours=1), users,
                     "решение отменено https://edit.test/y", edit_date=NOW)
        ok1 = live_mod.handle_edit(db, edited, -100700, False)
        ok2 = live_mod.handle_edit(db, edited, -100700, False)
        row = db.conn.execute(
            "SELECT text, edited_ts, deleted FROM messages "
            "WHERE chat_id=-100700 AND msg_id=10").fetchone()
        refs = db.refs_summary(chat_id=-100700).get("url", [])
    check(ok_ins and ok1 and not ok2,
          "live.handle_edit: правка применена + идемпотентность",
          f"{ok_ins}/{ok1}/{ok2}")
    check(row["text"] == "решение отменено https://edit.test/y"
          and row["edited_ts"] == int(NOW.timestamp()) and row["deleted"] == 0,
          "live.handle_edit: строка обновлена", str(dict(row)))
    check(any(r["value"] == "https://edit.test/y" for r in refs),
          "live.handle_edit: refs от правки записаны", str(refs))

    unknown = Msg(11, NOW - timedelta(minutes=30), users,
                  "правка невиданного", edit_date=NOW)
    with DB(p) as db:
        ok3 = live_mod.handle_edit(db, unknown, -100700, False)
        row11 = db.conn.execute(
            "SELECT text, edited_ts FROM messages "
            "WHERE chat_id=-100700 AND msg_id=11").fetchone()
    check(ok3 and row11 is not None and row11["text"] == "правка невиданного"
          and row11["edited_ts"] == int(NOW.timestamp()),
          "live.handle_edit: неизвестное сообщение вставляется целиком",
          str(dict(row11) if row11 else None))

    with DB(p) as db:
        n1 = live_mod.handle_delete(db, -100700, [10, 11, 999])
        n2 = live_mod.handle_delete(db, -100700, [10])
        d10 = db.conn.execute(
            "SELECT deleted FROM messages WHERE msg_id=10").fetchone()["deleted"]
        d11 = db.conn.execute(
            "SELECT deleted FROM messages WHERE msg_id=11").fetchone()["deleted"]
    check(n1 == 2 and n2 == 0 and d10 == 1 and d11 == 1,
          "live.handle_delete: помечает существующие, повтор и чужие — 0",
          f"{n1}/{n2}/{d10}/{d11}")

    pdf = Msg(12, NOW, users, "pdf",
              media=MessageMediaDocument(Document([], "application/pdf")))
    aud = Msg(13, NOW, users, "аудио",
              media=MessageMediaDocument(Document([DocumentAttributeVoice(10)], "audio/ogg")))
    m14 = Msg(14, NOW, users, "без медиа")
    with DB(p) as db:
        live_mod.handle_message(db, pdf, -100700, False)
        live_mod.handle_message(db, aud, -100700, False)
        live_mod.handle_message(db, m14, -100700, False)
        m14e = Msg(14, NOW, users, "теперь с голосовым",
                   media=MessageMediaDocument(
                       Document([DocumentAttributeVoice(9)], "audio/ogg")),
                   edit_date=NOW)
        live_mod.handle_edit(db, m14e, -100700, False)
        queued = {r["msg_id"] for r in db.conn.execute(
            "SELECT msg_id FROM media_assets WHERE chat_id=-100700")}
    check(queued == {13, 14},
          "live: не-аудио документ не ставится в очередь, правка с аудио — ставится",
          str(sorted(queued)))


def test_is_transcribable() -> None:
    print("=== media.is_transcribable ===")
    cases = [
        (Msg(1, NOW, None, "", media=MessageMediaDocument(
            Document([DocumentAttributeVoice(30)], "application/octet-stream"))), True,
         "document с Voice-атрибутом"),
        (Msg(2, NOW, None, "", media=MessageMediaDocument(
            Document([DocumentAttributeAudio(120)], "audio/mpeg"))), True,
         "document с Audio-атрибутом"),
        (Msg(3, NOW, None, "", media=MessageMediaDocument(
            Document([], "audio/ogg"))), True, "mime audio/*"),
        (Msg(4, NOW, None, "", media=MessageMediaDocument(
            Document([], "application/pdf"))), False, "pdf"),
        (Msg(5, NOW, None, "", media=MessageMediaDocument(
            Document([], "application/zip"))), False, "zip"),
        (Msg(6, NOW, None, "", media=MessageMediaPhoto()), False, "фото"),
        (Msg(7, NOW, None, "текст"), False, "без медиа"),
        (Msg(8, NOW, None, "", media=MessageMediaVoice()), True,
         "MessageMediaVoice (голосовое с Voice-атрибутом)"),
    ]
    for msg, want, label in cases:
        check(media_mod.is_transcribable(msg) == want,
              f"is_transcribable: {label} -> {want}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_index_media_"))
    try:
        test_db_edits(tmp)
        test_migration(tmp)
        test_message_row()
        test_sync_media_gate(tmp)
        test_sync_edit_pickup(tmp)
        test_backfill_edits(tmp)
        test_backfill_window_edits(tmp)
        test_live_edit_delete(tmp)
        test_is_transcribable()
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
