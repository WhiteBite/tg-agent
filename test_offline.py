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
        for m in msgs:
            yield m

    async def get_messages(self, entity, limit=None, filter=None, **kw):
        msgs = self._group(entity)["messages"]
        if filter is not None:  # InputMessagesFilterPinned
            msgs = [m for m in msgs if getattr(m, "pinned", False)]
        msgs = sorted(msgs, key=lambda m: m.id, reverse=True)
        return msgs[:limit] if limit else msgs

    async def iter_dialogs(self, folder=None, **kw):
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

def test_folder_and_backfill(db_path: Path) -> None:
    client = build_fixture()

    r = asyncio.run(sync_mod.run(db_path=str(db_path), client=client, folder=7))
    ids = {c["chat_id"] for c in r["chats"]}
    check(ids == {-100111, -100333},
          "sync --folder: взяты только чаты из папки (форум + канал)", str(ids))
    news = next(c for c in r["chats"] if c["chat_id"] == -100333)
    check(news["new_messages"] == 3, "sync --folder: канал-вещание синхронизирован",
          f"new={news['new_messages']}")

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
                    "tg_digests"}
        check(expected <= names and len(names) == 10,
              "MCP tools/list: все 10 инструментов на месте",
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
        check({"tg_live_topics", "tg_live_history"} <= names,
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
        rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        alive = wait_for(proc, lambda m: m.get("id") == 4)
        check(alive.get("id") == 4, "MCP: сервер жив после ошибки live-инструмента")
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
    env2 = {**os.environ, "TG_DB": str(db_path), "PORT": str(port), "HOST": "0.0.0.0"}
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
        check("login.py" in str(exc), "make_client: без ключей отсылает к login.py", str(exc))


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
    test_backfill_resume(db_path)
    test_marked_ids(db_path)
    test_reads(db_path)
    test_watch(db_path)
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
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
