#!/usr/bin/env python3
"""Офлайн-проверка всего стека без доступа к Telegram.

Что реально исполняется:
  1. sync.run() на подставном клиенте -> пишет SQLite-индекс (темы, курсоры, счётчики)
  2. повторный sync.run() -> инкрементальность (новое добавляется, старое не дублируется)
  3. функции чтения из db.py (chats/topics/history/search/stats/run_sql)
  4. НАСТОЯЩИЙ server.py как подпроцесс по MCP stdio: initialize -> tools/list -> tools/call
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# тесты герметичны: пользовательский .env (реальные ключи) не читается ни здесь, ни в подпроцессах
os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB, run_sql  # noqa: E402
import sync as sync_mod  # noqa: E402

NOW = datetime.now(timezone.utc)
FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------- fake telethon

class FakeMedia:
    pass


class MessageMediaPhoto(FakeMedia):
    pass


class MessageMediaVoice(FakeMedia):
    pass


class MessageActionTopicCreated:
    def __init__(self, title: str = "новая тема"):
        self.title = title


class MessageReplyHeader:
    def __init__(self, reply_to_msg_id: int, forum_topic: bool = False,
                 reply_to_top_id: int | None = None):
        self.reply_to_msg_id = reply_to_msg_id
        self.forum_topic = forum_topic
        self.reply_to_top_id = reply_to_top_id


class User:
    def __init__(self, uid: int, first_name: str, last_name: str = "", username: str = ""):
        self.id, self.first_name, self.last_name, self.username = uid, first_name, last_name, username


class PeerChan:
    def __init__(self, channel_id=None, user_id=None):
        self.channel_id, self.user_id = channel_id, user_id


class FwdFrom:
    def __init__(self, channel_id=None):
        self.from_id = PeerChan(channel_id)


class Msg:  # имитация telethon.tl.types.Message
    def __init__(self, mid, date, sender, text, media=None, reply_to=None, out=False,
                 action=None, pinned=False, fwd_from=None, reactions=None):
        self.id, self.date, self.message, self.media = mid, date, text, media
        self.sender_id = sender.id if sender else None
        self._sender = sender
        self.reply_to, self.out, self.action = reply_to, out, action
        self.views, self.forwards = None, None
        self.reactions = reactions
        self.pinned, self.fwd_from = pinned, fwd_from


class MessageServiceMsg:  # service message «тема создана»
    def __init__(self, mid, date, sender, action):
        self.id, self.date, self.sender_id, self._sender = mid, date, sender.id, sender
        self.action, self.message, self.media = action, None, None
        self.reply_to, self.out, self.reactions = None, False, None
        self.views, self.forwards = None, None


class Channel:  # супергруппа-форум
    def __init__(self, cid, title, username, forum=True):
        self.id, self.title, self.username = cid, title, username
        self.megagroup, self.broadcast, self.forum = True, False, forum
        self.participants_count = 42


class Dialog:
    def __init__(self, entity, name):
        self.entity, self.name = entity, name


class TopicTitle:
    def __init__(self, text):
        self.text = text


class Topic:
    def __init__(self, tid, title, top_message, closed=False):
        self.id, self.top_message, self.closed = tid, top_message, closed
        self.title = title  # в Telethon ForumTopic.title — обычная строка
        self.notify_date = int(NOW.timestamp())


class ForumTopicsRes:
    def __init__(self, topics):
        self.topics = topics


class GlobalRes:  # подмена ответа messages.SearchGlobalRequest
    def __init__(self, chats=(), users=()):
        self.chats = list(chats)
        self.users = list(users)
        self.messages = []


class FakeGetForumTopicsRequest:  # подмена messages.GetForumTopicsRequest
    def __init__(self, **kw):
        self.kw = kw


class FakePeerChat:
    def __init__(self, chat_id):
        self.chat_id = chat_id


class FakeFilter:
    def __init__(self, fid, title, peers):
        self.id, self.title, self.include_peers = fid, title, peers


class ReactionRes:
    def __init__(self, emoticon, count):
        self.reaction = type("R", (), {"emoticon": emoticon})()
        self.count = count


class Reactions:
    def __init__(self, *pairs):
        self.results = [ReactionRes(e, c) for e, c in pairs]


class FakeClient:
    """Мини-Telethon: get_dialogs / get_entity / iter_messages / __call__."""

    def __init__(self, groups: dict[int, dict], folders: dict[int, list[int]] | None = None):
        self.groups = groups  # chat_id -> {"entity":..., "messages":[...]}
        self.folders = folders or {}
        self.flood_once = False
        self.flood_mid = 0  # >0: FloodWait после N-го сообщения первой итерации
        self.folder_id_invalid = False  # getDialogs(folder=) отвечает FolderIdInvalid
        self.global_chats: list = []   # ответ на SearchGlobalRequest
        self.global_users: list = []
        self.last_global_req = None
        self.filter_updates: list = []  # UpdateDialogFilterRequest, посланные folders.py
        self.folder_filters = [(fid, t, ids) for fid, ids in (folders or {}).items()
                               for t in ({7: "AI Agent", 3: "Dev"}.get(fid, f"папка{fid}"),)]

    def _group(self, ref: Any) -> dict:
        # как Telethon: резолвит и сырой, и marked id одного и того же чата
        k = int(getattr(ref, "id", ref))
        if k in self.groups:
            return self.groups[k]
        if k > 0:
            alt = {-1000000000000 - k, -k}
        elif k < -1000000000000:
            alt = {-k - 1000000000000}
        else:
            alt = {-k}
        for a in alt:
            if a in self.groups:
                return self.groups[a]
        raise KeyError(k)

    async def get_dialogs(self):
        return [Dialog(g["entity"], g["entity"].title) for g in self.groups.values()]

    async def get_entity(self, peer):
        return self._group(peer)["entity"]

    def get_input_entity(self, peer):
        return peer

    async def iter_messages(self, entity, limit=None, reply_to=None,
                            offset_id=0, reverse=False, **kw):
        if self.flood_once:  # симуляция лимита Telegram: первый проход падает
            self.flood_once = False
            from telethon.errors import FloodWaitError
            e = FloodWaitError(None, 0)
            e.seconds = 0
            raise e
        msgs = self._group(entity)["messages"]
        if reply_to is not None:
            # Telegram отдаёт тред темы: сообщения с reply_to=topic_id + сама тема
            msgs = [m for m in msgs
                    if (getattr(m.reply_to, "reply_to_msg_id", None) == reply_to
                        and getattr(m.reply_to, "forum_topic", False)) or m.id == reply_to]
        elif offset_id:
            # backfill идёт снизу вверх: всё, что старше курсора
            msgs = [m for m in msgs if m.id > offset_id]
        msgs = sorted(msgs, key=lambda m: m.id, reverse=not reverse)
        if limit:
            msgs = msgs[:limit]
        for i, m in enumerate(msgs):
            if self.flood_mid and i == self.flood_mid:
                self.flood_mid = 0
                from telethon.errors import FloodWaitError
                e = FloodWaitError(None, 0)
                e.seconds = 0
                raise e
            yield m

    async def get_messages(self, entity, limit=None, filter=None, **kw):
        msgs = self._group(entity)["messages"]
        if filter is not None:  # InputMessagesFilterPinned
            msgs = [m for m in msgs if getattr(m, "pinned", False)]
        msgs = sorted(msgs, key=lambda m: m.id, reverse=True)
        return msgs[:limit] if limit else msgs

    async def iter_dialogs(self, folder=None, **kw):
        if folder is not None and self.folder_id_invalid:
            from telethon.errors import FolderIdInvalidError
            raise FolderIdInvalidError(None)
        if folder is None:
            for g in self.groups.values():
                yield Dialog(g["entity"], g["entity"].title)
            return
        for cid in self.folders.get(folder, []):
            g = self.groups[cid]
            yield Dialog(g["entity"], g["entity"].title)

    async def __call__(self, req):
        name = type(req).__name__
        if name == "GetDialogFiltersRequest":
            return [FakeFilter(fid, title, [FakePeerChat(abs(c)) for c in chats])
                    for fid, title, chats in self.folder_filters]
        if name == "SearchGlobalRequest":
            self.last_global_req = req
            chats = list(self.global_chats)
            users = list(self.global_users)
            if getattr(req, "broadcasts_only", None):
                chats = [c for c in chats if getattr(c, "broadcast", False)]
                users = []
            elif getattr(req, "groups_only", None):
                chats = [c for c in chats if not getattr(c, "broadcast", False)]
                users = []
            elif getattr(req, "users_only", None):
                chats = []
            return GlobalRes(chats, users)
        if name == "UpdateDialogFilterRequest":
            self.filter_updates.append(req)
            ids = [sync_mod._peer_marked(p) or sync_mod.marked_id(p)
                   for p in (req.filter.include_peers or [])]
            for i, (fid, t, _) in enumerate(self.folder_filters):
                if fid == req.id:
                    self.folder_filters[i] = (fid, t, ids)
            return True
        assert name == "GetForumTopicsRequest", name
        peer = getattr(req, "peer", None) or getattr(req, "channel", None)
        gid = int(getattr(peer, "id", peer))
        assert getattr(req, "limit", None) == 100
        return ForumTopicsRes(self.groups[gid]["topics"])


def build_fixture() -> FakeClient:
    users = {1: User(1, "Аня", "К", "anya"), 2: User(2, "Борис"), 3: User(3, "Света")}
    forum = Channel(-100111, "Работа / Команда", "team_forum", forum=True)
    plain = Channel(-100222, "Флудилка", None, forum=False)
    news = Channel(-100333, "AI News", "ai_news", forum=False)
    news.broadcast, news.megagroup = True, False  # канал-вещание

    topics = [Topic(1, "General", 102), Topic(5, "Релизы", 101), Topic(6, "Оффтоп", 100, closed=True)]
    forum_msgs = [
        MessageServiceMsg(5, NOW - timedelta(days=9), users[1], MessageActionTopicCreated()),
        MessageServiceMsg(6, NOW - timedelta(days=9), users[1], MessageActionTopicCreated()),
        Msg(11, NOW - timedelta(days=5), users[1], "релиз 1.2 выкатили", reply_to=MessageReplyHeader(5, True)),
        Msg(12, NOW - timedelta(days=5), users[2], "проверил, ок", reply_to=MessageReplyHeader(5, True)),
        Msg(13, NOW - timedelta(days=4), users[3], "а когда 1.3?", reply_to=MessageReplyHeader(5, True)),
        Msg(14, NOW - timedelta(days=3), users[1], "кофе в пятницу", reply_to=MessageReplyHeader(6, True)),
        Msg(38, NOW - timedelta(hours=2), users[3], "мем дня", reply_to=MessageReplyHeader(6, True),
            reactions=Reactions(("🔥", 7), ("❤", 2))),
        Msg(15, NOW - timedelta(days=1), users[2], "общее объявление без темы"),
        Msg(16, NOW - timedelta(hours=2), users[1], "релиз 1.2.1 — хотфикс",
            media=MessageMediaPhoto(), reply_to=MessageReplyHeader(5, True)),
    ]
    plain_msgs = [
        Msg(31, NOW - timedelta(days=2), users[2], "привет из обычной группы"),
        Msg(32, NOW - timedelta(days=1), users[3], "релиз тут тоже упоминаем"),
        Msg(33, NOW - timedelta(hours=4), users[1], "закреп: правила чтения", pinned=True),
        Msg(34, NOW - timedelta(hours=3), users[2],
            "годнота: @durov, t.me/+secret42 и https://example.com/guide"),
        Msg(35, NOW - timedelta(hours=2), users[3], "пересланное из канала",
            fwd_from=FwdFrom(channel_id=777)),
        Msg(36, NOW - timedelta(hours=1), users[1], "текст\u200e с\u202e невидимками"),
        Msg(37, NOW - timedelta(minutes=40), users[2], "держите токен ghp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ]
    news_msgs = [
        Msg(41, NOW - timedelta(days=400), users[1], "старый пост двухлетней давности"),
        Msg(42, NOW - timedelta(days=30), users[2], "GPT-6 анонсировали"),
        Msg(43, NOW - timedelta(hours=5), users[3], "свежая новость про модели"),
    ]
    return FakeClient(
        {
            -100111: {"entity": forum, "messages": forum_msgs, "topics": topics},
            -100222: {"entity": plain, "messages": plain_msgs, "topics": []},
            -100333: {"entity": news, "messages": news_msgs, "topics": []},
        },
        folders={7: [-100111, -100333]},  # папка «AI Agent»: форум + канал
    )


# ------------------------------------------------------------------ 1. sync

def test_sync(db_path: Path) -> None:
    client = build_fixture()

    # sender_name не должен звать сеть из sync-кода: клиент без кэша -> idNNN
    import warnings

    class NoCacheClient:
        async def get_entity(self, pid):
            raise AssertionError("get_entity не должен зваться из sender_name")

    async def _bare_sender() -> str:
        m = Msg(99, NOW, User(1, "Аня", "К", "anya"), "x")
        del m._sender
        return sync_mod.message_row(m, -100111, 1, NoCacheClient())["sender_name"]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bare = asyncio.run(_bare_sender())
    check(bare == "id1" and not any("never awaited" in str(w.message) for w in caught),
          "sender_name: без кэша отдаёт idN без мусорной корутины", f"{bare}")

    # тема определяется по сообщению
    m = client.groups[-100111]["messages"][2]
    check(sync_mod.topic_of_message(m) == 5, "topic_of_message: сообщение в теме «Релизы» -> 5")
    m15 = next(m for m in client.groups[-100111]["messages"] if m.id == 15)
    check(sync_mod.topic_of_message(m15) is None,
          "topic_of_message: сообщение без темы -> None")
    check(sync_mod.topic_of_message(client.groups[-100111]["messages"][0]) == 5,
          "topic_of_message: service TopicCreated -> id самой темы")

    # ответ на сообщение ВНУТРИ темы: корень темы в reply_to_top_id, а не в адресате
    nested = Msg(77, NOW, User(1, "Аня"), "ответ внутри темы",
                 reply_to=MessageReplyHeader(555, True, reply_to_top_id=5))
    check(sync_mod.topic_of_message(nested) == 5,
          "topic_of_message: вложенный ответ -> корень темы, а не сообщение-адресат",
          str(sync_mod.topic_of_message(nested)))
    check(sync_mod.is_group(client.groups[-100333]["entity"]) is False,
          "is_group: канал-вещание НЕ группа (берётся только если назван явно)")

    # list_topics честно называет поле top_message, а не «messages»
    async def _list_topics() -> list[dict]:
        return await sync_mod.list_topics(client, client.groups[-100111]["entity"])

    tops = asyncio.run(_list_topics())
    check(tops and all("top_message" in t and "messages" not in t for t in tops),
          "list_topics: поле называется top_message, не messages", str(tops[0]))

    probe = db_path.parent / "probe.sqlite"
    rd = asyncio.run(sync_mod.run(db_path=str(probe), client=client, dry_run=True))
    with DB(probe) as pdb:
        check(pdb.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0,
              "sync --dry-run: в базу ничего не записано")
        check({c["chat_id"] for c in rd["chats"]} == {-100111, -100222},
              "sync --dry-run: без --chat канал-вещание не трогается",
              str({c['chat_id'] for c in rd['chats']}))

    rn = asyncio.run(sync_mod.run(db_path=str(probe), client=client,
                                  names=["@ai_news"], dry_run=True))
    check({c["chat_id"] for c in rn["chats"]} == {-100333},
          "sync --chat @канал: явно названный канал-вещание берётся",
          str({c['chat_id'] for c in rn['chats']}))

    # только форум и обычная группа (канал-вещание оставляем для теста папок)
    r1 = asyncio.run(sync_mod.run(db_path=str(db_path), client=client,
                                  pattern="Работа|Флудилка"))
    check({c["chat_id"] for c in r1["chats"]} == {-100111, -100222},
          "sync --pattern: выбраны 2 группы", str([c["chat_id"] for c in r1["chats"]]))
    forum = next(c for c in r1["chats"] if c["chat_id"] == -100111)
    check(forum["is_forum"] is True, "sync: группа-форум определена")
    check({t["topic_id"] for t in forum["topics"]} == {1, 5, 6},
          "sync: сообщения разложены по темам 1/5/6", str(forum["topics"]))
    check(sum(c["new_messages"] for c in r1["chats"]) == 16,
          "sync: записано 16 сообщений (9 форум + 7 обычных)",
          str(sum(c["new_messages"] for c in r1["chats"])))

    r2 = asyncio.run(sync_mod.run(db_path=str(db_path), client=client,
                                  pattern="Работа|Флудилка"))
    check(sum(c["new_messages"] for c in r2["chats"]) == 0,
          "sync: повторный прогон ничего не дублирует",
          str(sum(c["new_messages"] for c in r2["chats"])))

    users = {1: User(1, "Аня", "К", "anya")}
    client.groups[-100111]["messages"].append(
        Msg(39, NOW, users[1], "релиз 1.3 готов", reply_to=MessageReplyHeader(5, True))
    )
    r3 = asyncio.run(sync_mod.run(db_path=str(db_path), client=client,
                                  pattern="Работа|Флудилка"))
    check(sum(c["new_messages"] for c in r3["chats"]) == 1,
          "sync: одно новое сообщение добавлено", str(sum(c["new_messages"] for c in r3["chats"])))


# ------------------------------------------------ 2. папки и бэкфилл истории

def test_folder_fallback(db_path: Path) -> None:
    print("=== sync --folder: FolderIdInvalid -> состав из include_peers ===")
    client = build_fixture()
    client.folder_id_invalid = True
    fb_db = db_path.parent / "folder_fb.sqlite"
    for suf in ("", "-wal", "-shm"):
        f = fb_db.with_name(fb_db.name + suf)
        if f.exists():
            f.unlink()
    r = asyncio.run(sync_mod.run(db_path=str(fb_db), client=client, folder=7))
    ids = {c["chat_id"] for c in r["chats"]}
    check(ids == {-100111, -100333},
          "sync --folder: FolderIdInvalid — чаты взяты из include_peers папки",
          str(ids))
    news = next((c for c in r["chats"] if c["chat_id"] == -100333), None)
    check(news is not None and news["new_messages"] == 3,
          "sync --folder: фолбэк синхронизировал канал-вещание", str(news))


def test_folder_edit(db_path: Path) -> None:
    print("=== folders: add/remove чатов в папке ===")
    import folders as folders_mod
    client = build_fixture()
    fdb = db_path.parent / "folder_edit.sqlite"
    if fdb.exists():
        fdb.unlink()

    res = asyncio.run(folders_mod.folder_edit(client, 7, ["-100222"],
                                              mode="add", db_path=str(fdb)))
    check(res.get("ok") and res.get("changed") == 1 and res.get("peers") == 3,
          "folders: add доложил чат в папку", str(res))
    upd = client.filter_updates[-1]
    peers = sorted(sync_mod._peer_marked(p) or sync_mod.marked_id(p)
                   for p in upd.filter.include_peers)
    check(peers == [-100333, -100222, -100111],
          "folders: include_peers пополнился, старые на месте", str(peers))
    with DB(fdb) as db:
        got = {r["chat_id"] for r in db.conn.execute(
            "SELECT chat_id FROM folder_chats WHERE folder_id=7")}
    check(got == {-100111, -100222, -100333},
          "folders: локальная копия состава обновлена", str(sorted(got)))

    res2 = asyncio.run(folders_mod.folder_edit(client, 7, ["-100222"], mode="add"))
    check(res2.get("changed") == 0 and len(client.filter_updates) == 1,
          "folders: повторный add — no-op без запроса к Telegram", str(res2))

    res3 = asyncio.run(folders_mod.folder_edit(client, 7, ["-100222"], mode="remove"))
    check(res3.get("ok") and res3.get("changed") == 1 and res3.get("peers") == 2,
          "folders: remove убрал чат", str(res3))
    upd3 = client.filter_updates[-1]
    peers3 = sorted(sync_mod._peer_marked(p) for p in upd3.filter.include_peers)
    check(-100222 not in peers3 and peers3 == [-100333, -100111],
          "folders: после remove чата в папке нет", str(peers3))

    res4 = asyncio.run(folders_mod.folder_edit(client, 999, ["-100222"], mode="add"))
    check(res4.get("ok") is False and "не найдена" in res4.get("error", ""),
          "folders: несуществующая папка — структурная ошибка", str(res4))
    res5 = asyncio.run(folders_mod.folder_edit(client, 7, ["@nope"], mode="add"))
    check(res5.get("ok") is False and "не найден" in res5.get("error", ""),
          "folders: несуществующий чат — структурная ошибка", str(res5))


def test_folder_and_backfill(db_path: Path) -> None:
    client = build_fixture()

    r = asyncio.run(sync_mod.run(db_path=str(db_path), client=client, folder=7))
    ids = {c["chat_id"] for c in r["chats"]}
    check(ids == {-100111, -100333},
          "sync --folder: взяты только чаты из папки (форум + канал)", str(ids))
    news = next(c for c in r["chats"] if c["chat_id"] == -100333)
    check(news["new_messages"] == 3, "sync --folder: канал-вещание синхронизирован",
          f"new={news['new_messages']}")

    # слой 229: getDialogFilters отвечает объектом с .filters, title — TextWithEntities
    from telethon.tl.types import TextWithEntities

    class WrapFiltersRes:
        def __init__(self, filters):
            self.filters = filters

    class FoldersClient:
        def __init__(self, res):
            self._res = res

        async def __call__(self, req):
            assert type(req).__name__ == "GetDialogFiltersRequest", req
            return self._res

    wrap = FoldersClient(WrapFiltersRes([
        FakeFilter(7, TextWithEntities("AI Agent", []), [FakePeerChat(111)]),
        FakeFilter(3, "Dev", [FakePeerChat(222)]),
    ]))
    fl = asyncio.run(sync_mod.fetch_folders(wrap))
    check(fl == [{"id": 7, "title": "AI Agent", "peers": [-111]},
                 {"id": 3, "title": "Dev", "peers": [-222]}],
          "sync: fetch_folders разворачивает DialogFilters и TextWithEntities-title",
          str(fl))
    flat = FoldersClient([FakeFilter(5, "Plain", [FakePeerChat(333)])])
    fl2 = asyncio.run(sync_mod.fetch_folders(flat))
    check(fl2 == [{"id": 5, "title": "Plain", "peers": [-333]}],
          "sync: fetch_folders принимает и плоский список (старый Telethon)",
          str(fl2))

    # бэкфилл: повторный проход не должен ничего добавить
    with DB(db_path) as db:
        bf = asyncio.run(sync_mod.backfill(client, db, -100333, forum=False,
                                           chunk_size=2, sleep=0,
                                           entity=client.groups[-100333]["entity"]))
    check(bf["seen"] == 3 and bf["new"] == 0,
          "backfill: повторный проход ничего не дублирует", str(bf))

    # новый канал с длинной историей: бэкфилл чанками по 2 сообщения
    users = {1: User(1, "Аня"), 2: User(2, "Борис")}
    old = Channel(-100444, "Старый канал", None, forum=False)
    old.broadcast, old.megagroup = True, False
    client.groups[-100444] = {
        "entity": old,
        "messages": [Msg(100 + i, NOW - timedelta(days=900 - i), users[(i % 2) + 1],
                         f"пост номер {i}") for i in range(7)],
        "topics": [],
    }
    client.folders[7] = client.folders[7] + [-100444]
    with DB(db_path) as db:
        bf2 = asyncio.run(sync_mod.backfill(client, db, -100444, forum=False,
                                            chunk_size=2, sleep=0,
                                            entity=client.groups[-100444]["entity"]))
        got = db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=-100444").fetchone()[0]
    check(bf2["seen"] == 7 and got == 7,
          "backfill: 7 сообщений дососаны чанками по 2", f"seen={bf2['seen']} got={got}")
    with DB(db_path) as db:
        check(db.get_cursor(-100444, 0) == 106,
              "backfill: курсор сохранён на последнем сообщении",
              str(db.get_cursor(-100444, 0)))


# ------------------------------------------------------- 3. чтение из индекса

def test_reads(db_path: Path) -> None:
    # к этому моменту в базе: форум (9, включая 17), Флудилка (2), AI News (3), Старый (7)
    with DB(db_path) as db:
        chats = db.chats()
        check(len(chats) == 4, "db.chats: 4 чата", str([c["id"] for c in chats]))
        topics = db.topics(-100111)
        titles = {t["topic_id"]: t["title"] for t in topics}
        check(titles == {1: "General", 5: "Релизы", 6: "Оффтоп"},
              "db.topics: названия тем из Telegram", str(titles))
        rel = next(t for t in topics if t["topic_id"] == 5)
        gen = next(t for t in topics if t["topic_id"] == 1)
        check(rel["local_count"] == 6, "db.topics: в теме «Релизы» 6 сообщений",
              f"local_count={rel['local_count']}")
        check(gen["local_count"] == 1, "db.topics: сообщение без темы уехало в General",
              f"gen={gen['local_count']}")

        hist = db.history(-100111, topic_id=5)
        check([h["msg_id"] for h in hist] == [5, 11, 12, 13, 16, 39],
              "db.history(topic=5): 6 сообщений в хронологическом порядке",
              str([h["msg_id"] for h in hist]))
        check(hist[0]["is_service"] == 1, "db.history: service-сообщение помечено")
        gen_hist = db.history(-100111, topic_id=1)
        check([h["msg_id"] for h in gen_hist] == [15],
              "db.history(topic=General): только сообщение без темы",
              str([h["msg_id"] for h in gen_hist]))
        since = int(time.time()) - 3600
        check([h["msg_id"] for h in db.history(-100111, topic_id=5, since_ts=since)] == [39],
              "db.history(since_ts): окно по времени работает")
        check(all(h.get("raw_json") is None for h in hist),
              "db.history: raw_json не отдаётся наружу")

        # полнотекстовый поиск по истории (в т.ч. пост двухлетней давности)
        fts = db.search_fts("новость")
        check(fts["engine"] == "fts5", "search_fts: работает через FTS5", fts["engine"])
        old = db.search_fts("двухлетней")
        check(old["count"] == 1 and old["hits"][0]["chat_id"] == -100333,
              "search_fts: находит пост двухлетней давности в канале",
              str([(h["chat_id"], h["msg_id"]) for h in old["hits"]]))
        scoped = db.search_fts("пост номер", chat_id=-100444, limit=100)
        check(scoped["count"] == 7, "search_fts: фильтр по chat_id работает",
              f"count={scoped['count']}")

        # LIKE-фолбэк при недоступном FTS5 отдаёт тот же контракт
        db.fts = False
        fb = db.search_fts("двухлетней")
        check(fb["engine"] == "like" and fb["count"] == 1
              and fb["hits"][0]["chat_id"] == -100333,
              "search_fts: LIKE-фолбэк возвращает те же engine/count/hits",
              f"{fb.get('engine')} {fb.get('count')}")
        check("_fallback" not in fb, "search_fts: без служебного поля _fallback")
        db.fts = True

        st = db.stats(-100111)
        check(st["total_messages"] == 10, "db.stats: всего 10 сообщений в форум-группе",
              f"total={st['total_messages']}")
        check(st["by_topic"][0]["topic_id"] == 5 and st["by_topic"][0]["n"] == 6,
              "db.stats: самая активная тема — «Релизы»", str(st["by_topic"][0]))
        check(any(m["kind"] == "photo" for m in st["by_media"]), "db.stats: медиа посчитано")

        # redact: секреты маскированы в выдаче
        sec = [h for h in db.history(-100222) if h["msg_id"] == 37][0]
        check("ghp_" not in sec["text"] and "[скрыто: github-token]" in sec["text"],
              "redact: токен в выдаче замаскирован", repr(sec["text"][:70]))

        # маскировка на инджесте: сырого токена нет нигде в базе
        raw = db.conn.execute(
            "SELECT text, raw_json FROM messages WHERE chat_id=-100222 AND msg_id=37").fetchone()
        check(raw is not None and "ghp_" not in (raw["text"] or "")
              and "ghp_" not in (raw["raw_json"] or ""),
              "ingest: токен не доезжает до messages.text/raw_json")
        check("[скрыто: github-token]" in (raw["text"] or ""),
              "ingest: текст в базе уже замаскирован", repr((raw["text"] or "")[:60]))
        n_refs = db.conn.execute(
            "SELECT COUNT(*) c FROM refs WHERE value LIKE '%ghp_%'").fetchone()["c"]
        check(n_refs == 0, "ingest: refs не содержат сырой токен", str(n_refs))
        like_hits = db.search("токен", chat_id=-100222)
        check(like_hits and all("ghp_" not in (h.get("text") or "") for h in like_hits),
              "search(LIKE-фолбэк): текст уже замаскирован в базе",
              str(len(like_hits)))

        # топ по реакциям
        tr = st["top_reactions"]
        check(bool(tr) and tr[0]["msg_id"] == 38 and tr[0]["score"] == 9,
              "stats.top_reactions: пост с реакциями на первом месте",
              str(tr[0] if tr else None))

        # папки из Telegram легли в индекс
        fl = db.folders_list()
        ai = next((f for f in fl if f["title"] == "AI Agent"), None)
        check(ai is not None and {-100111, -100333} <= set(ai["chat_ids"]),
              "folders: папка AI Agent с составом чатов", str(fl))

        # sanitize: невидимые/bidi-символы вычищены из выдачи
        hid = [h for h in db.history(-100222) if h["msg_id"] == 36][0]
        check(hid["text"] == "текст с невидимками",
              "sanitize: zero-width и bidi-символы удалены", repr(hid["text"]))

        # refs: упоминания/инвайты/url/форварды посчитаны
        refs = db.refs_summary(chat_id=-100222)
        check(refs.get("mention", [{}])[0].get("value") == "durov",
              "refs: @упоминание извлечено", str(refs.get("mention")))
        check(refs.get("invite", [{}])[0].get("value") == "+secret42",
              "refs: приватный инвайт извлечён", str(refs.get("invite")))
        check(refs.get("url", [{}])[0].get("value") == "https://example.com/guide",
              "refs: url извлечён", str(refs.get("url")))
        check(refs.get("forward", [{}])[0].get("value") == "777",
              "refs: канал-источник пересланного", str(refs.get("forward")))

        # t.me/служебные-пути не должны превращаться в mention
        from db import extract_refs
        rr = extract_refs("инвайт t.me/joinchat/Ab12, шер t.me/share/xyz, "
                          "стикеры t.me/addstickers/Cat, юзер t.me/realuser")
        mentions = {v for k, v in rr if k == "mention"}
        check(mentions == {"realuser"}, "refs: служебные пути t.me не попадают в mention",
              str(mentions))
        check(("invite", "joinchat/Ab12") in set(rr), "refs: joinchat остаётся инвайтом")

        # пины: закреп лёг в таблицу с темой и текстом
        pins = db.pins(-100222)
        check(len(pins) == 1 and pins[0]["msg_id"] == 33 and pins[0]["text"] == "закреп: правила чтения",
              "pins: закреплённое сообщение сохранено", str([(p['msg_id'], p['text']) for p in pins]))

        ok = run_sql(db, "SELECT topic_id, COUNT(*) n FROM messages WHERE chat_id=-100111 "
                         "GROUP BY topic_id ORDER BY n DESC")
        check(ok["ok"] and ok["rows"][0]["n"] == 6 and ok["columns"] == ["topic_id", "n"],
              "run_sql: SELECT работает", str(ok["rows"]))
        for bad in ["DELETE FROM messages", "PRAGMA table_info(messages)",
                    "SELECT 1; DROP TABLE messages", "UPDATE messages SET text='x'",
                    "ATTACH DATABASE 'probe_att.sqlite' AS p"]:
            res = run_sql(db, bad)
            check(res["ok"] is False, f"run_sql: заблокировано {bad[:28]!r}",
                  res.get("error", ""))

        # keyword-гейт не должен ловить слова внутри строковых литералов
        fp = run_sql(db, "SELECT COUNT(*) n FROM messages WHERE text LIKE '%update%'")
        check(fp["ok"] is True and fp["rows"][0]["n"] == 0,
              "run_sql: слово в LIKE-литерале не блокируется", fp.get("error", ""))

        neg = run_sql(db, "SELECT msg_id FROM messages WHERE chat_id=-100333", limit=-1)
        check(neg["ok"] and neg["row_count"] == 1,
              "run_sql: отрицательный limit зажимается", str(neg.get("row_count")))

        # сырой текст legacy-строк маскируется при выдаче
        db.conn.execute(
            "INSERT OR IGNORE INTO messages(chat_id, msg_id, topic_id, ts, text) "
            "VALUES(-100222, 999, 1, 1, 'legacy ghp_" + "c" * 40 + "')")
        db.conn.commit()
        leg = run_sql(db, "SELECT text FROM messages WHERE chat_id=-100222 AND msg_id=999")
        check(leg["ok"] and "ghp_" not in leg["rows"][0]["text"],
              "run_sql: выдача маскирует сырой текст", repr(leg["rows"][0]["text"][:50]))
        db.fts = False
        fbm = db.search_fts("legacy", chat_id=-100222)
        check(fbm["count"] == 1 and "ghp_" not in (fbm["hits"][0].get("text") or ""),
              "search_fts: LIKE-фолбэк тоже маскирует выдачу",
              repr((fbm["hits"][0].get("text") or "")[:50]))
        db.fts = True
        rid = db.conn.execute(
            "SELECT rowid FROM messages WHERE chat_id=-100222 AND msg_id=999").fetchone()["rowid"]
        db.conn.execute("DELETE FROM messages WHERE chat_id=-100222 AND msg_id=999")
        db.conn.commit()
        fts_left = db.conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE rowid=?", (rid,)).fetchone()[0]
        check(fts_left == 0, "fts: удаление сообщения чистит индекс (триггёр trg_msg_ad)",
              str(fts_left))


# --------------------------------------------------- 4. watch-лист

def test_watch(db_path: Path) -> None:
    with DB(db_path) as db:
        db.watch_add(-100111, 5, alias="Релизы команды", pinned=True)
        db.watch_add(-100111, 1, alias="Общий чат")
        db.watch_add(-100333, 0, alias="AI-новости")

        wl = db.watch_list()
        check(len(wl) == 3, "watch_list: 3 записи", str(len(wl)))
        check(wl[0]["alias"] == "Релизы команды" and wl[0]["topic_title"] == "Релизы",
              "watch_list: pinned-запись первой, алиас и название темы на месте",
              f"{wl[0]['alias']} / {wl[0]['topic_title']}")
        check(wl[0]["total"] == 6 and wl[0]["new_since_read"] == 6,
              "watch_list: счётчики всего/нового", f"{wl[0]['total']}/{wl[0]['new_since_read']}")

        # регрессия: канал без тем (topic 0) должен видеть свои сообщения,
        # раньше они писались с topic_id=NULL и watch показывал «всего 0»
        news_w = next(w for w in wl if w["chat_id"] == -100333)
        check(news_w["total"] == 3,
              "watch_list: канал без тем (topic 0) видит свои 3 сообщения",
              f"total={news_w['total']}")
        news_new = db.watch_new(-100333, 0, limit=10)
        check(news_new.get("pending_total") == 3 and len(news_new.get("messages", [])) == 3,
              "watch_new для канала без тем работает", str(news_new.get("pending_total")))

        new = db.watch_new(-100111, 5, limit=4)
        check(new["returned"] == 4 and new["pending_total"] == 6 and new["truncated"] is True,
              "watch_new: отдаёт лимит и честно говорит, что осталось",
              f"{new['returned']}/{new['pending_total']}")
        check(new["max_id"] == 13, "watch_new: max_id для отметки", str(new["max_id"]))

        check(db.watch_mark_read(-100111, 5, 13) is True, "watch_mark_read: отметка поставлена")
        after = db.watch_new(-100111, 5, limit=10)
        check([m["msg_id"] for m in after["messages"]] == [16, 39],
              "watch_new после отметки: только действительно новое",
              str([m["msg_id"] for m in after["messages"]]))
        check(db.watch_new(-100111, 6)["error"].startswith("тема 6"),
              "watch_new: тема вне watch-листа отклоняется")

        check(db.watch_remove(-100111, 1) is True, "watch_remove: запись убрана")
        check(len(db.watch_list()) == 2, "watch_list: осталось 2 записи")

        # watched_only: синхронизация только отмеченного
        client = build_fixture()
        rep = asyncio.run(sync_mod.run(db_path=str(db_path), client=client, watched_only=True))
        watched_ids = {r["chat_id"] for r in rep["chats"]}
        check(watched_ids == {-100111, -100333},
              "sync --watched: только чаты из watch-листа", str(watched_ids))


def test_entities_schema(db_path: Path) -> None:
    print("=== entities: схема, watch.project, read-хелперы ===")
    ent_db = db_path.parent / "entities_schema.sqlite"
    if ent_db.exists():
        ent_db.unlink()
    now = int(time.time())
    with DB(ent_db) as db:
        want = {"entities", "entity_events", "extraction_runs"}
        names = {r["name"] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check(want <= names, "entities: три таблицы в схеме", str(sorted(want - names)))
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(watch)")}
        check("project" in cols, "watch: колонка project в схеме", str(sorted(cols)))

        db.watch_add(-100555, 1, alias="прораб", project="стройка")
        w = next(x for x in db.watch_list() if x["chat_id"] == -100555)
        check(w.get("project") == "стройка",
              "watch_add: project round-trip через watch_list", str(w.get("project")))

        db.conn.execute(
            "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, status, project, due_ts, created_ts, updated_ts) "
            "VALUES(1, 'demo', -100555, 1, 'task', 'Закрыть контур', 'open', 'стройка', ?, ?, ?)",
            (now - 86400, now - 100, now - 10))
        db.conn.execute(
            "INSERT INTO entity_events(entity_id, ts, op) VALUES(1, ?, 'create')", (now - 100,))
        db.conn.execute(
            "INSERT INTO entity_events(entity_id, ts, op, field, to_val) VALUES(1, ?, 'update', 'owner', 'ИП Смирнов')",
            (now,))
        db.conn.commit()
        e = db.entity_get(1)
        check(e is not None and e["title"] == "Закрыть контур", "entity_get: сущность найдена")
        evs = (e or {}).get("events", [])
        check(len(evs) == 2 and evs[0]["op"] == "create" and evs[1]["op"] == "update",
              "entity_get: события упорядочены по ts", str([x.get("op") for x in evs]))

        check(len(db.entities_list(kind="task")) == 1, "entities_list: фильтр kind")
        check(db.entities_list(kind="risk") == [], "entities_list: чужой kind пуст")
        check(len(db.entities_list(status="open", project="стройка")) == 1,
              "entities_list: фильтры status+project")
        check(db.entities_list(project="монтаж") == [], "entities_list: чужой project пуст")

        db.conn.execute(
            "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, status, due_ts, created_ts, updated_ts, closed_ts) "
            "VALUES(2, 'demo', -100555, 1, 'task', 'Сделано', 'done', ?, ?, ?, ?)",
            (now - 86400, now - 200, now - 50, now - 50))
        db.conn.execute(
            "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, status, due_ts, created_ts, updated_ts) "
            "VALUES(3, 'demo', -100555, 1, 'task', 'На неделе', 'open', ?, ?, ?)",
            (now + 3 * 86400, now - 200, now - 50))
        db.conn.commit()
        od = db.entity_overdue()
        check([x["id"] for x in od] == [1],
              "entity_overdue: только просроченная открытая", str([x["id"] for x in od]))
    with DB(ent_db) as db:
        n = len(db.entities_list())
        check(n == 3, "entities: повторное открытие БД идемпотентно", str(n))
        db.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "closed_ts, created_ts, updated_ts) "
            "VALUES('demo', -100555, 1, 'task', 'Старая закрытая', 'open', 1, 1, 1)")
        db.conn.commit()
    with DB(ent_db) as db:
        st = db.conn.execute(
            "SELECT status FROM entities WHERE title='Старая закрытая'").fetchone()["status"]
        check(st == "closed", "entities: бэкфилл status для закрытых до фикса", st)


def test_extract_stub(db_path: Path) -> None:
    print("=== extract: stub-провайдер, курсор, dry-run ===")
    ex_db = db_path.parent / "extract_stub.sqlite"
    if ex_db.exists():
        ex_db.unlink()
    now = int(time.time())
    with DB(ex_db) as db:
        db.upsert_chat(chat_id=-100777, title="Стройка", username=None,
                       kind="supergroup", is_forum=True, members=5)
        db.upsert_topic(-100777, 42, "Стройка")
        db.watch_add(-100777, 42, project="стройка")
        msgs = [
            (101, 42, "решено: пол бетон М300"),
            (102, 42, "задача: закрыть контур у Alexey до 01.02.2027"),
            (103, 42, "вопрос: когда привезут арматуру"),
            (104, 42, "обещаю прислать смету"),
            (105, 42, "привет, как дела?"),
            (201, 43, "решено: это в другой, непросматриваемой теме"),
        ]
        for mid, tid, text in msgs:
            ts = now - 600 + mid
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100777, ?, ?, ?, ?, 1, 'Иван', 0, ?)",
                (mid, tid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
        db.conn.commit()

    import extract as extract_mod

    made = extract_mod.run(str(ex_db))
    with DB(ex_db) as db:
        ents = db.entities_list(pack="construction")
        kinds = sorted(e["kind"] for e in ents)
        check(len(ents) == 4 and kinds == ["decision", "promise", "question", "task"],
              "extract: 4 сущности с видами decision/task/question/promise", str(kinds))
        task = next((e for e in ents if e["kind"] == "task"), None)
        check(task is not None and task["owner"] == "Alexey" and task["due_ts"] is not None
              and task["project"] == "стройка",
              "extract: задача с owner, due_ts и проектом",
              f"owner={task and task['owner']} due_ts={task and task['due_ts']} "
              f"project={task and task['project']}")
        evs = db.conn.execute(
            "SELECT COUNT(*) c FROM entity_events WHERE op='new'").fetchone()["c"]
        check(evs == 4, "extract: 4 события new", str(evs))
        leak = db.conn.execute(
            "SELECT COUNT(*) c FROM entity_events WHERE msg_id=201").fetchone()["c"]
        check(leak == 0, "extract: непросматриваемая тема не извлечена", str(leak))
        run = db.conn.execute("SELECT * FROM extraction_runs").fetchone()
        check(run is not None and run["ops_applied"] == 4 and run["status"] == "ok",
              "extract: прогон записан, 4 применённых, статус ok",
              f"applied={run and run['ops_applied']} status={run and run['status']}")
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100777:42:construction'").fetchone()
        check(cur is not None and int(cur["value"]) == 105,
              "extract: курсор = max msg_id темы 42", str(cur and cur["value"]))

    made2 = extract_mod.run(str(ex_db))
    with DB(ex_db) as db:
        n2 = len(db.entities_list(pack="construction"))
        runs2 = db.conn.execute("SELECT COUNT(*) c FROM extraction_runs").fetchone()["c"]
    check(n2 == 4 and runs2 == 1 and made2 == [],
          "extract: повторный прогон ничего не добавляет",
          f"ents={n2} runs={runs2} made={made2}")

    with DB(ex_db) as db:
        db.conn.execute("DELETE FROM meta WHERE key='extract:-100777:42:construction'")
        db.conn.commit()
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        made3 = extract_mod.run(str(ex_db), dry_run=True)
    with DB(ex_db) as db:
        n3 = len(db.entities_list(pack="construction"))
        runs3 = db.conn.execute("SELECT COUNT(*) c FROM extraction_runs").fetchone()["c"]
        cur3 = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100777:42:construction'").fetchone()
    check(n3 == 4 and runs3 == 1 and cur3 is None and bool(made3) and buf.getvalue().strip(),
          "extract: dry-run печатает операции, но ничего не пишет",
          f"ents={n3} runs={runs3} cursor={cur3 and cur3['value']}")

    try:
        extract_mod.run(str(ex_db), pack_id="bogus")
        check(False, "extract: неизвестный пакет поднимает SystemExit", "нет исключения")
    except SystemExit as exc:
        check(str(exc) == "неизвестный пакет: bogus",
              "extract: неизвестный пакет поднимает SystemExit", str(exc))
    except KeyError as exc:
        check(False, "extract: неизвестный пакет поднимает SystemExit", f"KeyError: {exc}")

    with DB(ex_db) as db:
        db.upsert_topic(-100777, 44, "Баня")
        db.watch_add(-100777, 44, project="баня")
        for mid, text in ((301, "решено: баня по плану"), (302, "привет")):
            ts = now - 600 + mid
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100777, ?, 44, ?, ?, 1, 'Иван', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
        db.conn.commit()
    from packs import PACKS as _PACKS
    banya = dict(_PACKS["construction"])
    banya["id"] = "banya"
    banya["scope_filter"] = {"project_regex": "^баня$"}
    made_n = extract_mod.run(str(ex_db), packs=[banya])
    check(len(made_n) == 1 and made_n[0]["topic_id"] == 44 and made_n[0]["pack"] == "banya",
          "extract: кастомный пакет обрабатывает только свой scope",
          str([(m["pack"], m["topic_id"]) for m in made_n]))
    with DB(ex_db) as db:
        cur44 = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100777:44:banya'").fetchone()
        cur42 = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100777:42:banya'").fetchone()
        ents_b = db.entities_list(pack="banya")
    check(cur44 is not None and cur42 is None,
          "extract: чужой scope не тронут (курсор только у своего)",
          f"cur44={cur44 and cur44['value']} cur42={cur42 and cur42['value']}")
    check(len(ents_b) == 1 and ents_b[0]["project"] == "баня",
          "extract: сущность кастомного пакета наследует проект темы",
          str([(e["kind"], e["project"]) for e in ents_b]))


def test_extract_validation(db_path: Path) -> None:
    print("=== extract: семантика операций, валидация, ретраи провайдера ===")
    ex_db = db_path.parent / "extract_validation.sqlite"
    if ex_db.exists():
        ex_db.unlink()
    now = int(time.time())
    with DB(ex_db) as db:
        db.upsert_chat(chat_id=-100888, title="Объект", username=None,
                       kind="supergroup", is_forum=True, members=6)
        db.upsert_topic(-100888, 7, "Объект")
        db.watch_add(-100888, 7, project="стройка")
        texts = {
            11: "Приняли фундамент, заливка в пятницу",
            12: "Смена прораба на объекте согласована",
            13: "Объект сдан, работы завершены",
            14: "Поставка арматуры и сетки подтверждена поставщиком",
            15: "Контроль качества пройден без замечаний",
        }
        for mid, text in texts.items():
            ts = now - 600 + mid
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100888, ?, 7, ?, ?, 1, 'Иван', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
        db.conn.commit()

    import extract as extract_mod
    from packs import PACKS
    pack = PACKS["construction"]

    def result(ops, covered=11):
        return {"ops": ops, "covered_upto_msg_id": covered}

    def ev(msg, quote, chat=-100888):
        return {"chat_id": chat, "msg_id": msg, "quote": quote}

    def op_new(title, msg, quote, owner=None, body=None, dedup_hint=None):
        op = {"op": "new", "kind": "task", "title": title, "entity_id": None,
              "body": body, "owner": owner, "due_text": None, "due_ts": None,
              "confidence": 0.9, "evidence": [ev(msg, quote)]}
        if dedup_hint is not None:
            op["dedup_hint"] = dedup_hint
        return op

    def op_update(entity_id, msg, quote, owner=None, body=""):
        return {"op": "update", "kind": "task", "title": "Залить фундамент",
                "entity_id": entity_id, "body": body, "owner": owner,
                "due_text": None, "due_ts": None, "confidence": 0.9,
                "evidence": [ev(msg, quote)]}

    def op_flip(name, entity_id, msg, quote):
        return {"op": name, "kind": "task", "title": "Залить фундамент",
                "entity_id": entity_id, "body": "", "owner": None,
                "due_text": None, "due_ts": None, "confidence": 0.9,
                "evidence": [ev(msg, quote)]}

    def provider(ops, covered=11):
        return lambda prompt, p: result(ops, covered)

    def last_run(db):
        return db.conn.execute(
            "SELECT * FROM extraction_runs WHERE pack='construction' "
            "ORDER BY id DESC LIMIT 1").fetchone()

    extract_mod.run(str(ex_db), provider=provider([
        op_new("Залить фундамент", 11, "фундамент", owner="Иван", body="Заливка запланирована"),
        op_new("Галлюцинация", 999, "нет такого сообщения"),
    ]))
    with DB(ex_db) as db:
        run = last_run(db)
        check(run is not None and run["ops_applied"] == 1 and run["ops_rejected"] == 1
              and run["status"] == "partial",
              "extract: (a) 1 применён, 1 отклонён, статус partial",
              f"applied={run and run['ops_applied']} rejected={run and run['ops_rejected']} "
              f"status={run and run['status']}")
        n = db.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        check(n == 1, "extract: (a) создана ровно одна сущность", str(n))

    upd = op_update(1, 12, "прораба", owner="Мария", body="Прораб назначен")
    extract_mod.run(str(ex_db), provider=provider([upd]))
    with DB(ex_db) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        check(e["owner"] == "Мария", "extract: (b) owner обновлён", str(e["owner"]))
        check(e["body"] == "Заливка запланирована\nПрораб назначен",
              "extract: (b) body дополнен по политике append", repr(e["body"]))
        evs = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=1 AND op='update' ORDER BY id").fetchall()
        check(sorted(r["field"] for r in evs) == ["body", "owner"],
              "extract: (b) по одному событию на изменённое поле",
              str([r["field"] for r in evs]))
        own = next((r for r in evs if r["field"] == "owner"), None)
        check(own is not None and own["from_val"] == "Иван" and own["to_val"] == "Мария"
              and own["msg_id"] == 12 and own["quote"] == "прораба",
              "extract: (b) событие owner с from/to и доказательством",
              f"from={own and own['from_val']} to={own and own['to_val']}")
        upd_ts = e["updated_ts"]

    time.sleep(1.1)
    extract_mod.run(str(ex_db), provider=provider([upd]))
    with DB(ex_db) as db:
        evs = db.conn.execute(
            "SELECT COUNT(*) c FROM entity_events WHERE entity_id=1").fetchone()["c"]
        check(evs == 3, "extract: (b) повторный update без изменений не пишет события", str(evs))
        e = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        check(e["updated_ts"] == upd_ts, "extract: (b) no-op не трогает updated_ts",
              f"{e['updated_ts']} vs {upd_ts}")
        run = last_run(db)
        check(run["ops_applied"] == 1 and run["ops_rejected"] == 0,
              "extract: (b) no-op update всё равно считается применённым",
              f"applied={run['ops_applied']} rejected={run['ops_rejected']}")

    extract_mod.run(str(ex_db), provider=provider([
        op_update(999, 14, "арматуры", owner="Пётр"),
        op_update(1, 999, "вне окна"),
        op_update(1, 14, "арматуры   и сетки", owner="Пётр"),
    ]))
    with DB(ex_db) as db:
        run = last_run(db)
        check(run["ops_applied"] == 1 and run["ops_rejected"] == 2 and run["status"] == "partial",
              "extract: (c) несуществующий entity_id и evidence вне окна отклонены",
              f"applied={run['ops_applied']} rejected={run['ops_rejected']}")
        e = db.conn.execute("SELECT owner FROM entities WHERE id=1").fetchone()
        check(e["owner"] == "Пётр",
              "extract: (c) quote с лишними пробелами принят, owner обновлён", str(e["owner"]))

    extract_mod.run(str(ex_db), provider=provider([
        {"op": "update", "kind": "task", "title": "", "entity_id": 1,
         "body": "", "owner": None, "due_text": None, "due_ts": None,
         "confidence": None, "evidence": [ev(15, "без замечаний")]},
    ]))
    with DB(ex_db) as db:
        e = db.conn.execute(
            "SELECT owner, title, confidence FROM entities WHERE id=1").fetchone()
        check(e["owner"] == "Пётр" and e["title"] == "Залить фундамент"
              and e["confidence"] == 0.9,
              "extract: (c) update с null/пустыми полями не затирает существующие",
              f"owner={e['owner']} title={e['title']} confidence={e['confidence']}")

    with DB(ex_db) as db:
        db.conn.execute(
            "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
            "sender_id, sender_name, is_service, text) "
            "VALUES(-100700, 14, 7, ?, ?, 1, 'Иван', 0, 'секретная фраза чужого чата')",
            (now - 586, datetime.fromtimestamp(now - 586, timezone.utc).isoformat()))
        db.conn.commit()
    extract_mod.run(str(ex_db), provider=provider([
        {"op": "update", "kind": "task", "title": "Залить фундамент",
         "entity_id": 1, "body": "", "owner": "Олег", "due_text": None,
         "due_ts": None, "confidence": 0.9,
         "evidence": [ev(14, "секретная фраза", chat=-100700)]}]))
    with DB(ex_db) as db:
        run = last_run(db)
        check(run is not None and run["ops_applied"] == 0 and run["ops_rejected"] == 1,
              "extract: (c) evidence из чужого чата отклонено",
              f"applied={run and run['ops_applied']} rejected={run and run['ops_rejected']}")
        e = db.conn.execute("SELECT owner FROM entities WHERE id=1").fetchone()
        check(e["owner"] == "Пётр",
              "extract: (c) чужой чат в evidence не меняет сущность", str(e["owner"]))

    extract_mod.run(str(ex_db), provider=provider([op_flip("close", 1, 13, "сдан")]))
    with DB(ex_db) as db:
        e = db.conn.execute("SELECT closed_ts FROM entities WHERE id=1").fetchone()
        check(e["closed_ts"] is not None, "extract: (d) close выставил closed_ts",
              str(e["closed_ts"]))
        evc = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=1 AND op='close'").fetchone()
        check(evc is not None and evc["msg_id"] == 13,
              "extract: (d) одно событие close с доказательством",
              f"msg_id={evc and evc['msg_id']}")
        open_ids = [e["id"] for e in db.entities_list(status="open")]
        closed_ids = [e["id"] for e in db.entities_list(status="closed")]
        check(1 not in open_ids and 1 in closed_ids,
              "extract: (d) close перевёл status в closed",
              f"open={open_ids} closed={closed_ids}")

    extract_mod.run(str(ex_db), provider=provider([op_update(1, 12, "прораба", owner="Мария")]))
    with DB(ex_db) as db:
        run = last_run(db)
        check(run["ops_applied"] == 0 and run["ops_rejected"] == 1,
              "extract: (d) update по закрытой сущности отклонён",
              f"applied={run['ops_applied']} rejected={run['ops_rejected']}")
        e = db.conn.execute("SELECT owner FROM entities WHERE id=1").fetchone()
        check(e["owner"] == "Пётр", "extract: (d) закрытая сущность не изменена", str(e["owner"]))

    extract_mod.run(str(ex_db), provider=provider([op_flip("reopen", 1, 13, "сдан")]))
    with DB(ex_db) as db:
        e = db.conn.execute("SELECT closed_ts FROM entities WHERE id=1").fetchone()
        check(e["closed_ts"] is None, "extract: (d) reopen сбросил closed_ts", str(e["closed_ts"]))
        evr = db.conn.execute(
            "SELECT COUNT(*) c FROM entity_events WHERE entity_id=1 AND op='reopen'").fetchone()["c"]
        check(evr == 1, "extract: (d) одно событие reopen", str(evr))
        open_ids = [e["id"] for e in db.entities_list(status="open")]
        check(1 in open_ids, "extract: (d) reopen вернул status=open", str(open_ids))

    ops55 = ([op_new(f"Задача {i}", 14, "арматуры") for i in range(30)]
             + [op_new(f"Задача {i}", 15, "качества") for i in range(30, 55)])
    extract_mod.run(str(ex_db), provider=provider(ops55, covered=15))
    with DB(ex_db) as db:
        run = last_run(db)
        check(run["ops_applied"] == 50 and run["ops_rejected"] == 0
              and run["ops_overflow"] == 5 and run["status"] == "partial",
              "extract: (e) 55 валидных new: 50 применено, 5 — overflow, не rejected",
              f"applied={run and run['ops_applied']} rejected={run and run['ops_rejected']} "
              f"overflow={run and run['ops_overflow']}")
        n = db.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        check(n == 51, "extract: (e) сущностей 1+50", str(n))
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100888:7:construction'"
        ).fetchone()
        check(int(cur["value"]) == 14,
              "extract: (e) курсор остановлен до первой overflow-операции (msg 15)",
              str(cur and cur["value"]))

    extract_mod.run(str(ex_db), provider=provider(ops55, covered=15))
    with DB(ex_db) as db:
        n = db.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        run = last_run(db)
        check(n == 56 and run["ops_overflow"] == 0,
              "extract: (e) следующий прогон дочитал overflow: все 55 сущностей на месте",
              f"entities={n} overflow={run['ops_overflow']}")

    real_post = extract_mod._post
    saved = {k: os.environ.get(k) for k in ("EXTRACT_URL", "EXTRACT_KEY", "EXTRACT_MODEL")}
    os.environ["EXTRACT_URL"] = "http://stub.invalid/v1"
    os.environ.pop("EXTRACT_KEY", None)
    os.environ.pop("EXTRACT_MODEL", None)
    calls: list[dict] = []
    try:
        def fake_post(url, key, payload):
            calls.append(payload)
            if len(calls) == 1:
                return {"choices": [{"message": {"content": "это не JSON"},
                                     "finish_reason": "stop"}]}
            return {"choices": [{"message": {"content": json.dumps(
                {"ops": [], "covered_upto_msg_id": 11})}, "finish_reason": "stop"}]}

        extract_mod._post = fake_post
        res = extract_mod.provider_openai("промпт", pack)
        check(isinstance(res, dict) and res["ops"] == [] and len(calls) == 2,
              "extract: (g) после мусора провайдер вернул dict за 2 вызова",
              f"calls={len(calls)}")
        p0 = calls[0]
        check(p0["model"] == "gpt-4o-mini" and p0["temperature"] == 0.1
              and p0["response_format"]["json_schema"]["name"] == "entity_ops"
              and p0["response_format"]["json_schema"]["strict"] is True
              and p0["response_format"]["json_schema"]["schema"] == pack["json_schema"],
              "extract: (g) payload: модель, температура, strict json_schema пакета",
              f"model={p0['model']} temp={p0['temperature']}")
        msgs = calls[1]["messages"]
        check(msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
              and msgs[1]["content"] == "промпт",
              "extract: (g) системное сообщение отделено от данных",
              f"roles={[m['role'] for m in msgs]}")
        check(len(msgs) == 4 and msgs[2]["role"] == "assistant"
              and msgs[2]["content"] == "это не JSON" and msgs[3]["role"] == "user"
              and msgs[3]["content"].startswith("Your output failed:"),
              "extract: (g) ретрай дополнен ответом модели и фидбеком об ошибке",
              f"roles={[m['role'] for m in msgs]}")

        def fail_post(url, key, payload):
            calls.append(payload)
            raise OSError("сеть недоступна")

        extract_mod._post = fail_post
        try:
            extract_mod.provider_openai("промпт", pack)
            check(False, "extract: (g) вечный сбой провайдера поднимает исключение")
        except RuntimeError:
            check(True, "extract: (g) вечный сбой провайдера поднимает исключение")
        check(len(calls) == 5, "extract: (g) три попытки до исключения", str(len(calls)))

        with DB(ex_db) as db:
            runs_before = db.conn.execute(
                "SELECT COUNT(*) c FROM extraction_runs").fetchone()["c"]
            cur_before = db.conn.execute(
                "SELECT value FROM meta WHERE key='extract:-100888:7:construction'").fetchone()
        extract_mod.run(str(ex_db), provider=extract_mod.provider_openai)
        with DB(ex_db) as db:
            runs_after = db.conn.execute(
                "SELECT COUNT(*) c FROM extraction_runs").fetchone()["c"]
            cur_after = db.conn.execute(
                "SELECT value FROM meta WHERE key='extract:-100888:7:construction'").fetchone()
        check(runs_after == runs_before and cur_before is not None and cur_after is not None
              and cur_after["value"] == cur_before["value"],
              "extract: (g) сбой провайдера: курсор не двинут, прогон не записан",
              f"runs {runs_before}->{runs_after} "
              f"cursor {cur_before and cur_before['value']}->{cur_after and cur_after['value']}")
    finally:
        extract_mod._post = real_post
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    with DB(ex_db) as db:
        db.upsert_topic(-100888, 8, "Поток")
        db.watch_add(-100888, 8, project="стройка")
        for i in range(250):
            mid = 300 + i
            ts = now - 300 + i
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100888, ?, 8, ?, ?, 1, 'Иван', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), f"сообщение {i}"))
        db.conn.commit()

    made = extract_mod.run(str(ex_db), provider=extract_mod.provider_stub)
    m8 = next((m for m in made if m["topic_id"] == 8), None)
    check(m8 is not None and m8["messages"] == 200 and m8["upto_msg"] == 499,
          "extract: (h) первый прогон берёт только 200 сообщений",
          f"messages={m8 and m8['messages']} upto={m8 and m8['upto_msg']}")
    with DB(ex_db) as db:
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100888:8:construction'").fetchone()
        check(cur is not None and int(cur["value"]) == 499,
              "extract: (h) курсор = последнему msg_id среза", str(cur and cur["value"]))

    made = extract_mod.run(str(ex_db), provider=extract_mod.provider_stub)
    m8 = next((m for m in made if m["topic_id"] == 8), None)
    check(m8 is not None and m8["messages"] == 50 and m8["upto_msg"] == 549,
          "extract: (h) второй прогон подбирает остаток",
          f"messages={m8 and m8['messages']} upto={m8 and m8['upto_msg']}")

    with DB(ex_db) as db:
        for mid, text in ((16, "Новая поставка на объект"), (17, "Смета обновлена"),
                          (18, "Задача C принята в работу"), (19, "Задача C закрыта по факту"),
                          (20, "Задача C снова открыта"), (21, "Задача C подтверждена"),
                          (22, "Задача C в работе"), (23, "Задача C почти готова")):
            ts = now - 600 + mid
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100888, ?, 7, ?, ?, 1, 'Иван', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
        db.conn.commit()

    extract_mod.run(str(ex_db), provider=lambda prompt, p: result([], 1))
    with DB(ex_db) as db:
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100888:7:construction'").fetchone()
        check(cur is not None and int(cur["value"]) == 15,
              "extract: (i) covered меньше курсора не двигает курсор назад",
              str(cur and cur["value"]))
    extract_mod.run(str(ex_db), provider=lambda prompt, p: result([], 17))
    with DB(ex_db) as db:
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100888:7:construction'").fetchone()
        check(cur is not None and int(cur["value"]) == 17,
              "extract: (i) после клампа курсор двигается вперёд", str(cur and cur["value"]))

    extract_mod.run(str(ex_db), provider=lambda prompt, p: result([op_new("Задача C", 18, "принята")], 18))
    with DB(ex_db) as db:
        row = db.conn.execute(
            "SELECT id FROM entities WHERE title='Задача C' AND closed_ts IS NULL").fetchone()
    check(row is not None, "extract: (j) создана сущность для сценария reopen",
          str(row and row["id"]))
    ent_c = row["id"]
    extract_mod.run(str(ex_db), provider=lambda prompt, p: result([op_flip("close", ent_c, 19, "закрыта")], 19))
    extract_mod.run(str(ex_db), provider=lambda prompt, p: result([op_new("Задача C", 20, "снова")], 20))
    with DB(ex_db) as db:
        n = db.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE title='Задача C'").fetchone()["c"]
        check(n == 2, "extract: (j) тот же kind+title после close создаёт вторую сущность", str(n))
    extract_mod.run(str(ex_db), provider=lambda prompt, p: result([op_flip("reopen", ent_c, 21, "подтверждена")], 21))
    with DB(ex_db) as db:
        run = last_run(db)
        check(run is not None and run["ops_applied"] == 0 and run["ops_rejected"] == 1
              and run["status"] == "partial",
              "extract: (j) reopen при живом дубле отклонён без исключения",
              f"applied={run and run['ops_applied']} rejected={run and run['ops_rejected']}")
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100888:7:construction'").fetchone()
        check(cur is not None and int(cur["value"]) == 21,
              "extract: (j) курсор двигается несмотря на отклонённый reopen",
              str(cur and cur["value"]))
        e = db.conn.execute("SELECT closed_ts FROM entities WHERE id=?", (ent_c,)).fetchone()
        check(e["closed_ts"] is not None, "extract: (j) старая сущность осталась закрытой",
              str(e["closed_ts"]))
    extract_mod.run(str(ex_db), provider=lambda prompt, p: result([], 23))
    with DB(ex_db) as db:
        run = last_run(db)
        check(run is not None and run["ops_rejected"] == 0,
              "extract: (j) следующий прогон здоров", str(dict(run) if run else None))

    with DB(ex_db) as db:
        for mid, text in ((24, "Задача E принята"), (25, "Задача E закрыта"),
                          (26, "Задача E снова в работе"), (27, "переоткрываем старую задачу")):
            ts = now - 600 + mid
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100888, ?, 7, ?, ?, 1, 'Иван', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
        db.conn.commit()
    extract_mod.run(str(ex_db), provider=lambda prompt, p: result([op_new("Задача E", 24, "принята")], 24))
    with DB(ex_db) as db:
        ent_e = db.conn.execute(
            "SELECT id FROM entities WHERE title='Задача E'").fetchone()["id"]
    extract_mod.run(str(ex_db), provider=lambda prompt, p: result([op_flip("close", ent_e, 25, "закрыта")], 25))
    extract_mod.run(str(ex_db), provider=lambda prompt, p: result(
        [op_new("Задача E", 26, "снова"), op_flip("reopen", ent_e, 27, "переоткрываем")], 27))
    with DB(ex_db) as db:
        run = last_run(db)
        check(run is not None and run["ops_applied"] == 2 and run["ops_rejected"] == 0,
              "extract: (j) new+reopen одним батчем не роняет прогон",
              str(dict(run) if run else None))
        n_open = db.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE title='Задача E' "
            "AND closed_ts IS NULL").fetchone()["c"]
        e_closed = db.conn.execute(
            "SELECT closed_ts FROM entities WHERE id=?", (ent_e,)).fetchone()["closed_ts"]
        check(n_open == 1 and e_closed is not None,
              "extract: (j) reopen при внутрибатчевом дубле пропущен, краша нет",
              f"open={n_open} closed_ts={e_closed}")
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100888:7:construction'").fetchone()
        check(cur is not None and int(cur["value"]) == 27,
              "extract: (j) курсор дошёл до конца батча", str(cur and cur["value"]))

    with DB(ex_db) as db:
        db.upsert_topic(-100888, 9, "Бюджет")
        db.watch_add(-100888, 9, project="стройка")
        for i in range(5):
            mid = 600 + i
            ts = now - 300 + i
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100888, ?, 9, ?, ?, 1, 'Иван', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                 "длинное сообщение " + "x" * 300))
        db.conn.commit()

    saved_chars = os.environ.get("EXTRACT_MAX_CHARS")
    os.environ["EXTRACT_MAX_CHARS"] = "800"
    try:
        made = extract_mod.run(str(ex_db), provider=lambda prompt, p: result([], 0))
        m9 = next((m for m in made if m["topic_id"] == 9), None)
        check(m9 is not None and 0 < m9["messages"] < 5,
              "extract: (k) бюджет символов режет хвост батча",
              f"messages={m9 and m9['messages']}")
        kept = m9["messages"]
        with DB(ex_db) as db:
            cur9 = db.conn.execute(
                "SELECT value FROM meta WHERE key='extract:-100888:9:construction'").fetchone()
        check(cur9 is not None and int(cur9["value"]) == 600 + kept - 1,
              "extract: (k) курсор = последнему вошедшему сообщению",
              str(cur9 and cur9["value"]))
    finally:
        if saved_chars is None:
            os.environ.pop("EXTRACT_MAX_CHARS", None)
        else:
            os.environ["EXTRACT_MAX_CHARS"] = saved_chars

    made = extract_mod.run(str(ex_db), provider=lambda prompt, p: result([], 0))
    m9b = next((m for m in made if m["topic_id"] == 9), None)
    check(m9b is not None and m9b["messages"] == 5 - kept,
          "extract: (k) следующий прогон подбирает остаток",
          f"messages={m9b and m9b['messages']} kept={kept}")

    with DB(ex_db) as db:
        for mid, text in ((28, "Снова возвращаемся к фундаменту"),
                          (29, "Акция: триал Acme Cloud на месяц"),
                          (30, "Двойная задача закрыта и переоткрыта")):
            ts = now - 600 + mid
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100888, ?, 7, ?, ?, 1, 'Иван', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
        db.conn.commit()

    extract_mod.run(str(ex_db), provider=provider([op_new("Залить фундамент", 28, "фундаменту")]))
    with DB(ex_db) as db:
        n = db.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        check(n == 60, "extract: (l) дубликат title не создаёт вторую сущность", str(n))
        evd = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=1 AND op='update' AND field='dedup'").fetchone()
        check(evd is not None, "extract: (l) дубликат конвертирован в update/dedup", str(evd))
        run = last_run(db)
        check(run["ops_applied"] == 1 and run["ops_rejected"] == 0,
              "extract: (l) конвертация дубликата считается применённой",
              f"applied={run['ops_applied']}")

    twin = op_new("Двойная задача", 30, "закрыта и переоткрыта")
    extract_mod.run(str(ex_db), provider=provider([twin, dict(twin)]))
    with DB(ex_db) as db:
        n = db.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        check(n == 61, "extract: (l) два одинаковых new в одном батче дают одну сущность", str(n))
        tw_id = db.conn.execute(
            "SELECT id FROM entities WHERE title='Двойная задача'").fetchone()["id"]
        evs = db.conn.execute(
            "SELECT op, field FROM entity_events WHERE entity_id=? ORDER BY id",
            (tw_id,)).fetchall()
        check([(r["op"], r["field"]) for r in evs] == [("new", None), ("update", "dedup")],
              "extract: (l) второй new конвертирован в update первого",
              str([(r["op"], r["field"]) for r in evs]))
        run = last_run(db)
        check(run["ops_applied"] == 2, "extract: (l) обе операции засчитаны", str(run["ops_applied"]))

    extract_mod.run(str(ex_db), provider=provider([
        op_new("Acme Cloud — триал 30 дней", 29, "триал Acme Cloud", dedup_hint="acme.cloud"),
        op_new("Триал 30 дней на Acme Cloud", 29, "триал Acme Cloud", dedup_hint=" Acme.Cloud "),
    ]))
    with DB(ex_db) as db:
        n = db.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        check(n == 62, "extract: (l) одинаковый dedup_hint дедупит разные формулировки", str(n))
        acme = db.conn.execute("SELECT title FROM entities WHERE title LIKE 'Acme%'").fetchone()
        check(acme is not None and acme["title"] == "Acme Cloud — триал 30 дней",
              "extract: (l) выживает первая формулировка", str(acme and acme["title"]))


def test_entities_mcp(db_path: Path) -> None:
    print("=== MCP: сущности — tg_entities / tg_entity / tg_overdue ===")
    ent_db = db_path.parent / "entities_mcp.sqlite"
    if ent_db.exists():
        ent_db.unlink()
    now = int(time.time())
    with DB(ent_db) as db:
        db.upsert_chat(chat_id=-100999, title="Стройка", username=None,
                       kind="supergroup", is_forum=True, members=6)
        db.upsert_topic(-100999, 7, "Объект")
        db.watch_add(-100999, 7, project="стройка")
        texts = {
            21: "Задача: сдать отчёт по объекту",
            22: "Задача: подготовить смету на следующий месяц",
        }
        for mid, text in texts.items():
            ts = now - 600 + mid
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100999, ?, 7, ?, ?, 1, 'Иван', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
        db.conn.commit()

    import extract as extract_mod

    def op_new(title, msg, quote, due_ts):
        return {"op": "new", "kind": "task", "title": title, "entity_id": None,
                "body": None, "owner": "Иван", "due_text": None, "due_ts": due_ts,
                "confidence": 0.9,
                "evidence": [{"chat_id": -100999, "msg_id": msg, "quote": quote}]}

    extract_mod.run(str(ent_db), provider=lambda prompt, pack: {
        "ops": [
            op_new("Сдать отчёт", 21, "сдать отчёт", now - 86400),
            op_new("Подготовить смету", 22, "подготовить смету", now + 30 * 86400),
        ],
        "covered_upto_msg_id": 22,
    })
    with DB(ent_db) as db:
        ids = sorted(r["id"] for r in db.entities_list(project="стройка"))
    check(len(ids) == 2, "entities MCP: сидированы 2 сущности через extract.run",
          str(ids))

    env = dict(os.environ, TG_DB=str(ent_db))
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "0"}}})
        wait_for(proc, lambda m: m.get("id") == 1)
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        def call(tool: str, args: dict, rid: int) -> dict:
            rpc(proc, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}})
            msg = wait_for(proc, lambda m: m.get("id") == rid)
            assert not msg.get("result", {}).get("isError"), msg
            return json.loads(msg["result"]["content"][0]["text"])

        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = wait_for(proc, lambda m: m.get("id") == 2)
        names = {t["name"] for t in tools["result"]["tools"]}
        check({"tg_entities", "tg_entity", "tg_overdue"} <= names,
              "entities MCP: инструменты зарегистрированы", str(sorted(names)))

        ents = call("tg_entities", {"project": "стройка"}, 3)
        check(ents.get("count") == 2
              and all(e.get("project") == "стройка" for e in ents.get("items", [])),
              "MCP tg_entities: обе сущности проекта", str(ents.get("count")))
        check(bool(ents.get("items")) and all(
            k in ents["items"][0] for k in ("kind", "status", "owner", "due_ts")),
              "MCP tg_entities: у каждой сущности kind/status/owner/due_ts",
              str(sorted(ents["items"][0])))
        check("note" in ents and "не инструкции" in ents["note"],
              "MCP tg_entities: пометка «данные, не инструкции»",
              str(ents.get("note"))[:80])

        q = call("tg_entities", {"query": "отчёт"}, 8)
        check(q.get("count") == 1 and q["items"][0]["title"] == "Сдать отчёт",
              "MCP tg_entities: query ищет подстроку в title/body", str(q.get("count")))
        q2 = call("tg_entities", {"query": "которого нет нигде"}, 9)
        check(q2.get("count") == 0,
              "MCP tg_entities: query без совпадений пуст", str(q2.get("count")))

        one = call("tg_entity", {"id": ids[0]}, 4)
        check(one.get("id") == ids[0] and one.get("title") == "Сдать отчёт"
              and isinstance(one.get("events"), list) and len(one["events"]) >= 1,
              "MCP tg_entity: сущность с событиями",
              f"id={one.get('id')} events={len(one.get('events') or [])}")
        miss = call("tg_entity", {"id": 9999}, 5)
        check(miss.get("error") == "not found",
              "MCP tg_entity: неизвестный id — not found", str(miss)[:80])

        od = call("tg_overdue", {}, 6)
        check(od.get("count") == 1
              and [e["title"] for e in od.get("items", [])] == ["Сдать отчёт"],
              "MCP tg_overdue: только просроченная",
              str([e.get("title") for e in od.get("items", [])]))
        check(isinstance(od.get("now_ts"), int),
              "MCP tg_overdue: now_ts в ответе", str(od.get("now_ts")))
        od2 = call("tg_overdue", {"days_ahead": 3650}, 7)
        check(od2.get("count") == 2,
              "MCP tg_overdue(days_ahead=3650): обе сущности", str(od2.get("count")))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_monitor_notify(db_path: Path) -> None:
    print("=== monitor + notify: сводка по проектам и само-уведомление ===")
    mon_db = db_path.parent / "monitor_notify.sqlite"
    if mon_db.exists():
        mon_db.unlink()
    now = int(time.time())
    with DB(mon_db) as db:
        db.upsert_chat(chat_id=-1007001, title="Стройка", username=None,
                       kind="supergroup", is_forum=True, members=3)
        db.upsert_topic(-1007001, 11, "Объект")
        db.watch_add(-1007001, 11, project="стройка")
        db.upsert_chat(chat_id=-1007002, title="Баня", username=None,
                       kind="supergroup", is_forum=True, members=2)
        db.upsert_topic(-1007002, 12, "Печь")
        db.watch_add(-1007002, 12, project="баня")

        def ent(project: str, title: str, due_ts: int, closed_ts: int | None = None) -> None:
            db.conn.execute(
                "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
                "due_ts, project, created_ts, updated_ts, closed_ts) "
                "VALUES('tg', ?, ?, 'task', ?, ?, ?, ?, ?, ?, ?)",
                (-1007001 if project == "стройка" else -1007002,
                 11 if project == "стройка" else 12,
                 title, "closed" if closed_ts else "open", due_ts, project,
                 now - 7200, now - 7200, closed_ts))

        ent("стройка", "Покрасить забор", now - 86400)
        ent("стройка", "Заказать окна", now + 30 * 86400)
        ent("стройка", "Сдать фундамент", now - 2 * 86400, closed_ts=now)
        ent("баня", "Купить дрова", now - 3600)
        db.conn.commit()

    import monitor as monitor_mod
    import notify as notify_mod

    summary = monitor_mod.collect(str(mon_db))
    check(set(summary["projects"]) == {"стройка", "баня"},
          "monitor.collect: оба проекта в сводке", str(sorted(summary["projects"])))
    od_titles = sorted(e["title"] for e in summary["overdue"])
    check(od_titles == ["Купить дрова", "Покрасить забор"],
          "monitor.collect: overdue — ровно 2 открытые просроченные, закрытая отсутствует",
          str(od_titles))

    res = monitor_mod.run(str(mon_db), days_ahead=3650)
    check(isinstance(res.get("overdue"), list) and len(res["overdue"]) == 2,
          "monitor.run(days_ahead=3650): просроченные остались просроченными",
          str(len(res.get("overdue") or [])))
    check(isinstance(res.get("upcoming"), list)
          and [e["title"] for e in res["upcoming"]] == ["Заказать окна"],
          "monitor.run(days_ahead=3650): будущий срок в upcoming",
          str([e.get("title") for e in res.get("upcoming") or []]))
    check(res.get("notify") is None,
          "monitor.run без send: notify не вызывался", str(res.get("notify")))

    lines = monitor_mod.format_report(summary)
    check(bool(lines) and any("стройка" in ln for ln in lines)
          and any("баня" in ln for ln in lines)
          and all(len(ln) <= 300 for ln in lines),
          "monitor.format_report: строки по обоим проектам, <=300 симв.",
          str(len(lines)))

    check(len(summary["news"]) == 4,
          "monitor.collect: news — все сущности моложе 24ч", str(len(summary["news"])))
    stale = monitor_mod.collect(str(mon_db), news_hours=1)
    check(stale["news"] == [],
          "monitor.collect: news_hours=1 отсекает двухчасовые", str(len(stale["news"])))
    check("нового за 24ч: 4" in lines[0],
          "monitor.format_report: шапка считает новое", lines[0][:120])
    check(any("новое:" in ln and "Купить дрова" in ln for ln in lines),
          "monitor.format_report: новое раскрыто списком", str(lines[:6]))

    dry = notify_mod.notify(["x"], dry_run=True)
    check(dry.get("ok") is True and dry.get("dry_run") is True
          and dry.get("planned") == ["x"],
          "notify dry_run: planned без telethon и без сети", str(dry)[:120])

    saved = os.environ.pop("TG_ALLOW_SEND", None)
    try:
        blocked = notify_mod.notify(["x"], dry_run=False)
        check(blocked.get("ok") is False and "TG_ALLOW_SEND" in str(blocked.get("error", "")),
              "notify без TG_ALLOW_SEND: отправка заблокирована", str(blocked)[:120])
    finally:
        if saved is not None:
            os.environ["TG_ALLOW_SEND"] = saved

    srv = (Path(__file__).parent / "server.py").read_text(encoding="utf-8")
    check("notify" not in srv, "server.py не содержит notify (MCP остаётся read-only)")


def test_media(db_path: Path) -> None:
    print("=== media: очередь транскрипций, воркер, экстракция ===")
    md_db = db_path.parent / "media.sqlite"
    if md_db.exists():
        md_db.unlink()
    import live as live_mod
    import media as media_mod
    import extract as extract_mod

    users = {1: User(1, "Иван")}
    group = Channel(-100600, "Голосовая", None, forum=False)
    client = FakeClient({
        -100600: {"entity": group, "messages": [
            Msg(601, NOW - timedelta(hours=1), users[1], None, media=MessageMediaVoice()),
        ], "topics": []},
    })
    asyncio.run(sync_mod.run(db_path=str(md_db), client=client, names=["-100600"]))
    with DB(md_db) as db:
        row = db.conn.execute(
            "SELECT kind, status, text FROM media_assets "
            "WHERE chat_id=-100600 AND msg_id=601").fetchone()
    check(row is not None and row["kind"] == "voice" and row["status"] == "pending"
          and row["text"] is None,
          "media: sync ставит голосовое в очередь (pending)",
          str(dict(row) if row else None))

    vmsg = Msg(602, datetime.fromtimestamp(int(time.time()), timezone.utc), users[1], None,
               media=MessageMediaVoice())
    with DB(md_db) as db:
        ok = live_mod.handle_message(db, vmsg, -100600, False)
        row = db.conn.execute(
            "SELECT status FROM media_assets WHERE chat_id=-100600 AND msg_id=602").fetchone()
    check(ok and row is not None and row["status"] == "pending",
          "media: live ставит голосовое в очередь", str(dict(row) if row else None))

    saved = {k: os.environ.get(k) for k in
             ("MEDIA_PROVIDER", "MEDIA_URL", "MEDIA_KEY", "MEDIA_MODEL")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        res = media_mod.run(str(md_db))
        check(res == {"done": 2, "failed": 0},
              "media: stub-воркер обработал оба голосовых", str(res))
        with DB(md_db) as db:
            row = db.conn.execute(
                "SELECT status, text, engine FROM media_assets "
                "WHERE chat_id=-100600 AND msg_id=601").fetchone()
        check(row is not None and row["status"] == "done" and row["engine"] == "stub"
              and row["text"] == "[транскрипция voice -100600/601]",
              "media: детерминированная stub-транскрипция", str(dict(row) if row else None))

        with DB(md_db) as db:
            media_mod.enqueue(db, -100600, 603, "voice")
            media_mod.enqueue(db, -100600, 603, "voice")
            media_mod.enqueue(db, -100600, 604, "photo")
            n603 = db.conn.execute(
                "SELECT COUNT(*) FROM media_assets WHERE chat_id=-100600 AND msg_id=603"
            ).fetchone()[0]
            n604 = db.conn.execute(
                "SELECT COUNT(*) FROM media_assets WHERE chat_id=-100600 AND msg_id=604"
            ).fetchone()[0]
        check(n603 == 1, "media: повторный enqueue не дублирует строку", str(n603))
        check(n604 == 0, "media: фото в очередь транскрипции не ставится", str(n604))

        def boom(chat_id: int, msg_id: int, kind: str) -> dict:
            raise RuntimeError("провайдер недоступен")

        res2 = media_mod.run(str(md_db), provider=boom)
        check(res2 == {"done": 0, "failed": 1},
              "media: сбой провайдера помечает строку failed", str(res2))
        res3 = media_mod.run(str(md_db))
        check(res3 == {"done": 0, "failed": 0},
              "media: failed-строка не перерабатывается повторно", str(res3))

        vmsg2 = Msg(605, datetime.fromtimestamp(int(time.time()), timezone.utc), users[1],
                    None, media=MessageMediaVoice())
        with DB(md_db) as db:
            live_mod.handle_message(db, vmsg2, -100600, False)
            db.watch_add(-100600, 1, project="стройка")

        def voice_prov(chat_id: int, msg_id: int, kind: str) -> dict:
            return {"text": "решено: переносим срок", "engine": "custom"}

        res4 = media_mod.run(str(md_db), provider=voice_prov)
        check(res4 == {"done": 1, "failed": 0},
              "media: кастомный провайдер расшифровал голосовое", str(res4))

        captured: dict = {}

        def capture_prov(prompt: str, pack: dict) -> dict:
            captured["prompt"] = prompt
            return {"ops": [], "covered_upto_msg_id": 605}

        extract_mod.run(str(md_db), provider=capture_prov)
        check("решено: переносим срок" in captured.get("prompt", ""),
              "media: транскрипция доезжает до промпта экстракции")

        with DB(md_db) as db:
            db.conn.execute("DELETE FROM meta WHERE key='extract:-100600:1:construction'")
            db.conn.commit()
        extract_mod.run(str(md_db), provider=extract_mod.provider_stub)
        with DB(md_db) as db:
            ents = db.entities_list(pack="construction", kind="decision")
        check(len(ents) == 1 and "переносим срок" in ents[0]["title"],
              "media: stub-экстрактор создал decision из транскрипции",
              str([e["title"] for e in ents]))

        vmsg3 = Msg(606, datetime.fromtimestamp(int(time.time()), timezone.utc), users[1],
                    None, media=MessageMediaVoice())
        with DB(md_db) as db:
            live_mod.handle_message(db, vmsg3, -100600, False)
        secret = "sk-" + "a" * 40
        res5 = media_mod.run(str(md_db), provider=lambda c, m, k: {
            "text": f"решено: ключ {secret} утёк", "engine": "custom"})
        check(res5 == {"done": 1, "failed": 0},
              "media: транскрипция с секретом обработана", str(res5))
        with DB(md_db) as db:
            row = db.conn.execute(
                "SELECT text FROM media_assets WHERE chat_id=-100600 AND msg_id=606").fetchone()
        check(row is not None and secret not in (row["text"] or "")
              and "[скрыто:" in (row["text"] or ""),
              "media: секрет в транскрипции маскируется до записи",
              repr(row and (row["text"] or "")[:80]))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_export(db_path: Path) -> None:
    print("=== export: JSONL-провайдер, курсор integrations, стабы 101/notion ===")
    exp_db = db_path.parent / "export.sqlite"
    if exp_db.exists():
        exp_db.unlink()
    out_path = db_path.parent / "export_run.jsonl"
    if out_path.exists():
        out_path.unlink()
    now = int(time.time())
    import export as export_mod
    with DB(exp_db) as db:
        for eid, title in ((1, "Залить фундамент"), (2, "Заказать арматуру"), (3, "Без событий")):
            db.conn.execute(
                "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, created_ts, updated_ts) "
                "VALUES(?, 'construction', -100999, 7, 'task', ?, ?, ?)", (eid, title, now, now))
        for eid, mid in ((1, 11), (2, 12)):
            db.conn.execute(
                "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
                "VALUES(?, 1, ?, 'new', -100999, ?, 'фраза')", (eid, now - 3600, mid))
        db.conn.commit()

    res = export_mod.run(str(exp_db), system="file", out=str(out_path))
    check(res.get("ok") is True and res.get("exported") == 2 and out_path.exists(),
          "export: первый прогон выгружает сущности с событиями, без событий — нет", str(res))
    lines = out_path.read_text(encoding="utf-8").splitlines()
    recs = [json.loads(x) for x in lines]
    check(len(lines) == 2 and all("entity" in r and "events" in r and "exported_ts" in r for r in recs),
          "export: 2 строки JSONL с ключами entity/events/exported_ts", str(len(lines)))
    with DB(exp_db) as db:
        ints = db.conn.execute(
            "SELECT entity_id, synced_ts FROM integrations WHERE system='file' ORDER BY entity_id").fetchall()
    check([r["entity_id"] for r in ints] == [1, 2] and all(r["synced_ts"] for r in ints),
          "export: курсор integrations только у выгруженных", str([dict(r) for r in ints]))

    size1 = out_path.stat().st_size
    res2 = export_mod.run(str(exp_db), system="file", out=str(out_path))
    check(res2.get("ok") is True and res2.get("exported") == 0 and out_path.stat().st_size == size1,
          "export: без новых событий — 0 выгружено, файл не растёт", str(res2))

    with DB(exp_db) as db:
        synced1 = db.conn.execute(
            "SELECT synced_ts FROM integrations WHERE entity_id=1 AND system='file'").fetchone()["synced_ts"]
        db.conn.execute(
            "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
            "VALUES(1, 2, ?, 'update', -100999, 98, 'на границе курсора')", (synced1,))
        db.conn.execute(
            "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
            "VALUES(2, 2, ?, 'update', -100999, 97, 'старое событие')", (synced1 - 100,))
        db.conn.commit()
    res_edge = export_mod.run(str(exp_db), system="file", out=str(out_path))
    last_edge = json.loads(out_path.read_text(encoding="utf-8").splitlines()[-1])
    check(res_edge.get("exported") == 1 and last_edge["entity"]["id"] == 1
          and [e["quote"] for e in last_edge["events"]] == ["на границе курсора"],
          "export: событие с ts == курсору не теряется, более старое не всплывает",
          str(res_edge))
    res_edge2 = export_mod.run(str(exp_db), system="file", out=str(out_path))
    check(res_edge2.get("exported") == 0,
          "export: после граничного события повторный прогон пуст", str(res_edge2))

    with DB(exp_db) as db:
        db.conn.execute(
            "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
            "VALUES(1, 3, ?, 'update', -100999, 99, 'срок сдвинут')", (now - 1800,))
        db.conn.commit()
    res3 = export_mod.run(str(exp_db), system="file", out=str(out_path))
    last = json.loads(out_path.read_text(encoding="utf-8").splitlines()[-1])
    check(res3.get("ok") is True and res3.get("exported") == 1 and last["entity"]["id"] == 1
          and len(last["events"]) == 1 and last["events"][0]["quote"] == "срок сдвинут",
          "export: новое событие выгружает ровно одну сущность и только новое событие", str(res3))

    res_n = export_mod.run(str(exp_db), system="notion")
    check(res_n.get("ok") is False and "не реализован" in res_n.get("error", ""),
          "export: notion — структурированная ошибка без исключения", str(res_n))
    res_1 = export_mod.run(str(exp_db), system="101")
    check(res_1.get("ok") is False and "не реализован" in res_1.get("error", ""),
          "export: 101 — структурированная ошибка без исключения", str(res_1))
    res_b = export_mod.run(str(exp_db), system="bogus")
    check(res_b.get("ok") is False and "неизвестная система" in res_b.get("error", ""),
          "export: неизвестная система — структурированная ошибка", str(res_b))

    saved_argv = sys.argv
    try:
        sys.argv = ["export.py", "--db", str(exp_db), "--system", "bogus"]
        try:
            export_mod.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
        check(code == 1, "export: CLI выходит кодом 1 на структурной ошибке", str(code))
    finally:
        sys.argv = saved_argv

    crash_db = db_path.parent / "export_crash.sqlite"
    crash_out = db_path.parent / "export_crash.jsonl"
    for f in (crash_db, crash_out):
        if f.exists():
            f.unlink()
    with DB(crash_db) as db:
        for eid, title in ((1, "Первая"), (2, "Вторая")):
            db.conn.execute(
                "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, created_ts, updated_ts) "
                "VALUES(?, 'construction', -100999, 7, 'task', ?, ?, ?)", (eid, title, now, now))
            db.conn.execute(
                "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
                "VALUES(?, 1, ?, 'new', -100999, ?, 'цитата')", (eid, now - 60, eid))
        db.conn.commit()

    orig_open = Path.open
    state = {"writes": 0}

    def crash_open(self, *a, **k):
        fh = orig_open(self, *a, **k)
        if self != crash_out:
            return fh

        class Wrapped:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fh.close()

            def write(self, data):
                state["writes"] += 1
                if state["writes"] == 2:
                    raise RuntimeError("краш на второй строке")
                return fh.write(data)

            def flush(self):
                fh.flush()

        return Wrapped()

    Path.open = crash_open
    try:
        try:
            export_mod.run(str(crash_db), system="file", out=str(crash_out))
            crashed = False
        except RuntimeError:
            crashed = True
    finally:
        Path.open = orig_open
    check(crashed, "export: краш посреди прогона виден наружу", str(crashed))
    with DB(crash_db) as db:
        synced = db.conn.execute(
            "SELECT COUNT(*) FROM integrations WHERE system='file' "
            "AND synced_ts IS NOT NULL").fetchone()[0]
    check(synced == 1, "export: сущность до краха закоммичена (окно — одна строка)",
          str(synced))
    res_c = export_mod.run(str(crash_db), system="file", out=str(crash_out))
    ids = [json.loads(x)["entity"]["id"]
           for x in crash_out.read_text(encoding="utf-8").splitlines()]
    check(res_c.get("exported") == 1 and ids == [1, 2],
          "export: повтор после краха — только незакоммиченное, без дублей",
          f"{res_c} ids={ids}")


# ------------------------------------------- 5. настоящий server.py по MCP stdio

def rpc(proc: subprocess.Popen, payload: dict) -> None:
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def read_msg(proc: subprocess.Popen, timeout: float = 60.0) -> dict:
    q = getattr(proc, "_lines", None)
    if q is None:
        q = queue.Queue()

        def pump() -> None:
            for line in proc.stdout:
                q.put(line)
            q.put(None)

        proc._lines = q
        threading.Thread(target=pump, daemon=True).start()
    while True:
        try:
            line = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("нет ответа от MCP-сервера")
        if line is None:
            raise RuntimeError("сервер закрыл stdout: " + (proc.stderr.read() or ""))
        if line.strip():
            return json.loads(line)


def wait_for(proc: subprocess.Popen, pred) -> dict:
    for _ in range(40):
        msg = read_msg(proc)
        if pred(msg):
            return msg
    raise TimeoutError("не дождались нужного сообщения")


def test_live_and_poll(db_path: Path) -> None:
    print("=== live.py: обработчик + /api/poll ===")
    live_path = db_path.parent / "live.sqlite"
    users = {1: User(1, "Аня", "К", "anya")}
    msg = Msg(100, datetime.fromtimestamp(int(time.time()), timezone.utc), users[1],
              "ссылка https://live.test/x", reply_to=MessageReplyHeader(5, True))
    import live as live_mod

    with DB(str(live_path)) as db:
        ok1 = live_mod.handle_message(db, msg, -100111, True)
        ok2 = live_mod.handle_message(db, msg, -100111, True)  # та же месса повторно
        refs = db.refs_summary(chat_id=-100111).get("url", [])
        n = db.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    check(ok1 and not ok2, "live.handle_message: вставка + идемпотентность", f"{ok1}/{ok2}")
    check(len(refs) == 1 and refs[0]["value"] == "https://live.test/x",
          "live: refs у новой мессы записаны", str(refs))
    check(n == 1, "live: в индексе ровно одна строка", str(n))

    # /api/poll отдаёт сообщения по курсору времени
    import socket
    import urllib.request as urlreq

    with socket.socket() as probe:  # свободный порт, чтобы не драться с живым UI
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "TG_DB": str(db_path), "PORT": str(port), "HOST": "127.0.0.1"}
    proc = subprocess.Popen([sys.executable, "ui.py"], env=env,
                            cwd=str(Path(__file__).parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        got = None
        for _ in range(50):
            try:
                got = json.loads(urlreq.urlopen(
                    f"http://127.0.0.1:{port}/api/poll?since=0", timeout=1).read())
                break
            except Exception:
                time.sleep(0.2)
        check(got is not None and len(got["messages"]) > 0,
              "ui /api/poll: отдаёт сообщения индекса",
              str(len(got["messages"]) if got else None))
        fut = json.loads(urlreq.urlopen(
            f"http://127.0.0.1:{port}/api/poll?since={int(time.time()) + 600}", timeout=2).read())
        check(fut["messages"] == [], "ui /api/poll: с курсором в будущем — пусто")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_gates(db_path: Path) -> None:
    print("=== Гейты: busy_timeout, даты, flood, live-темы, токен UI ===")
    # busy_timeout: live.py и sync.py не подерутся за базу
    with DB(str(db_path)) as db:
        bt = db.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    check(int(bt) >= 5000, "db: busy_timeout >= 5s", str(bt))

    # окно по датам и листание before_id (уровень db)
    now = int(time.time())
    with DB(str(db_path)) as db:
        old = db.history(-100111, date_to=now - 86400 * 2)
        paged = db.history(-100111, before_id=12)
    check(bool(old) and all(m["ts"] <= now - 86400 * 2 for m in old),
          "history: date_to отсекает свежее", str(len(old)))
    check(bool(paged) and all(m["msg_id"] < 12 for m in paged),
          "history: before_id листает в прошлое", str([m["msg_id"] for m in paged]))

    # FloodWait: первая итерация падает — sync ждёт и доделывает
    client = build_fixture()
    client.flood_once = True
    flood_db = db_path.parent / "flood.sqlite"
    rep = asyncio.run(sync_mod.run(db_path=str(flood_db), client=client))
    n = sum(c["new_messages"] for c in rep["chats"])
    check(n == 16, "sync: FloodWait пережил, всё записано (16)", str(n))

    # FloodWait посреди итерации: вставленные до ожидания не занижают new
    client = build_fixture()
    client.flood_mid = 3
    flood_db2 = db_path.parent / "flood_mid.sqlite"
    rep2 = asyncio.run(sync_mod.run(db_path=str(flood_db2), client=client))
    n2 = sum(c["new_messages"] for c in rep2["chats"])
    with DB(str(flood_db2)) as db:
        actual = db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    check(n2 == actual and actual > 0,
          "sync: FloodWait посреди прохода — new равен числу вставленных",
          f"reported={n2} in_db={actual}")

    # live: новая тема сразу с заголовком
    live_mod = __import__("live")
    users = {2: User(2, "Боря", "П", "borya")}
    svc = Msg(500, datetime.fromtimestamp(now, timezone.utc), users[2], "тема создана",
              action=MessageActionTopicCreated(title="Срочное"))
    gates_db = db_path.parent / "gates.sqlite"
    with DB(str(gates_db)) as db:
        live_mod.handle_message(db, svc, -100333, True)
        t = db.topics(-100333)
    check(any(x["topic_id"] == 500 and x["title"] == "Срочное" for x in t),
          "live: новая тема сохранена с заголовком", str(t))

    # UI с TG_UI_TOKEN: без токена 401, с токеном 200
    import socket
    import urllib.error as urlerr
    import urllib.request as urlreq

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "TG_DB": str(db_path), "PORT": str(port),
           "HOST": "127.0.0.1", "TG_UI_TOKEN": "sekret"}
    proc = subprocess.Popen([sys.executable, "ui.py"], env=env,
                            cwd=str(Path(__file__).parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        code = None
        for _ in range(50):
            try:
                urlreq.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1)
                code = 200
                break
            except urlerr.HTTPError as e:
                code = e.code
                break
            except Exception:
                time.sleep(0.2)
        check(code == 401, "ui: /api/state без токена -> 401", str(code))
        got = json.loads(urlreq.urlopen(
            f"http://127.0.0.1:{port}/api/state?token=sekret", timeout=2).read())
        check("chats" in got, "ui: с токеном работает", str(list(got)[:3]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_strict_and_doctor(db_path: Path) -> None:
    print("=== TG_STRICT_WATCH, doctor, бюджет дайджеста ===")
    env = dict(os.environ, TG_DB=str(db_path), TG_STRICT_WATCH="1")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "0"}}})
        wait_for(proc, lambda m: m.get("id") == 1)
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        def scall(tool: str, args: dict, rid: int) -> dict:
            rpc(proc, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}})
            m = wait_for(proc, lambda x: x.get("id") == rid)
            return json.loads(m["result"]["content"][0]["text"])

        den = scall("tg_read", {"chat_id": -100222, "topic_id": 1}, 21)
        check(den.get("error") == "access_denied",
              "strict: чат вне watch-листа отклонён", str(den)[:90])
        okr = scall("tg_read", {"chat_id": -100111, "topic_id": 5}, 22)
        check("messages" in okr and len(okr["messages"]) > 0,
              "strict: слежуемая тема читается", str(okr.get("count")))
        fnd = scall("tg_find", {"query": "durov"}, 23)
        check(all(h["chat_id"] != -100222 for h in fnd.get("hits", [])),
              "strict: tg_find вне watch не ищет", str(fnd.get("count")))
        tp = scall("tg_topics", {}, 24)
        check(all(c["chat_id"] != -100222 for c in tp["chats"]),
              "strict: tg_topics не показывает чужие чаты",
              str([c["chat_id"] for c in tp["chats"]]))
        den2 = scall("tg_entities", {"chat_id": -100222}, 25)
        check(den2.get("error") == "access_denied",
              "strict: tg_entities вне watch-листа отклонён", str(den2)[:90])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # doctor: офлайн-чеки работают и единообразны
    import doctor
    checks = doctor.run_checks()
    by = {c["name"]: c for c in checks}
    check(by["sqlite FTS5"]["ok"] is True, "doctor: FTS5-чек зелёный", by["sqlite FTS5"]["detail"])
    check(by["redact self-test"]["ok"] is True, "doctor: redact self-test зелёный",
          by["redact self-test"]["detail"])
    check(all(set(c) == {"name", "ok", "detail"} for c in checks),
          "doctor: единый формат чеков", str(len(checks)))

    # бюджет контекста дайджеста
    import digest as digest_mod
    big = [{"sender_name": "x", "text": "ы" * 300} for _ in range(100)]
    ctx = digest_mod.build_context(big, "Тест")
    check(len(ctx) <= digest_mod.DIGEST_MAX_CHARS + 100,
          "digest: контекст вписан в бюджет", f"{len(ctx)}")
    check("не вошли в контекст" in ctx, "digest: обрезка честно помечена")


def test_mcp_server(db_path: Path) -> None:
    env = dict(os.environ, TG_DB=str(db_path))
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "0"}}})
        init = wait_for(proc, lambda m: m.get("id") == 1)
        check("serverInfo" in init.get("result", {}), "MCP initialize: сервер ответил",
              str(init.get("result", {}).get("serverInfo")))
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = wait_for(proc, lambda m: m.get("id") == 2)
        names = {t["name"] for t in tools["result"]["tools"]}
        expected = {"tg_watch", "tg_new", "tg_read", "tg_find", "tg_stats",
                    "tg_mark_read", "tg_index_stats", "tg_links", "tg_topics",
                    "tg_digests", "tg_entities", "tg_entity", "tg_overdue"}
        check(expected <= names and len(names) == 13,
              "MCP tools/list: все 13 инструментов на месте",
              str(sorted(names)))
        check(not any(n.startswith(("send_", "tg_live")) for n in names),
              "MCP: без флагов нет ни отправки, ни живого доступа")
        check("tg_sql" not in names, "MCP: tg_sql скрыт без TG_EXPOSE_SQL=1")

        def call(tool: str, args: dict, rid: int) -> dict:
            rpc(proc, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}})
            msg = wait_for(proc, lambda m: m.get("id") == rid)
            assert not msg.get("result", {}).get("isError"), msg
            return json.loads(msg["result"]["content"][0]["text"])

        w = call("tg_watch", {}, 3)
        check(w["count"] == 2 and w["items"][0]["name"] == "Релизы команды",
              "MCP tg_watch: отдаёт watch-лист с алиасами", str(w["count"]))
        check(w["items"][0]["new_since_read"] == 2,
              "MCP tg_watch: непрочитанное считается от last_read",
              str(w["items"][0]["new_since_read"]))

        new = call("tg_new", {"chat_id": -100111, "topic_id": 5}, 4)
        check([m["msg_id"] for m in new["messages"]] == [16, 39],
              "MCP tg_new: только новое с момента прочтения",
              str([m["msg_id"] for m in new["messages"]]))
        one = call("tg_new", {"chat_id": -100111, "topic_id": 5, "limit": 1}, 4)
        check(one["truncated"] is True,
              "MCP tg_new: truncated не сбрасывается, когда отдали не всё",
              f"returned={one['returned']} pending={one['pending_total']} truncated={one.get('truncated')}")

        hist = call("tg_read", {"chat_id": -100111, "topic_id": 5, "limit": 10}, 5)
        check(hist["count"] == 6, "MCP tg_read(topic=5): 6 сообщений", f"{hist['count']}")

        found = call("tg_find", {"query": "двухлетней"}, 6)
        check(found["count"] == 1 and found["engine"] == "fts5",
              "MCP tg_find: FTS5-поиск по старой истории", f"{found['count']}")

        stats = call("tg_stats", {"chat_id": -100111}, 7)
        check(stats["total_messages"] == 10, "MCP tg_stats: 10 сообщений",
              f"{stats['total_messages']}")

        mr = call("tg_mark_read", {"chat_id": -100111, "topic_id": 5, "mark_read_id": 39}, 8)
        check(mr["ok"] is True, "MCP tg_mark_read: отметка прошла")
        w2 = call("tg_watch", {}, 9)
        check(w2["items"][0]["new_since_read"] == 0,
              "MCP tg_watch после mark_read: непрочитанного нет",
              str(w2["items"][0]["new_since_read"]))
        check(all("pinned" in it for it in w2["items"]),
              "MCP tg_watch: у каждой темы есть поле pinned")

        lk = call("tg_links", {"chat_id": -100222}, 11)
        check(lk["mention"][0]["value"] == "durov" and lk["forward"][0]["value"] == "777",
              "MCP tg_links: ссылки и форварды отдаются",
              str({k: v[0]["value"] for k, v in lk.items() if isinstance(v, list) and v}))

        # --- tg_digests (дайджесты, stub-провайдер) ---
        import digest as digest_mod

        made = digest_mod.run(str(db_path))
        check(len(made) >= 1, "digest.run: выжимки созданы", str(len(made)))
        made2 = digest_mod.run(str(db_path))
        check(made2 == [], "digest.run повторно: новых нет (курсор в meta)", str(len(made2)))
        dg = call("tg_digests", {"limit": 10}, 9)
        check(isinstance(dg, list) and len(dg) >= 1
              and "[stub-выжимка]" in dg[0]["summary"],
              "MCP tg_digests: дайджесты отдаются",
              str(dg[0]["summary"][:70] if dg else None))

        # --- tg_topics: структура индекса ---
        tp = call("tg_topics", {}, 9)
        check(len(tp["chats"]) >= 2 and any(c["chat_id"] == -100111 for c in tp["chats"]),
              "MCP tg_topics: чаты индекса отдаются", str([c["chat_id"] for c in tp["chats"]]))
        tp2 = call("tg_topics", {"chat_id": -100111}, 9)
        check(any(t["topic_id"] == 5 and t["title"] == "Релизы" for t in tp2["topics"]),
              "MCP tg_topics(chat_id): темы со счётчиками",
              str([t["topic_id"] for t in tp2["topics"]]))

        # --- бюджет выдачи и листание ---
        rd = call("tg_read", {"chat_id": -100111, "topic_id": 5,
                              "limit": 100, "budget_chars": 200}, 9)
        check(rd["truncated"] is True and rd["count"] < 6 and "next_before" in rd,
              "MCP tg_read: budget_chars режет выдачу + next_before",
              f"count={rd['count']} truncated={rd['truncated']}")
        # before_id листает строго старше — вырезанное бюджетом не теряется
        check(rd["next_before"] == 16,
              "MCP tg_read: next_before позволяет добрать вырезанное бюджетом",
              f"next_before={rd['next_before']} count={rd['count']}")
        rd2 = call("tg_read", {"chat_id": -100111, "topic_id": 5, "before_id": 13}, 9)
        check(bool(rd2["messages"]) and all(m["msg_id"] < 13 for m in rd2["messages"]),
              "MCP tg_read: before_id листает в прошлое",
              str([m["msg_id"] for m in rd2["messages"]]))

        st = call("tg_index_stats", {}, 10)
        check(st["messages"] == 27 and st["watched"] == 2,
              "MCP tg_index_stats: 27 сообщений / 2 в watch-листе",
              f"{st['messages']}/{st['watched']}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_live_tools_flag(db_path: Path) -> None:
    print("=== TG_ALLOW_LIVE: инструменты живого доступа ===")
    env = dict(os.environ, TG_DB=str(db_path), TG_ALLOW_LIVE="1")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "0"}}})
        wait_for(proc, lambda m: m.get("id") == 1)
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = wait_for(proc, lambda m: m.get("id") == 2)
        names = {t["name"] for t in tools["result"]["tools"]}
        check({"tg_live_topics", "tg_live_history", "tg_live_search"} <= names,
              "MCP: TG_ALLOW_LIVE=1 добавляет tg_live_* инструменты",
              str(sorted(names)))

        # вызов живого инструмента без кредов — внятная ошибка, а не зависание
        rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "tg_live_history",
                              "arguments": {"chat_id": -100111, "limit": 5}}})
        resp = wait_for(proc, lambda m: m.get("id") == 3)
        body = json.loads(resp["result"]["content"][0]["text"])
        check("error" in body,
              "MCP: tg_live без кредов возвращает JSON-ошибку, а не молчит",
              str(body.get("error", ""))[:80])
        rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                   "params": {"name": "tg_live_search",
                              "arguments": {"query": "ИИ", "limit": 5}}})
        resp4 = wait_for(proc, lambda m: m.get("id") == 4)
        body4 = json.loads(resp4["result"]["content"][0]["text"])
        check("error" in body4,
              "MCP: tg_live_search без кредов — JSON-ошибка, не зависание",
              str(body4.get("error", ""))[:80])
        rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        alive = wait_for(proc, lambda m: m.get("id") == 5)
        check(alive.get("id") == 5, "MCP: сервер жив после ошибки live-инструмента")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_backfill_resume(db_path: Path) -> None:
    print("=== backfill: resume прерванного прогона ===")
    client = build_fixture()
    u1, u2 = User(1, "Аня"), User(2, "Борис")
    arch = Channel(-100555, "Архив", None, forum=False)
    arch.broadcast, arch.megagroup = False, True
    group = {"entity": arch,
             "messages": [Msg(200 + i, NOW - timedelta(days=800 - i),
                              u1 if i % 2 else u2, f"архив {i}") for i in range(7)],
             "topics": []}
    client.groups[-100555] = group

    bf_db = db_path.parent / "bf_resume.sqlite"
    with DB(bf_db) as db:
        first = asyncio.run(sync_mod.backfill(
            client, db, -100555, forum=False, chunk_size=5, sleep=0,
            max_messages=3, entity=arch))
        check(first["seen"] == 3, "backfill: max_messages останавливает после 3", str(first))
        check(db.get_meta("backfill:-100555") == "202",
              "backfill: прогресс сохранён в meta", str(db.get_meta("backfill:-100555")))
        second = asyncio.run(sync_mod.backfill(
            client, db, -100555, forum=False, chunk_size=5, sleep=0, entity=arch))
        check(second["seen"] == 4,
              "backfill: повторный запуск продолжает, а не начинает с нуля",
              str(second))
        total = db.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=-100555").fetchone()[0]
        check(total == 7, "backfill: в сумме все 7 сообщений", str(total))


def test_marked_ids(db_path: Path) -> None:
    print("=== id-формы: entity.id -> marked на границе Telethon ===")
    client = build_fixture()
    u1 = User(1, "Аня")
    real = Channel(777, "Real Form Group", None, forum=False)
    real.broadcast, real.megagroup = False, True
    group = {"entity": real, "messages": [Msg(10, NOW, u1, "из real-формы")], "topics": []}
    client.groups[777] = group

    mk_db = db_path.parent / "marked.sqlite"
    r = asyncio.run(sync_mod.run(db_path=str(mk_db), client=client,
                                 names=["-1000000000777"]))
    check(len(r["chats"]) == 1, "select_chats: матчится marked-форма id", str(len(r["chats"])))
    with DB(mk_db) as db:
        row = db.conn.execute(
            "SELECT id FROM chats WHERE title='Real Form Group'").fetchone()
        check(row is not None and row["id"] == -1000000000777,
              "sync: положительный entity.id хранится как marked", str(row["id"] if row else None))
        mid = db.conn.execute(
            "SELECT chat_id FROM messages WHERE text='из real-формы'").fetchone()
        check(mid is not None and mid["chat_id"] == -1000000000777,
              "sync: сообщения привязаны к marked chat_id",
              str(mid["chat_id"] if mid else None))


def test_ui_security(db_path: Path) -> None:
    print("=== UI: Host-гейт, Content-Type на POST, 0.0.0.0 без токена ===")
    import socket
    import urllib.error as urlerr
    import urllib.request as urlreq

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "TG_DB": str(db_path), "PORT": str(port), "HOST": "127.0.0.1"}
    proc = subprocess.Popen([sys.executable, "ui.py"], env=env,
                            cwd=str(Path(__file__).parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # legacy-строка с сырым токеном: /api/messages и /api/poll не должны его отдавать
        with DB(str(db_path)) as db:
            db.conn.execute(
                "INSERT OR IGNORE INTO messages(chat_id, msg_id, topic_id, ts, text) "
                "VALUES(-100222, 998, 1, 1, 'ui ghp_" + "d" * 40 + "')")
            db.conn.commit()

        for _ in range(50):  # ждём подъёма
            try:
                urlreq.urlopen(f"http://127.0.0.1:{port}/api/poll?since=0", timeout=1)
                break
            except Exception:
                time.sleep(0.2)

        msgs = json.loads(urlreq.urlopen(
            f"http://127.0.0.1:{port}/api/messages?chat_id=-100222&topic_id=1&limit=300",
            timeout=2).read())
        poll = json.loads(urlreq.urlopen(
            f"http://127.0.0.1:{port}/api/poll?since=0", timeout=2).read())
        check(all("ghp_" not in (m.get("text") or "") for m in msgs["messages"]),
              "ui /api/messages: сырой текст замаскирован",
              str(len(msgs["messages"])))
        check(all("ghp_" not in (m.get("text") or "") for m in poll["messages"]),
              "ui /api/poll: сырой текст замаскирован", str(len(poll["messages"])))

        st = json.loads(urlreq.urlopen(
            f"http://127.0.0.1:{port}/api/state", timeout=5).read())
        c222 = next((c for c in st["chats"] if c["id"] == -100222), None)
        check(c222 is not None
              and {"topics", "watched", "pins", "last_text", "last_sender",
                   "new_total", "msg_count", "is_forum"} <= set(c222),
              "ui /api/state: контракт чата сохранён",
              str(sorted(c222)) if c222 else "нет чата")
        check(c222 is not None and "ghp_" not in (c222.get("last_text") or ""),
              "ui /api/state: last_text замаскирован")
        check(st.get("stats", {}).get("messages", 0) > 0 and "folders" in st,
              "ui /api/state: stats и folders на месте")

        # подменённый Host (DNS-rebinding/CSRF) отклоняется
        req = urlreq.Request(f"http://127.0.0.1:{port}/api/state",
                             headers={"Host": "evil.example"})
        try:
            urlreq.urlopen(req, timeout=2)
            code = 200
        except urlerr.HTTPError as e:
            code = e.code
        check(code == 403, "ui: чужой Host -> 403", str(code))

        # POST не-JSON (form) — как из чужой формы в браузере
        data = "chat_id=-100222&topic_id=1".encode()
        req2 = urlreq.Request(f"http://127.0.0.1:{port}/api/watch", data=data,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            urlreq.urlopen(req2, timeout=2)
            code2 = 200
        except urlerr.HTTPError as e:
            code2 = e.code
        check(code2 == 415, "ui: POST без application/json -> 415", str(code2))

        # легальный JSON POST работает и откатывается
        body = json.dumps({"chat_id": -100222, "topic_id": 1}).encode()
        req3 = urlreq.Request(f"http://127.0.0.1:{port}/api/watch", data=body,
                              headers={"Content-Type": "application/json"})
        got = json.loads(urlreq.urlopen(req3, timeout=2).read())
        body_u = json.dumps({"chat_id": -100222, "topic_id": 1}).encode()
        req4 = urlreq.Request(f"http://127.0.0.1:{port}/api/unwatch", data=body_u,
                              headers={"Content-Type": "application/json"})
        un = json.loads(urlreq.urlopen(req4, timeout=2).read())
        check(got.get("ok") and un.get("ok"),
              "ui: JSON POST работает (watch + unwatch)", f"{got}/{un}")

        # визард в UI: список чатов из Telegram (в тестах кредов нет — внятная ошибка)
        avail = json.loads(urlreq.urlopen(
            f"http://127.0.0.1:{port}/api/available", timeout=15).read())
        check(avail.get("ok") is False and avail.get("error"),
              "ui /api/available: без ключей понятная ошибка, не 500",
              str(avail.get("error"))[:70])

        bad = urlreq.Request(f"http://127.0.0.1:{port}/api/sync_chat",
                             data=json.dumps({"chat": ""}).encode(),
                             headers={"Content-Type": "application/json"})
        try:
            urlreq.urlopen(bad, timeout=2)
            bad_code = 200
        except urlerr.HTTPError as e:
            bad_code = e.code
        check(bad_code == 400, "ui /api/sync_chat: пустой чат -> 400", str(bad_code))

        ok_req = urlreq.Request(f"http://127.0.0.1:{port}/api/sync_chat",
                                data=json.dumps({"chat": "@nobody", "days": 3}).encode(),
                                headers={"Content-Type": "application/json"})
        started = json.loads(urlreq.urlopen(ok_req, timeout=5).read())
        check(started.get("started") is True and started.get("days") == 3,
              "ui /api/sync_chat: запуск синка принят", str(started))
        status = None
        for _ in range(60):
            status = json.loads(urlreq.urlopen(
                f"http://127.0.0.1:{port}/api/sync_status", timeout=2).read())
            if not status.get("running"):
                break
            time.sleep(0.25)
        check(status is not None and status.get("code") is not None,
              "ui /api/sync_chat: прогресс долетает до /api/sync_status",
              str(status)[:80])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        with DB(str(db_path)) as db:
            db.conn.execute("DELETE FROM messages WHERE chat_id=-100222 AND msg_id=998")
            db.conn.commit()

    # HOST=0.0.0.0 без токена — подниматься отказывается
    # PYTHONIOENCODING: отказ печатается по-русски, без utf-8 чтение пайпа падает на Windows
    env2 = {**os.environ, "TG_DB": str(db_path), "PORT": str(port), "HOST": "0.0.0.0",
            "PYTHONIOENCODING": "utf-8"}
    proc2 = subprocess.Popen([sys.executable, "ui.py"], env=env2,
                             cwd=str(Path(__file__).parent),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8")
    out = proc2.communicate(timeout=20)[0] or ""
    check(proc2.returncode != 0 and "TG_UI_TOKEN" in out,
          "ui: отказ поднимать 0.0.0.0 без TG_UI_TOKEN", out.strip()[:70])


def test_live_collect(db_path: Path) -> None:
    print("=== live.collect_ids: подхват новых чатов ===")
    import live as live_mod

    lc_db = db_path.parent / "live_collect.sqlite"
    with DB(lc_db) as db:
        db.upsert_chat(chat_id=-100111, title="Форум", username=None,
                       kind="supergroup", is_forum=True, members=5)
    ids, forums = live_mod.collect_ids(str(lc_db))
    check(-100111 in ids and -100111 in forums,
          "live.collect_ids: чаты из индекса", str(sorted(ids)))
    with DB(lc_db) as db:
        db.watch_add(-100999, 1, alias="новый")
    ids2, _ = live_mod.collect_ids(str(lc_db))
    check(-100999 in ids2, "live.collect_ids: watch-добавка видна без рестарта",
          str(sorted(ids2)))


def test_digest_resilience(db_path: Path) -> None:
    print("=== digest: сбой LLM, маскировка, курсор ===")
    dg_db = db_path.parent / "digest_res.sqlite"
    with DB(dg_db) as db:
        db.watch_add(-100222, 1, alias="Тест")
        db.conn.execute(
            "INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) "
            "VALUES(-100222, 1, 1, 1, 'сырой ghp_" + "e" * 40 + "')")
        db.conn.commit()

    import digest as digest_mod
    old_env = {k: os.environ.get(k) for k in ("DIGEST_PROVIDER", "DIGEST_URL")}
    os.environ["DIGEST_PROVIDER"] = "openai"
    os.environ["DIGEST_URL"] = "http://127.0.0.1:9/x"
    try:
        made = digest_mod.run(str(dg_db))
        check(made == [], "digest: сбой LLM не роняет прогон", str(made))
        with DB(dg_db) as db:
            n = db.conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
            check(n == 0, "digest: сбой LLM не пишет дайджест", str(n))

        os.environ["DIGEST_PROVIDER"] = "stub"
        made2 = digest_mod.run(str(dg_db))
        with DB(dg_db) as db:
            dg = db.conn.execute("SELECT summary FROM digests").fetchone()
        check(made2 and dg is not None and "ghp_" not in (dg["summary"] or ""),
              "digest: выжимка не содержит сырой токен",
              (dg["summary"] or "")[:60] if dg else "нет дайджеста")
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_tg_new_fit_honest(db_path: Path) -> None:
    print("=== tg_new: returned/max_id после урезки бюджетом ===")
    tn_db = db_path.parent / "tg_new_fit.sqlite"
    with DB(tn_db) as db:
        db.upsert_chat(chat_id=-100777, title="Длинная тема", username=None,
                       kind="supergroup", is_forum=True, members=1)
        db.watch_add(-100777, 1, alias="длинная")
        for i in range(1, 31):
            db.insert_message({"chat_id": -100777, "msg_id": i, "topic_id": 1,
                               "ts": 1700000000 + i, "text": "ы" * 1200})
    import server as server_mod
    old_index = server_mod.INDEX
    server_mod.INDEX = str(tn_db)
    try:
        out = json.loads(server_mod.tg_new(-100777, 1))
    finally:
        server_mod.INDEX = old_index
    check(out["pending_total"] == 30 and out["truncated"] is True,
          "tg_new: бюджет урезает выдачу",
          f"returned={out.get('returned')} pending={out.get('pending_total')}")
    check(out["returned"] == len(out["messages"])
          and out["max_id"] == max(m["msg_id"] for m in out["messages"]),
          "tg_new: returned/max_id описывают показанное, а не запрошенное",
          f"returned={out.get('returned')} max_id={out.get('max_id')} "
          f"real_max={max(m['msg_id'] for m in out['messages'])}")


def test_marked_migration(db_path: Path) -> None:
    print("=== миграция старых индексов на marked-id ===")
    mig_db = db_path.parent / "mig.sqlite"
    with DB(mig_db) as db:
        db.upsert_chat(chat_id=777, title="Old", username=None,
                       kind="supergroup", is_forum=True, members=1)
        db.upsert_chat(chat_id=55, title="OldG", username=None,
                       kind="group", is_forum=False, members=1)
        db.insert_message({"chat_id": 777, "msg_id": 10, "topic_id": 1, "ts": 5, "text": "x"})
        db.insert_message({"chat_id": 55, "msg_id": 1, "ts": 5, "text": "y"})
        db.watch_add(777, 1, alias="старая")
        db.set_cursor(777, 0, 10)
        db.set_meta("backfill:777", "10")

    with DB(mig_db) as db:
        ids = {r["id"] for r in db.chats()}
        check(ids == {-1000000000777, -55}, "миграция: chats переведены в marked", str(ids))
        mid = db.conn.execute("SELECT chat_id FROM messages WHERE msg_id=10").fetchone()
        check(mid is not None and mid["chat_id"] == -1000000000777,
              "миграция: messages переведены")
        wl = db.watch_list()
        check(wl and wl[0]["chat_id"] == -1000000000777 and wl[0]["alias"] == "старая",
              "миграция: watch переведён", str([(w["chat_id"], w["alias"]) for w in wl]))
        check(db.get_cursor(-1000000000777, 0) == 10, "миграция: курсор перенесён",
              str(db.get_cursor(-1000000000777, 0)))
        check(db.get_meta("backfill:-1000000000777") == "10",
              "миграция: backfill-курсор перенесён")
        fts = db.conn.execute(
            "SELECT COUNT(*) FROM messages_fts f JOIN messages m ON m.rowid = f.rowid "
            "WHERE m.chat_id = -1000000000777").fetchone()[0]
        check(fts == 1, "миграция: FTS согласован с перенесёнными строками", str(fts))


def test_build_state_last(db_path: Path) -> None:
    print("=== build_state: последнее сообщение по (chat_id, msg_id) ===")
    bs_db = db_path.parent / "last_msg.sqlite"
    with DB(bs_db) as db:
        db.upsert_chat(chat_id=-100501, title="A", username=None, kind="supergroup",
                       is_forum=False, members=1)
        db.upsert_chat(chat_id=-100502, title="B", username=None, kind="supergroup",
                       is_forum=False, members=1)
        db.insert_message({"chat_id": -100501, "msg_id": 100, "ts": 300, "text": "A-100"})
        db.insert_message({"chat_id": -100501, "msg_id": 50, "ts": 200, "text": "A-50"})
        db.insert_message({"chat_id": -100502, "msg_id": 50, "ts": 250, "text": "B-50"})
    import ui as ui_mod
    old = ui_mod.DB_PATH
    ui_mod.DB_PATH = str(bs_db)
    try:
        st = ui_mod.build_state()
    finally:
        ui_mod.DB_PATH = old
    a = next(c for c in st["chats"] if c["id"] == -100501)
    b = next(c for c in st["chats"] if c["id"] == -100502)
    check(a["last_text"] == "A-100", "build_state: last чата A — его максимум",
          repr(a["last_text"]))
    check(b["last_text"] == "B-50", "build_state: last чата B — его максимум",
          repr(b["last_text"]))


def test_env_loader(db_path: Path) -> None:
    print("=== env: .env загрузчик ===")
    from env import load_env, write_env

    envf = db_path.parent / "test_env.env"
    envf.write_text(
        "# коммент\nAPI_TEST_ONE=1\nAPI_TEST_TWO='в кавычках'\n# API_TEST_SKIPPED=x\n",
        encoding="utf-8")
    for k in ("API_TEST_ONE", "API_TEST_TWO"):
        os.environ.pop(k, None)
    try:
        load_env(envf)
        check(os.environ.get("API_TEST_ONE") == "1",
              "env: читает KEY=VALUE", str(os.environ.get("API_TEST_ONE")))
        check(os.environ.get("API_TEST_TWO") == "в кавычках",
              "env: снимает кавычки", str(os.environ.get("API_TEST_TWO")))
        check("API_TEST_SKIPPED" not in os.environ, "env: комменты пропускает")
        os.environ["API_TEST_ONE"] = "777"
        load_env(envf)
        check(os.environ["API_TEST_ONE"] == "777",
              "env: не перекрывает уже заданное", os.environ["API_TEST_ONE"])
        load_env(db_path.parent / "nope.env")
        check(True, "env: отсутствующий файл — тихий no-op")

        # notepad пишет BOM, Windows PowerShell — UTF-16: оба должны читаться
        bom = db_path.parent / "test_bom.env"
        bom.write_text("API_BOM=42\n", encoding="utf-8-sig")
        os.environ.pop("API_BOM", None)
        load_env(bom)
        check(os.environ.get("API_BOM") == "42", "env: utf-8 c BOM читается")
        u16 = db_path.parent / "test_u16.env"
        u16.write_text("API_U16=43\n", encoding="utf-16")
        os.environ.pop("API_U16", None)
        load_env(u16)
        check(os.environ.get("API_U16") == "43", "env: utf-16 (Out-File) читается")

        # write_env обновляет свои ключи, не трогая чужие
        os.environ.pop("API_TEST_ONE", None)
        os.environ.pop("API_TEST_TWO", None)
        write_env({"API_TEST_TWO": "новое"}, path=envf)
        load_env(envf)
        check(os.environ.get("API_TEST_TWO") == "новое" and os.environ.get("API_TEST_ONE") == "1",
              "env: write_env обновляет ключ и не трогает остальные",
              f"{os.environ.get('API_TEST_TWO')}/{os.environ.get('API_TEST_ONE')}")

        # TG_ENV_FILE управляет тем, какой файл читать ("" — не читать вовсе)
        knob = db_path.parent / "test_knob.env"
        knob.write_text("API_KNOB=1\n", encoding="utf-8")
        os.environ.pop("API_KNOB", None)
        os.environ["TG_ENV_FILE"] = ""
        load_env()
        check("API_KNOB" not in os.environ, "env: TG_ENV_FILE='' отключает чтение .env")
        os.environ["TG_ENV_FILE"] = str(knob)
        load_env()
        check(os.environ.get("API_KNOB") == "1", "env: TG_ENV_FILE указывает конкретный файл")
        os.environ["TG_ENV_FILE"] = ""
        os.environ.pop("API_KNOB", None)
        knob.unlink(missing_ok=True)

        # бутстрап логина: спросил один раз -> записал -> больше не спрашивает
        import builtins
        import login as login_mod
        bs = db_path.parent / "test_bootstrap.env"
        bs.write_text("OTHER=1\n", encoding="utf-8")
        answers = iter(["123", "deadbeef"])
        orig_input = builtins.input
        builtins.input = lambda prompt="": next(answers)
        for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
            os.environ.pop(k, None)
        try:
            login_mod.ensure_creds(bs)
            ok1 = os.environ.get("TELEGRAM_API_ID") == "123"
            login_mod.ensure_creds(bs)  # второй вызов не должен спрашивать
            ok2 = "OTHER=1" in bs.read_text(encoding="utf-8")
            check(ok1 and ok2, "login: бутстрап спрашивает раз и не портит .env",
                  str((os.environ.get("TELEGRAM_API_ID"), ok2)))
        finally:
            builtins.input = orig_input
            for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
                os.environ.pop(k, None)
            bs.unlink(missing_ok=True)
        check(callable(getattr(login_mod, "main", None)),
              "login: модуль импортируется офлайн (без сети)")

        import io as _io
        from env import harden_stdio
        saved_out = sys.stdout
        buf = _io.TextIOWrapper(_io.BytesIO(), encoding="cp1251", errors="strict")
        sys.stdout = buf
        try:
            harden_stdio()  # переводит текущий stdout в errors=replace
            print("🔥 Привет — эмодзи вне cp1251")
            buf.flush()
            crashed = False
        except UnicodeEncodeError:
            crashed = True
        finally:
            sys.stdout = saved_out
        check(not crashed,
              "env: harden_stdio не роняет print на эмодзи в cp1251-консоли",
              str(crashed))
    finally:
        for k in ("API_TEST_ONE", "API_TEST_TWO", "API_BOM", "API_U16"):
            os.environ.pop(k, None)
        envf.unlink(missing_ok=True)
        bom.unlink(missing_ok=True)
        u16.unlink(missing_ok=True)


def test_login_2fa(db_path: Path) -> None:
    print("=== login: 2FA-пароль не роняет и не гоняет QR заново ===")
    import login as login_mod
    from telethon.errors import PasswordHashInvalidError

    class FakeSession:
        def __init__(self, key):
            self.auth_key = key

    class FakeLoginClient:
        def __init__(self, keys, auth_key=None):
            self._keys = list(keys)
            self.calls = 0
            self.session = FakeSession(auth_key)

        async def sign_in(self, password=None):
            self.calls += 1
            k = self._keys.pop(0) if self._keys else None
            if isinstance(k, Exception):
                raise k

    bad = PasswordHashInvalidError(None)
    orig = login_mod.getpass.getpass
    try:
        # две опечатки -> третья верная
        fc = FakeLoginClient([bad, bad, None])
        answers = iter(["a", "b", "c"])
        login_mod.getpass.getpass = lambda prompt="": next(answers)
        ok = asyncio.run(login_mod._password_flow(fc))
        check(ok is True and fc.calls == 3,
              "login: неверный пароль -> повторный вопрос, не падение", str(fc.calls))

        # три промаха -> честный False (вызывающий скажет, что делать)
        fc2 = FakeLoginClient([bad, bad, bad])
        answers2 = iter(["a", "b", "c"])
        login_mod.getpass.getpass = lambda prompt="": next(answers2)
        ok2 = asyncio.run(login_mod._password_flow(fc2))
        check(ok2 is False and fc2.calls == 3, "login: после 3 промахов — False, без исключения")

        # пустой ввод не расходует попытку
        fc3 = FakeLoginClient([None])
        answers3 = iter(["", "верный"])
        login_mod.getpass.getpass = lambda prompt="": next(answers3)
        ok3 = asyncio.run(login_mod._password_flow(fc3))
        check(ok3 is True and fc3.calls == 1,
              "login: пустой ввод не тратит попытку", str(fc3.calls))

        check(login_mod._needs_password(FakeLoginClient([], auth_key=None)) is False
              and login_mod._needs_password(FakeLoginClient([], auth_key=object())) is True,
              "login: _needs_password определяет принятый QR по auth_key")

        # подсказка к 2FA-паролю (Telegram хранит её и показывает на экране ввода)
        class FakePassword:
            def __init__(self, hint=None, has_recovery=False):
                self.hint = hint
                self.has_recovery = has_recovery

        class HintClient:
            def __init__(self, pw):
                self._pw = pw

            async def __call__(self, req):
                if req.__class__.__name__ == "GetPasswordRequest":
                    return self._pw
                raise AssertionError(req)

            async def sign_in(self, password=None):
                return None

        got = asyncio.run(login_mod._password_hint(
            HintClient(FakePassword(hint="мой кот", has_recovery=True))))
        check(got == "мой кот", "login: подсказка 2FA вытаскивается", repr(got))
        empty = asyncio.run(login_mod._password_hint(HintClient(FakePassword())))
        check(empty is None, "login: без подсказки — None, не падает", repr(empty))
        broken = asyncio.run(login_mod._password_hint(object()))
        check(broken is None, "login: сбой запроса подсказки не роняет логин")
        want_reset = asyncio.run(login_mod._has_recovery(
            HintClient(FakePassword(has_recovery=True))))
        check(want_reset is True, "login: флаг резервной почты читается")
    finally:
        login_mod.getpass.getpass = orig


def test_start_kwargs(db_path: Path) -> None:
    print("=== старт клиента: Telethon не принимает phone=None ===")
    import sync as sync_mod

    os.environ.pop("TELEGRAM_PHONE", None)
    check(sync_mod.start_kwargs() == {},
          "start_kwargs: без TELEGRAM_PHONE kwargs пусты (пусть Telethon сам спросит)",
          str(sync_mod.start_kwargs()))
    os.environ["TELEGRAM_PHONE"] = "+79990001122"
    try:
        kwargs = sync_mod.start_kwargs()
        check(kwargs == {"phone": "+79990001122"},
              "start_kwargs: с TELEGRAM_PHONE прокидывает номер", str(kwargs))
        check(kwargs.get("phone") is not None, "start_kwargs: phone никогда не None")
    finally:
        os.environ.pop("TELEGRAM_PHONE", None)


def test_days_window(db_path: Path) -> None:
    print("=== sync --days реально ограничивает окно ===")
    client = build_fixture()
    w_db = db_path.parent / "days.sqlite"
    for suf in ("", "-wal", "-shm"):
        f = w_db.with_name(w_db.name + suf)
        if f.exists():
            f.unlink()
    r = asyncio.run(sync_mod.run(db_path=str(w_db), client=client,
                                 pattern="Флудилка", days=1))
    got = sum(c["new_messages"] for c in r["chats"])
    since = time.time() - 86400
    expected = sum(1 for m in client.groups[-100222]["messages"]
                   if m.date.timestamp() >= since)
    check(got == expected and 0 < expected < 7,
          "sync --days 1: взяты только сообщения за сутки, без досасывания истории",
          f"got={got} expected={expected} (всего в чате 7)")


def test_start_wizard(db_path: Path) -> None:
    print("=== start.py: одна команда вместо десяти ===")
    import start as start_mod

    chats = [
        {"id": -100111, "title": "Работа", "username": "team_forum", "kind": "forum"},
        {"id": -100222, "title": "Без юзернейма", "username": None, "kind": "group"},
        {"id": -100333, "title": "Новости", "username": "ai_news", "kind": "channel"},
    ]
    check(start_mod.parse_pick("", chats) == [], "визард: пустой ответ = отмена")
    check(start_mod.parse_pick("1,3", chats) == ["@team_forum", "@ai_news"],
          "визард: номера через запятую", str(start_mod.parse_pick("1,3", chats)))
    check(start_mod.parse_pick("all", chats) == ["@team_forum", "-100222", "@ai_news"],
          "визард: all -> все чаты (без юзернейма — по id)",
          str(start_mod.parse_pick("all", chats)))
    check(start_mod.parse_pick("@custom, 2", chats) == ["@custom", "-100222"],
          "визард: ручной @username вместе с номером")
    check(start_mod.parse_pick("99, abc", chats) == [],
          "визард: мусор и выход за диапазон игнорируются")

    client = build_fixture()
    listed = asyncio.run(sync_mod.list_chat_choices(client))
    kinds = {c["kind"] for c in listed}
    check({"forum", "group", "channel"} <= kinds,
          "визард: листинг различает форум/группу/канал", str(sorted(kinds)))
    check(any(c["title"] == "Работа / Команда" and c["username"] == "team_forum"
              for c in listed), "визард: в списке есть заголовок и юзернейм")

    os.environ.pop("TELEGRAM_API_ID", None)
    os.environ.pop("TELEGRAM_API_HASH", None)
    try:
        sync_mod.make_client()
        check(False, "make_client: без ключей должен быть понятный SystemExit")
    except SystemExit as exc:
        check("start.py" in str(exc), "make_client: без ключей отсылает к start.py", str(exc))

    sess_name = db_path.parent / "test_make_client"
    os.environ["TELEGRAM_API_ID"] = "123"
    os.environ["TELEGRAM_API_HASH"] = "0123456789abcdef0123456789abcdef"
    os.environ["TELEGRAM_SESSION_NAME"] = str(sess_name)
    cl = None
    try:
        cl = sync_mod.make_client()
        check(getattr(cl, "_no_updates", False) is True,
              "make_client: receive_updates=False — CLI не блокирует общий .session",
              str(getattr(cl, "_no_updates", None)))
        bt = cl.session._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        check(bt == 30000,
              "make_client: busy_timeout 30с — конкуренция за .session переживается",
              str(bt))
    finally:
        for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_NAME"):
            os.environ.pop(k, None)
        if cl is not None:
            cl.session.close()
        sess_name.with_suffix(".session").unlink(missing_ok=True)


def test_topic_norm(db_path: Path) -> None:
    print("=== topic_id: General=1 канонически, миграция NULL, индекс ===")
    tdb = db_path.parent / "topic_norm.sqlite"
    if tdb.exists():
        tdb.unlink()
    with DB(tdb) as db:
        db.upsert_chat(-100500, "Группа", None, "group", False, 3)
        db.watch_add(-100500, 1)
        db.insert_message({"chat_id": -100500, "msg_id": 1, "ts": 100, "text": "без темы"})
        tid = db.conn.execute("SELECT topic_id FROM messages WHERE msg_id=1").fetchone()["topic_id"]
        check(tid == 1, "db: insert без topic_id пишет General=1", str(tid))
        db.insert_message({"chat_id": -100500, "msg_id": 2, "topic_id": 0, "ts": 200, "text": "ноль"})
        tid0 = db.conn.execute("SELECT topic_id FROM messages WHERE msg_id=2").fetchone()["topic_id"]
        check(tid0 == 1, "db: topic_id=0 нормализуется в 1", str(tid0))
        w = db.watch_list()
        check(w[0]["total"] == 2, "db: watch_list считает General по прямому сравнению",
              str(w[0]["total"]))
        plan = db.conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM messages WHERE chat_id=? AND topic_id=?",
            (-100500, 1)).fetchall()
        check(any("ix_msg_chat_topic" in (r["detail"] or "") for r in plan),
              "db: прямой topic_id=? использует индекс ix_msg_chat_topic",
              str([r["detail"] for r in plan]))

    ldb = db_path.parent / "topic_legacy.sqlite"
    if ldb.exists():
        ldb.unlink()
    import sqlite3 as sq
    raw = sq.connect(str(ldb))
    raw.execute(
        "CREATE TABLE messages ("
        "chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, topic_id INTEGER, "
        "ts INTEGER, date_iso TEXT, sender_id INTEGER, sender_name TEXT, "
        "is_out INTEGER DEFAULT 0, is_service INTEGER DEFAULT 0, text TEXT, "
        "media TEXT, reply_to INTEGER, views INTEGER, forwards INTEGER, "
        "reactions TEXT, raw_json TEXT, PRIMARY KEY (chat_id, msg_id))")
    raw.execute(
        "INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) "
        "VALUES(-100600, 1, NULL, 100, 'legacy без темы')")
    raw.commit()
    raw.close()
    with DB(ldb) as db:
        tid = db.conn.execute("SELECT topic_id FROM messages WHERE msg_id=1").fetchone()["topic_id"]
        check(tid == 1, "db: NULL topic_id старой базы мигрирует в 1 при открытии", str(tid))
        db.upsert_chat(-100600, "Легаси", None, "group", False, 3)
        db.watch_add(-100600, 1)
        w = db.watch_list()
        check(w[0]["total"] == 1, "db: watch_list видит мигрированный NULL как General",
              str(w[0]["total"]))
        hist = db.history(-100600, topic_id=1)
        check(len(hist) == 1, "db: history читает мигрированное сообщение", str(len(hist)))


def test_server_db_cache(db_path: Path) -> None:
    print("=== server: соединение кэшируется на поток ===")
    import server as server_mod
    server_mod.INDEX = str(db_path)
    server_mod._tls.__dict__.pop("db", None)
    constructions: list[str] = []
    orig_db = server_mod.DB

    class CountingDB:
        def __init__(self, path):
            constructions.append(str(path))
            self._real = orig_db(path)

        def __getattr__(self, name):
            return getattr(self._real, name)

    server_mod.DB = CountingDB
    try:
        h1 = server_mod._db()
        h2 = server_mod._db()
        check(h1.db is h2.db, "server: _db отдаёт то же соединение", str(constructions))
        with h1 as db:
            db.chats()
        with h2 as db:
            db.chats()
        check(constructions == [str(db_path)],
              "server: DB() конструируется один раз на поток", str(constructions))
    finally:
        server_mod.DB = orig_db
        server_mod._tls.__dict__.pop("db", None)


def test_extract_all_indexed(db_path: Path) -> None:
    print("=== extract --all-indexed: область — весь индекс ===")
    import extract as extract_mod
    now = int(time.time())

    def seed(path: Path, watch: bool) -> None:
        if path.exists():
            path.unlink()
        with DB(path) as db:
            db.upsert_chat(-100111, "Форум", None, "supergroup", True, 5)
            db.upsert_chat(-100222, "Группа", None, "group", False, 5)
            db.upsert_topic(-100111, 7, "Тема")
            db.upsert_topic(-100111, 8, "Оффтоп")
            if watch:
                db.watch_add(-100111, 7)
            for cid, mid, tid, text in ((-100111, 101, 7, "задача подготовить отчёт"),
                                        (-100111, 102, 8, "решено согласовать смету"),
                                        (-100222, 201, 1, "обещаю прислать акт")):
                ts = now - 60
                db.conn.execute(
                    "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                    "sender_id, sender_name, is_service, text) VALUES(?,?,?,?,?,1,'А',0,?)",
                    (cid, mid, tid, ts,
                     datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
            db.conn.commit()

    ai_db = db_path.parent / "all_indexed.sqlite"
    seed(ai_db, watch=True)
    made = extract_mod.run(str(ai_db), provider=extract_mod.provider_stub)
    scopes = {(m["chat_id"], m["topic_id"]) for m in made}
    check(scopes == {(-100111, 7)},
          "extract: без флага — только watch-темы", str(scopes))

    ai_db2 = db_path.parent / "all_indexed2.sqlite"
    seed(ai_db2, watch=False)
    made2 = extract_mod.run(str(ai_db2), provider=extract_mod.provider_stub, all_indexed=True)
    scopes2 = {(m["chat_id"], m["topic_id"]) for m in made2}
    check({(-100111, 7), (-100111, 8), (-100222, 1)} == scopes2,
          "extract: --all-indexed покрывает все (chat, topic) индекса", str(scopes2))
    with DB(ai_db2) as db:
        ents = db.entities_list()
        t8 = [e for e in ents if e["chat_id"] == -100111 and e["topic_id"] == 8]
        g1 = [e for e in ents if e["chat_id"] == -100222 and e["topic_id"] == 1]
        check(len(t8) == 1 and len(g1) == 1,
              "extract: --all-indexed создаёт сущности вне watch-листа",
              f"t8={len(t8)} g1={len(g1)}")


def test_search_global(db_path: Path) -> None:
    print("=== search: глобальный поиск каналов/групп/юзеров по слову ===")
    import search as search_mod
    client = build_fixture()
    chan = Channel(777, "AI Tools Hub", "ai_tools_hub", forum=False)
    chan.broadcast, chan.megagroup = True, False
    chan.participants_count = 12500
    group_ch = Channel(778, "AI чат", "ai_chat_ru", forum=True)
    group_ch.broadcast, group_ch.megagroup = False, True
    client.global_chats = [chan, group_ch]
    client.global_users = [User(555, "Пётр", "Иванов", "petr_ai")]

    rows = asyncio.run(search_mod.search_global(client, "ИИ"))
    by_id = {r["chat_id"]: r for r in rows}
    check(len(rows) == 3 and {-1000000000777, -1000000000778, 555} <= set(by_id),
          "search: каналы, группы и юзеры одним запросом, id в marked-форме",
          str(sorted(by_id)))
    check(by_id[-1000000000777]["kind"] == "channel"
          and by_id[-1000000000777]["username"] == "ai_tools_hub"
          and by_id[-1000000000777]["participants"] == 12500,
          "search: канал с юзернеймом и подписчиками", str(by_id.get(-1000000000777)))
    check(by_id[-1000000000778]["kind"] == "supergroup" and by_id[555]["kind"] == "user"
          and by_id[555]["username"] == "petr_ai",
          "search: группа и юзер с правильными kind/username",
          f"{by_id.get(-1000000000778)} {by_id.get(555)}")
    check(client.last_global_req is not None and client.last_global_req.q == "ИИ"
          and client.last_global_req.limit == 20,
          "search: запрос идёт с q и limit", str(client.last_global_req and
                                                 (client.last_global_req.q, client.last_global_req.limit)))

    rows_c = asyncio.run(search_mod.search_global(client, "ИИ", kind="channel"))
    check([r["chat_id"] for r in rows_c] == [-1000000000777],
          "search: kind=channel оставляет только каналы-вещания",
          str([r["chat_id"] for r in rows_c]))
    check(client.last_global_req.broadcasts_only is True,
          "search: фильтр типа уходит флагом в запрос", str(client.last_global_req.broadcasts_only))
    rows_u = asyncio.run(search_mod.search_global(client, "ИИ", kind="user"))
    check([r["chat_id"] for r in rows_u] == [555],
          "search: kind=user оставляет только юзеров", str([r["chat_id"] for r in rows_u]))
    try:
        asyncio.run(search_mod.search_global(client, "x", kind="bogus"))
        check(False, "search: неизвестный тип поднимает ValueError")
    except ValueError:
        check(True, "search: неизвестный тип поднимает ValueError")


def test_services_pack(db_path: Path) -> None:
    print("=== services: пакет для сервисов, триалов и промо ===")
    sv_db = db_path.parent / "services_pack.sqlite"
    if sv_db.exists():
        sv_db.unlink()
    now = int(time.time())
    with DB(sv_db) as db:
        db.upsert_chat(-100444, "Сервисы и триалы", None, "supergroup", False, 800)
        db.watch_add(-100444, 1, project="сервисы")
        for mid, text in ((401, "Триал 30 дней на Acme Cloud, acme.cloud/register"),
                          (402, "сервис betaai.com — нейросети для дизайнеров"),
                          (403, "промо: скидка 50% на всё")):
            ts = now - 60
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100444, ?, 1, ?, ?, 1, 'Марат', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
        db.conn.commit()
    import extract as extract_mod
    from packs import PACKS

    def sop(title, msg, quote, hint, kind="trial", due_text=None):
        return {"op": "new", "kind": kind, "title": title, "entity_id": None,
                "body": None, "owner": None, "due_text": due_text, "due_ts": None,
                "confidence": 0.9, "dedup_hint": hint,
                "evidence": [{"chat_id": -100444, "msg_id": msg, "quote": quote}]}

    extract_mod.run(str(sv_db), packs=[PACKS["services"]], provider=lambda p, pk: {
        "ops": [sop("Acme Cloud — триал 30 дней", 401, "Триал 30 дней",
                    "acme.cloud", due_text="30 дней")],
        "covered_upto_msg_id": 401})
    with DB(sv_db) as db:
        n = db.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE pack='services'").fetchone()["c"]
        e = db.conn.execute("SELECT * FROM entities WHERE pack='services'").fetchone()
        check(n == 1 and e["kind"] == "trial" and e["title"].startswith("Acme Cloud")
              and e["attrs_json"] and json.loads(e["attrs_json"])["due_text"] == "30 дней",
              "services: триал с kind/title/due_text через dedup_hint-схему",
              f"n={n} kind={e and e['kind']} attrs={e and e['attrs_json']}")

    extract_mod.run(str(sv_db), packs=[PACKS["services"]], provider=lambda p, pk: {
        "ops": [sop("Триал на Acme Cloud", 402, "сервис betaai.com", " Acme.Cloud ")],
        "covered_upto_msg_id": 402})
    with DB(sv_db) as db:
        n = db.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE pack='services'").fetchone()["c"]
        evd = db.conn.execute(
            "SELECT * FROM entity_events WHERE op='update' AND field='dedup'").fetchone()
        check(n == 1 and evd is not None,
              "services: другая формулировка с тем же hint — дубликат, не сущность",
              f"n={n}")

    with DB(sv_db) as db:
        for mid, text in ((404, "сервис gamma.io с генерацией видео"),
                          (405, "триал 7 дней на delta.ai")):
            ts = now - 30
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100444, ?, 1, ?, ?, 1, 'Марат', 0, ?)",
                (mid, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
        db.conn.commit()
    extract_mod.run(str(sv_db), packs=[PACKS["services"]],
                    provider=extract_mod.provider_stub)
    with DB(sv_db) as db:
        ks = {r["kind"] for r in db.conn.execute(
            "SELECT kind FROM entities WHERE pack='services'")}
        check({"service", "trial", "promo"} <= ks,
              "services: stub-провайдер создаёт все три вида", str(ks))

    made = extract_mod.run(str(sv_db), packs=[PACKS["services"]],
                           provider=extract_mod.provider_stub)
    check(made == [], "services: повторный прогон без новых сообщений пуст", str(made))


def test_pipeline(db_path: Path) -> None:
    print("=== pipeline: sync -> extract -> digest -> monitor ===")
    import pipeline as pipeline_mod
    pl_db = db_path.parent / "pipeline.sqlite"
    if pl_db.exists():
        pl_db.unlink()
    with DB(pl_db) as db:
        db.watch_add(-100222, 1)
        db.watch_add(-100333, 1)
    client = build_fixture()

    rep = asyncio.run(pipeline_mod.run(str(pl_db), client=client))
    check(rep["sync"]["ok"] and rep["sync"]["new_messages"] == 10 and rep["sync"]["chats"] == 2,
          "pipeline: sync по watch-листу синхронизировал обе группы",
          str(rep["sync"]))
    check(rep["extract"]["ok"] and rep["extract"]["areas"] > 0,
          "pipeline: extract отработал по свежим сообщениям", str(rep["extract"]))
    check(rep["digest"]["ok"] and rep["digest"]["made"] == 2,
          "pipeline: digest собран по каждой watch-теме", str(rep["digest"]))
    check(rep["monitor"]["ok"] and len(rep["monitor"]["lines"]) >= 1,
          "pipeline: monitor дал сводку", str(rep["monitor"]["lines"])[:80])
    with DB(pl_db) as db:
        n_msgs = db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_dig = db.conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    check(n_msgs == 10 and n_dig == 2,
          "pipeline: данные в базе после одного прогона", f"msgs={n_msgs} digests={n_dig}")

    rep2 = asyncio.run(pipeline_mod.run(str(pl_db), client=client))
    check(rep2["sync"]["ok"] and rep2["sync"]["new_messages"] == 0
          and rep2["digest"]["made"] == 0 and rep2["extract"]["areas"] == 0,
          "pipeline: второй прогон инкрементален",
          f"sync={rep2['sync']} digest={rep2['digest']} extract={rep2['extract']}")

    rep3 = asyncio.run(pipeline_mod.run(str(pl_db), client=None, skip_sync=True))
    check("sync" not in rep3 and rep3["extract"]["ok"] and rep3["monitor"]["ok"],
          "pipeline: --skip sync гоняет офлайн-шаги без Telegram",
          str(sorted(rep3)))


def test_extract_until_done(db_path: Path) -> None:
    print("=== extract --until-done: дожёвывает хвост раундами ===")
    ud_db = db_path.parent / "until_done.sqlite"
    if ud_db.exists():
        ud_db.unlink()
    now = int(time.time())
    with DB(ud_db) as db:
        db.upsert_chat(-100555, "Поток", None, "supergroup", False, 3)
        db.watch_add(-100555, 1, project="сервисы")
        for i in range(1, 10):
            ts = now - 600 + i
            db.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                "sender_id, sender_name, is_service, text) "
                "VALUES(-100555, ?, 1, ?, ?, 1, 'А', 0, ?)",
                (i, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                 f"сервис demo{i}.com — раздача номер {i}"))
        db.conn.commit()
    import extract as extract_mod
    saved = extract_mod.MAX_MESSAGES_PER_RUN
    extract_mod.MAX_MESSAGES_PER_RUN = 2
    try:
        made = extract_mod.run(str(ud_db), pack_id="services",
                               provider=extract_mod.provider_stub, until_done=True)
    finally:
        extract_mod.MAX_MESSAGES_PER_RUN = saved
    check(sum(m["messages"] for m in made) == 9 and len(made) == 5,
          "extract --until-done: 5 раундов по 2 сообщения, всё дочитано",
          str([(m["messages"], m["ops_applied"]) for m in made]))
    with DB(ud_db) as db:
        n = db.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE pack='services'").fetchone()["c"]
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key='extract:-100555:1:services'").fetchone()
    check(n == 9 and cur is not None and int(cur["value"]) == 9,
          "extract --until-done: 9 сущностей, курсор на последнем сообщении",
          f"ents={n} cursor={cur and cur['value']}")
    made2 = extract_mod.run(str(ud_db), pack_id="services",
                            provider=extract_mod.provider_stub, until_done=True)
    check(made2 == [], "extract --until-done: повторный прогон пуст", str(made2))


def test_watch_suggest(db_path: Path) -> None:
    print("=== watch suggest: топ-тем с готовыми командами ===")
    import argparse
    import io
    from contextlib import redirect_stdout
    sg_db = db_path.parent / "suggest.sqlite"
    if sg_db.exists():
        sg_db.unlink()
    with DB(sg_db) as db:
        db.upsert_chat(-100501, "Форум", "team_forum", "supergroup", True, 5)
        db.upsert_topic(-100501, 7, "Релизы")
        db.upsert_topic(-100501, 8, "Мемы")
        db.watch_add(-100501, 7, alias="релизы")
        for tid, n in ((7, 300), (8, 120)):
            for i in range(n):
                ts = 1700000000 + i
                db.conn.execute(
                    "INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) "
                    "VALUES(-100501, ?, ?, ?, 'x')", (tid * 1000 + i, tid, ts))
        db.recount_topics(-100501)
        db.conn.commit()
    import watch as watch_mod
    buf = io.StringIO()
    with redirect_stdout(buf):
        watch_mod.cmd_suggest(argparse.Namespace(
            db=str(sg_db), top=5, min_messages=50))
    out = buf.getvalue()
    check("watch.py add --chat team_forum --topic 8" in out
          and "Мемы" in out,
          "watch suggest: непросматриваемая тема с готовой командой", out[-200:])
    check("--topic 7" not in out,
          "watch suggest: уже отмеченная тема не предлагается", out[-200:])


def test_llm_extra_body(db_path: Path) -> None:
    print("=== llm_http: dashscope отключает thinking в payload ===")
    from llm_http import extra_body
    check(extra_body("https://dashscope.aliyuncs.com/compatible-mode/v1")
          == {"enable_thinking": False},
          "llm_http: dashscope URL глушит thinking", str(extra_body("dashscope")))
    check(extra_body("https://api.openai.com/v1") == {} and extra_body(None) == {},
          "llm_http: прочие провайдеры не трогаются", str(extra_body("openai")))
    from llm_http import call_with_deadline
    check(call_with_deadline(lambda x: x * 2, 5, 21) == 42,
          "llm_http: быстрый вызов возвращает результат")
    try:
        call_with_deadline(time.sleep, 0.2, 5)
        check(False, "llm_http: дедлайн прерывает зависший вызов")
    except TimeoutError:
        check(True, "llm_http: дедлайн прерывает зависший вызов")

    def boom() -> None:
        raise ValueError("провайдер упал")

    try:
        call_with_deadline(boom, 5)
        check(False, "llm_http: исключение провайдера пробрасывается вызывающему")
    except ValueError:
        check(True, "llm_http: исключение провайдера пробрасывается вызывающему")
    import extract as extract_mod
    from packs import PACKS
    calls: list[dict] = []
    orig_post = extract_mod._post
    saved = {k: os.environ.get(k) for k in ("EXTRACT_URL", "EXTRACT_KEY", "EXTRACT_MODEL")}
    os.environ["EXTRACT_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    os.environ.pop("EXTRACT_KEY", None)
    os.environ.pop("EXTRACT_MODEL", None)

    def fake_post(url, key, payload):
        calls.append(payload)
        return {"choices": [{"message": {
            "content": '{"ops": [], "covered_upto_msg_id": 5}'},
            "finish_reason": "stop"}]}

    extract_mod._post = fake_post
    try:
        extract_mod.provider_openai("промпт", PACKS["services"])
        check(calls and calls[0].get("enable_thinking") is False,
              "extract: provider_openai кладёт enable_thinking=False для dashscope",
              str({k: v for k, v in calls[0].items() if k != "messages"})[:120])
    finally:
        extract_mod._post = orig_post
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_extract_parallel(db_path: Path) -> None:
    print("=== extract: параллельные воркеры обрабатывают темы одновременно ===")
    par_db = db_path.parent / "parallel.sqlite"
    if par_db.exists():
        par_db.unlink()
    now = int(time.time())
    with DB(par_db) as db:
        for cid, alias in ((-100701, "альфа"), (-100702, "бета")):
            db.upsert_chat(cid, f"Чат {alias}", None, "supergroup", False, 3)
            db.watch_add(cid, 1, project="сервисы")
            for i in range(1, 4):
                ts = now - 60 + i
                db.conn.execute(
                    "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
                    "sender_id, sender_name, is_service, text) "
                    "VALUES(?, ?, 1, ?, ?, 1, 'А', 0, ?)",
                    (cid, i, ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                     f"сервис {alias}{i}.com — раздача"))
        db.conn.commit()
    import extract as extract_mod
    made = extract_mod.run(str(par_db), pack_id="services",
                           provider=extract_mod.provider_stub,
                           until_done=True, workers=2)
    chats = sorted(m["chat_id"] for m in made)
    check(chats == [-100702, -100701],
          "extract: оба чата обработаны параллельно", str(chats))
    with DB(par_db) as db:
        n = db.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE pack='services'").fetchone()["c"]
        by_chat = dict(db.conn.execute(
            "SELECT chat_id, COUNT(*) FROM entities WHERE pack='services' GROUP BY chat_id").fetchall())
    check(n == 6 and by_chat == {-100701: 3, -100702: 3},
          "extract: сущности из обоих параллельных скоупов", str(by_chat))


def main() -> int:
    import tempfile
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    db_path = Path(tempfile.gettempdir()) / "tg_agent_test" / "tg_index.sqlite"
    if db_path.parent.exists():
        for f in db_path.parent.iterdir():
            f.unlink()
    test_sync(db_path)
    test_folder_and_backfill(db_path)
    test_folder_fallback(db_path)
    test_folder_edit(db_path)
    test_backfill_resume(db_path)
    test_marked_ids(db_path)
    test_reads(db_path)
    test_watch(db_path)
    test_entities_schema(db_path)
    test_extract_stub(db_path)
    test_extract_validation(db_path)
    test_entities_mcp(db_path)
    test_monitor_notify(db_path)
    test_media(db_path)
    test_export(db_path)
    test_live_and_poll(db_path)
    test_gates(db_path)
    test_strict_and_doctor(db_path)
    test_live_tools_flag(db_path)
    test_mcp_server(db_path)
    test_ui_security(db_path)
    test_live_collect(db_path)
    test_digest_resilience(db_path)
    test_tg_new_fit_honest(db_path)
    test_marked_migration(db_path)
    test_build_state_last(db_path)
    test_env_loader(db_path)
    test_login_2fa(db_path)
    test_start_kwargs(db_path)
    test_days_window(db_path)
    test_start_wizard(db_path)
    test_topic_norm(db_path)
    test_server_db_cache(db_path)
    test_extract_all_indexed(db_path)
    test_search_global(db_path)
    test_services_pack(db_path)
    test_pipeline(db_path)
    test_extract_until_done(db_path)
    test_watch_suggest(db_path)
    test_llm_extra_body(db_path)
    test_extract_parallel(db_path)
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
