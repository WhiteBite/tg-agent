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
    def __init__(self, reply_to_msg_id: int, forum_topic: bool = False):
        self.reply_to_msg_id = reply_to_msg_id
        self.forum_topic = forum_topic


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
        self.title = TopicTitle(title)  # TextWithEntities, как в настоящем ответе
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

    async def get_dialogs(self):
        return [Dialog(g["entity"], g["entity"].title) for g in self.groups.values()]

    async def get_entity(self, peer):
        return self.groups[int(peer)]["entity"]

    async def iter_messages(self, entity, limit=None, reply_to=None,
                            offset_id=0, reverse=False, **kw):
        if self.flood_once:  # симуляция лимита Telegram: первый проход падает
            self.flood_once = False
            from telethon.errors import FloodWaitError
            e = FloodWaitError(None, 0)
            e.seconds = 0
            raise e
        msgs = self.groups[int(entity)]["messages"]
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
        cid = int(getattr(entity, "id", entity))
        msgs = self.groups[cid]["messages"]
        if filter is not None:  # InputMessagesFilterPinned
            msgs = [m for m in msgs if getattr(m, "pinned", False)]
        msgs = sorted(msgs, key=lambda m: m.id, reverse=True)
        return msgs[:limit] if limit else msgs

    async def iter_dialogs(self, folder=None, **kw):
        assert folder is not None, "iter_dialogs без folder не должен вызываться"
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

    # тема определяется по сообщению
    m = client.groups[-100111]["messages"][2]
    check(sync_mod.topic_of_message(m) == 5, "topic_of_message: сообщение в теме «Релизы» -> 5")
    m15 = next(m for m in client.groups[-100111]["messages"] if m.id == 15)
    check(sync_mod.topic_of_message(m15) is None,
          "topic_of_message: сообщение без темы -> None")
    check(sync_mod.topic_of_message(client.groups[-100111]["messages"][0]) == 5,
          "topic_of_message: service TopicCreated -> id самой темы")
    check(sync_mod.is_group(client.groups[-100333]["entity"]) is False,
          "is_group: канал-вещание НЕ группа (берётся только если назван явно)")

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

        # сырой текст legacy-строк маскируется при выдаче
        db.conn.execute(
            "INSERT OR IGNORE INTO messages(chat_id, msg_id, topic_id, ts, text) "
            "VALUES(-100222, 999, 1, 1, 'legacy ghp_" + "c" * 40 + "')")
        db.conn.commit()
        leg = run_sql(db, "SELECT text FROM messages WHERE chat_id=-100222 AND msg_id=999")
        check(leg["ok"] and "ghp_" not in leg["rows"][0]["text"],
              "run_sql: выдача маскирует сырой текст", repr(leg["rows"][0]["text"][:50]))
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
    test_reads(db_path)
    test_watch(db_path)
    test_live_and_poll(db_path)
    test_gates(db_path)
    test_strict_and_doctor(db_path)
    test_mcp_server(db_path)
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
