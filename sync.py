#!/usr/bin/env python3
"""Синхронизация Telegram-групп и их тем (форумов) в локальный SQLite-индекс.

    python3 sync.py                       # все группы из диалогов
    python3 sync.py --chat mygroup --chat -1001234567890
    python3 sync.py --pattern "работа|команда"
    python3 sync.py --days 7 --topics     # окно 7 дней, только темы
    python3 sync.py --dry-run             # показать, что нашлось, ничего не писать

Инкрементально: для каждой темы запоминается курсор (id последнего сообщения),
повторный запуск тянет только новое. Курсоры лежат в таблице meta той же БД.

Скрипт ничего не отправляет в Telegram — только читает.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import DB, clean_text, extract_refs
from sanitize import sanitize_name
from env import load_env
import media as media_mod

load_env()

try:  # Telethon нужен только для реального прогона, не для офлайн-тестов
    from telethon import TelegramClient
    from telethon.tl import functions
    from telethon.tl.types import MessageService
    from telethon.sessions import StringSession
    HAVE_TELETHON = True
except ImportError:  # pragma: no cover
    HAVE_TELETHON = False


MEDIA_LABELS = {
    "MessageMediaPhoto": "photo",
    "MessageMediaDocument": "document",
    "MessageMediaWebPage": "link",
    "MessageMediaContact": "contact",
    "MessageMediaPoll": "poll",
    "MessageMediaGeo": "geo",
    "MessageMediaDice": "dice",
    "MessageMediaInvoice": "invoice",
}


def _label(obj: Any) -> str:
    return type(obj).__name__


def is_group(dialog_or_chat: Any) -> bool:
    """True для обычных групп и супергрупп (каналы-вещания и забаненные — нет)."""
    chat = getattr(dialog_or_chat, "entity", dialog_or_chat)
    if chat is None:
        return False
    name = _label(chat)
    if name in ("Chat",):  # обычная группа
        return True
    if name in ("ChatForbidden", "ChannelForbidden"):
        return False
    # супергруппа: megagroup и не broadcast-канал
    return bool(getattr(chat, "megagroup", False)) and not bool(getattr(chat, "broadcast", False))


def is_forum(chat: Any) -> bool:
    return bool(getattr(chat, "forum", False))


def kind_of(chat: Any) -> str:
    name = _label(chat)
    if name == "Chat":
        return "group"
    if name == "Channel":
        if getattr(chat, "broadcast", False):
            return "channel"
        return "supergroup"
    return name.lower()


try:  # телега не обязательна в офлайн-тестах
    from telethon.errors import FloodWaitError as _FloodWait
    from telethon.errors import FolderIdInvalidError as _FolderIdInvalid
except Exception:  # pragma: no cover
    class _FloodWait(Exception):  # type: ignore[no-redef]
        pass

    class _FolderIdInvalid(Exception):  # type: ignore[no-redef]
        pass


async def _flood_sleep(exc: Exception, attempt: int) -> None:
    wait = min(int(getattr(exc, "seconds", 30) or 30) + 1, 900)
    print(f"    FloodWait: сплю {wait}s (попытка {attempt}/3)…", flush=True)
    await asyncio.sleep(wait)


def start_kwargs() -> dict[str, str]:
    """Telethon.start(phone=None) падает сразу; без TELEGRAM_PHONE пусть спросит сам."""
    phone = os.environ.get("TELEGRAM_PHONE")
    return {"phone": phone} if phone else {}


def make_client() -> Any:
    """TelegramClient из .env, без подключения."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("нет TELEGRAM_API_ID / TELEGRAM_API_HASH — "
                         "запусти python start.py: спросит один раз и запомнит")
    sess = os.environ.get("TELEGRAM_SESSION_STRING")
    name = os.environ.get("TELEGRAM_SESSION_NAME", "tg_agent_session")
    client = TelegramClient(StringSession(sess) if sess else name, int(api_id), api_hash,
                            receive_updates=False)
    try:
        # общий .session бывает занят живым MCP-клиентом — пусть SQLite ждёт, а не падает
        client.session._conn.execute("PRAGMA busy_timeout=30000")
    except AttributeError:  # StringSession — нечего настраивать
        pass
    return client


async def list_chat_choices(client: Any) -> list[dict]:
    """Чаты для визарда: группы (в т.ч. форумы) и каналы, свежие сверху."""
    out: list[dict] = []
    async for d in client.iter_dialogs():
        e = getattr(d, "entity", d)
        if _label(e) in ("ChatForbidden", "ChannelForbidden"):
            continue
        if getattr(e, "broadcast", False):
            kind = "channel"
        elif is_group(e):
            kind = "forum" if is_forum(e) else "group"
        else:
            continue
        out.append({
            "id": marked_id(e),
            "title": getattr(e, "title", None) or getattr(d, "name", None),
            "username": getattr(e, "username", None),
            "kind": kind,
        })
    return out


def topic_of_message(msg: Any) -> Optional[int]:
    """Определяет id темы сообщения.

    Telegram хранит темы как треды: у сообщения внутри темы reply_to ведёт на
    id темы (reply_to.top_msg_id при forum_topic=True). У ответа на сообщение
    ВНУТРИ темы адресат лежит в reply_to_msg_id, а корень темы — в reply_to_top_id,
    поэтому сначала смотрим top_id. У сервисного сообщения «тема создана» тема —
    это его собственный id.
    """
    reply_to = getattr(msg, "reply_to", None)
    if reply_to is not None and getattr(reply_to, "forum_topic", False):
        tid = (getattr(reply_to, "reply_to_top_id", None)
               or getattr(reply_to, "reply_to_msg_id", None))
        if tid:
            return int(tid)
    action = getattr(msg, "action", None)
    if action is not None and _label(action) == "MessageActionTopicCreated":
        return int(msg.id)
    if reply_to is not None:
        # обычные ответы внутри группы без тем — темы нет
        return None
    return None


def sender_name(msg: Any, client: Any) -> Optional[str]:
    """Имя автора из кэша Telethon рядом с сообщением; сети не зовём."""
    if getattr(msg, "out", False):
        return "me"
    if not getattr(msg, "sender_id", None):
        return None
    try:
        sender = msg._sender  # type: ignore[attr-defined]
    except Exception:
        sender = None
    if sender is None:
        return f"id{msg.sender_id}"
    parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
    name = " ".join(p for p in parts if p)
    return name or getattr(sender, "username", None) or f"id{msg.sender_id}"


def marked_id(entity: Any) -> int:
    """id в marked-форме Telegram: канал -100…, старая группа -N, юзер +N.
    Telethon отдаёт entity.id в сыром виде, а итераторы/события ждут marked."""
    eid = int(getattr(entity, "id", entity))
    if eid < 0:
        return eid
    label = _label(entity)
    if label == "Channel":
        return -1000000000000 - eid
    if label == "Chat":
        return -eid
    return eid


def media_of(msg: Any) -> Optional[str]:
    media = getattr(msg, "media", None)
    if media is None:
        return None
    return MEDIA_LABELS.get(_label(media), _label(media).replace("MessageMedia", "").lower())


def message_row(msg: Any, chat_id: int, topic_id: Optional[int], client: Any) -> dict[str, Any]:
    date = getattr(msg, "date", None)
    ts = int(date.timestamp()) if date is not None else None
    # сервисное сообщение: у него есть action и нет текста
    # (в Telethon это MessageService; проверяем по сути, а не по имени класса)
    service = getattr(msg, "action", None) is not None
    # msg.reactions — объект ReactionCounts с .results (список ReactionCount)
    reactions = []
    rc = getattr(msg, "reactions", None)
    for c in getattr(rc, "results", None) or []:
        reactions.append(
            {
                "emoji": getattr(getattr(c, "reaction", None), "emoticon", None),
                "count": getattr(c, "count", None),
            }
        )
    text = getattr(msg, "message", None)
    if text is None and service:
        text = f"[service:{_label(getattr(msg, 'action', None) or '')}]"
    if text:
        text = clean_text(text)
    name = sender_name(msg, client)
    if name:
        name = sanitize_name(name)
    fwd = getattr(msg, "fwd_from", None)
    fwd_cid = None
    if fwd is not None:
        fid = getattr(fwd, "from_id", None)
        fwd_cid = getattr(fid, "channel_id", None) or getattr(fid, "user_id", None)
    edit_date = getattr(msg, "edit_date", None)
    return {
        "chat_id": chat_id,
        "msg_id": int(msg.id),
        "topic_id": topic_id,
        "ts": ts,
        "date_iso": date.isoformat() if date is not None else None,
        "sender_id": getattr(msg, "sender_id", None),
        "sender_name": name,
        "is_out": bool(getattr(msg, "out", False)),
        "is_service": service,
        "text": text,
        "media": media_of(msg),
        "reply_to": getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "reactions": reactions,
        "fwd_from": fwd_cid,
        "edited_ts": int(edit_date.timestamp()) if edit_date is not None else None,
        "deleted": False,
    }


async def list_topics(client: Any, entity: Any) -> list[dict]:
    """Список тем форума через messages.GetForumTopics (в Telethon нет обёртки)."""
    topics: list[dict] = []
    offset_date: Optional[datetime] = None
    offset_id = offset_topic = 0
    while True:
        res = await client(
            functions.messages.GetForumTopicsRequest(
                peer=entity,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=100,
            )
        )
        got = list(getattr(res, "topics", None) or [])
        for t in got:
            # ForumTopic.title в Telethon — строка; TextWithEntities держим на всякий
            title = getattr(t, "title", None)
            if title is not None and not isinstance(title, str):
                title = getattr(title, "text", None)
            topics.append(
                {
                    "topic_id": int(t.id),
                    "title": title or str(t.id),
                    "closed": bool(getattr(t, "closed", False)),
                    "top_message": int(getattr(t, "top_message", 0) or 0),
                }
            )
        if len(got) < 100:
            break
        last = got[-1]
        offset_topic = int(last.id)
        offset_id = int(getattr(last, "top_message", 0) or 0)
        notify = getattr(last, "notify_date", None)
        offset_date = (
            datetime.fromtimestamp(int(notify), tz=timezone.utc)
            if isinstance(notify, (int, float))
            else (notify if isinstance(notify, datetime) else None)
        )
        await asyncio.sleep(0.4)
    return topics


async def pull_chat(
    client: Any,
    db: DB,
    chat_id: int,
    *,
    forum: bool,
    limit: Optional[int],
    min_id: int,
    since_ts: Optional[int],
    dry_run: bool,
) -> dict:
    """Один проход по группе сверху вниз; тема определяется по каждому сообщению.

    Почему одним проходом: в форуме сообщения General не имеют reply_to, поэтому
    по темам через iter_messages(reply_to=...) они недостижимы. Проход по всей
    группе с разбиением по reply_to.top_msg_id ловит и темы, и General.

    since_ts — граница окна (--days): идём от свежих вниз и останавливаемся,
    как только сообщение старше границы.
    """
    attempt = 0
    total_new = 0
    total_edited = 0
    total_per_topic: dict[int, int] = {}
    while True:  # FloodWait: повторяем проход; вставки идемпотентны
        new = seen = edited = 0
        max_id = min_id
        per_topic: dict[int, int] = {}
        try:
            async for msg in client.iter_messages(chat_id, limit=limit):
                if since_ts and msg.date is not None and int(msg.date.timestamp()) < since_ts:
                    break
                if msg.id <= min_id:
                    break
                seen += 1
                tid = topic_of_message(msg) if forum else None
                if tid is None:
                    tid = 1  # General: и в форуме (сообщения без reply_to), и в группе без тем
                if dry_run:
                    per_topic[tid] = per_topic.get(tid, 0) + 1
                    max_id = max(max_id, msg.id)
                    continue
                row = message_row(msg, chat_id, tid, client)
                fresh = db.insert_message(row)
                if fresh:
                    new += 1
                    per_topic[tid] = per_topic.get(tid, 0) + 1
                elif row["edited_ts"] is not None:
                    # строка уже есть (обычно вставлена live.py) — подтягиваем правку
                    fresh = db.update_message(row)
                    if fresh:
                        edited += 1
                if fresh:
                    db.insert_refs(chat_id, row["msg_id"],
                                   extract_refs(row["text"], row["fwd_from"]))
                    if media_mod.is_transcribable(msg):
                        media_mod.enqueue(db, chat_id, row["msg_id"], row["media"])
                max_id = max(max_id, msg.id)
            break
        except _FloodWait as exc:
            # вставленные до FloodWait строки в ретрае вернут False — считаем их сразу
            total_new += new
            total_edited += edited
            for tid, cnt in per_topic.items():
                total_per_topic[tid] = total_per_topic.get(tid, 0) + cnt
            attempt += 1
            if attempt > 3:
                raise
            await _flood_sleep(exc, attempt)
    total_new += new
    total_edited += edited
    for tid, cnt in per_topic.items():
        total_per_topic[tid] = total_per_topic.get(tid, 0) + cnt
    return {"new": total_new, "edited": total_edited, "seen": seen,
            "max_id": max_id, "per_topic": total_per_topic}


def _peer_marked(p: Any) -> Optional[int]:
    """PeerChannel/PeerChat/PeerUser -> «помеченный» id как в диалогах."""
    ch = getattr(p, "channel_id", None)
    if ch is not None:
        return -1000000000000 - int(ch)
    ct = getattr(p, "chat_id", None)
    if ct is not None:
        return -int(ct)
    us = getattr(p, "user_id", None)
    if us is not None:
        return int(us)
    return None


async def fetch_folders(client: Any) -> list[dict]:
    """Папки аккаунта: messages.GetDialogFiltersRequest; состав — include_peers.

    Схема вызова — как в chigwell/telegram-mcp (folders.py), но берём только чтение.
    Слой 229+ заворачивает ответ в объект DialogFilters (список в .filters), а
    title папки — TextWithEntities; старые Telethon и фейки отдают плоский список."""
    from telethon.tl import functions
    res = await client(functions.messages.GetDialogFiltersRequest())
    rows = getattr(res, "filters", None)
    if rows is None:
        rows = res or []
    out: list[dict] = []
    for f in rows:
        fid = getattr(f, "id", None)
        title = getattr(f, "title", None)
        if title is not None and not isinstance(title, str):
            title = getattr(title, "text", None)
        if fid is None or not title:
            continue
        peers = [m for m in (_peer_marked(p) for p in (getattr(f, "include_peers", None) or [])) if m]
        out.append({"id": int(fid), "title": str(title), "peers": peers})
    return out


async def fetch_pins(client: Any, entity: Any) -> list[Any]:
    """Закреплённые сообщения: search-фильтр pinned, с фолбэком по флагу.

    Паттерн — из chigwell/telegram-mcp (get_pinned_messages)."""
    try:
        from telethon.tl.types import InputMessagesFilterPinned as _PF
        msgs = await client.get_messages(entity, filter=_PF())
    except Exception:
        msgs = [m for m in await client.get_messages(entity, limit=50)
                if getattr(m, "pinned", False)]
    return msgs or []


async def sync_chat(
    client: Any,
    db: DB,
    dialog_or_chat: Any,
    *,
    limit: Optional[int],
    days: Optional[int],
    dry_run: bool,
) -> dict:
    entity = getattr(dialog_or_chat, "entity", dialog_or_chat)
    chat_id = marked_id(entity)
    forum = is_forum(entity)
    title = getattr(entity, "title", None) or getattr(dialog_or_chat, "name", None)

    db.upsert_chat(
        chat_id=chat_id,
        title=title or "",
        username=getattr(entity, "username", None),
        kind=kind_of(entity),
        is_forum=forum,
        members=getattr(entity, "participants_count", None),
    )

    since_ts = int(time.time()) - days * 86400 if days else None
    titles: dict[int, str] = {}
    if forum:
        for t in await list_topics(client, entity):
            titles[t["topic_id"]] = t["title"]
            db.upsert_topic(chat_id, t["topic_id"], t["title"], t["closed"])
        titles.setdefault(1, "General")
        if not dry_run:
            db.upsert_topic(chat_id, 1, "General")

    min_id = db.get_cursor(chat_id, 0)
    res = await pull_chat(
        client, db, chat_id,
        forum=forum, limit=limit, min_id=min_id, since_ts=since_ts, dry_run=dry_run,
    )
    report: dict[str, Any] = {
        "chat_id": chat_id,
        "title": title,
        "is_forum": forum,
        "new_messages": res["new"],
        "edited_messages": res["edited"],
        "seen_messages": res["seen"],
        "topics": [
            {
                "topic_id": tid,
                "title": titles.get(tid) if tid is not None else None,
                "new": n,
            }
            for tid, n in sorted(res["per_topic"].items(), key=lambda kv: -kv[1])
        ],
    }
    if not dry_run and res["max_id"] > min_id:
        db.set_cursor(chat_id, 0, res["max_id"])
    if not dry_run:
        db.recount_topics(chat_id, general_id=1)
        for m in await fetch_pins(client, entity):
            tid = topic_of_message(m) if forum else None
            if tid is None:
                tid = 1
            row = message_row(m, chat_id, tid, client)
            db.insert_pin(chat_id, int(m.id), tid, row["ts"],
                          row["sender_name"], row["text"])
    return report



async def backfill(
    client: Any,
    db: DB,
    chat_id: int,
    *,
    forum: bool,
    start_id: int = 0,
    chunk_size: int = 3000,
    sleep: float = 0.5,
    max_messages: Optional[int] = None,
    dry_run: bool = False,
    entity: Any = None,
) -> dict:
    """Досасывает историю снизу вверх (от старых к новым) чанками.

    Обычная синхронизация идёт сверху вниз и обрывается на первом уже известном
    сообщении — для канала с историей в несколько лет это значит «никогда не
    дойти до конца». Здесь наоборот: курсор start_id двигается вперёд, прогресс
    пишется в БД после каждого чанка (meta:backfill:<chat_id>), поэтому
    прерванный запуск продолжается с того же места.
    """
    total_new = total_seen = 0
    if start_id == 0:
        saved = db.get_meta(f"backfill:{chat_id}")
        start_id = int(saved) if saved else 0
    cursor = start_id
    if entity is None:
        entity = await client.get_entity(chat_id)
    if not dry_run:
        # без этого группа была бы невидима в tg_index_stats / watch.py list
        db.upsert_chat(
            chat_id=chat_id,
            title=getattr(entity, "title", "") or "",
            username=getattr(entity, "username", None),
            kind=kind_of(entity),
            is_forum=forum,
            members=getattr(entity, "participants_count", None),
        )
    while True:
        attempt = 0
        rows: list[dict[str, Any]] = []
        audio_ids: set[int] = set()
        while True:  # FloodWait: повторяем тот же чанк (вставки идемпотентны)
            chunk_seen = 0
            chunk_max = cursor
            rows = []
            audio_ids = set()
            try:
                async for msg in client.iter_messages(
                    chat_id, limit=chunk_size, offset_id=cursor, reverse=True
                ):
                    chunk_seen += 1
                    tid = topic_of_message(msg) if forum else None
                    if tid is None:
                        tid = 1
                    if not dry_run:
                        rows.append(message_row(msg, chat_id, tid, client))
                        if media_mod.is_transcribable(msg):
                            audio_ids.add(int(msg.id))
                    chunk_max = max(chunk_max, msg.id)
                    if max_messages and total_seen + chunk_seen >= max_messages:
                        break
                break
            except _FloodWait as exc:
                attempt += 1
                if attempt > 3:
                    raise
                await _flood_sleep(exc, attempt)
        chunk_new = 0
        if not dry_run:
            chunk_new = db.insert_messages(rows)
            for row in rows:
                db.insert_refs(chat_id, row["msg_id"],
                               extract_refs(row["text"], row["fwd_from"]))
            for row in rows:
                if row["msg_id"] in audio_ids:
                    media_mod.enqueue(db, chat_id, row["msg_id"], row["media"])
            for row in rows:
                if row["edited_ts"] is not None and db.update_message(row):
                    db.insert_refs(chat_id, row["msg_id"],
                                   extract_refs(row["text"], row["fwd_from"]))
            db.set_meta(f"backfill:{chat_id}", str(chunk_max))
            db.conn.commit()
        total_new += chunk_new
        total_seen += chunk_seen
        print(f"    чанк {cursor}->{chunk_max}: просмотрено {chunk_seen}, новых {chunk_new}",
              flush=True)
        if chunk_seen == 0 or chunk_max <= cursor:
            break
        cursor = chunk_max
        if max_messages and total_seen >= max_messages:
            break
        if sleep:
            await asyncio.sleep(sleep)
    if not dry_run:
        db.recount_topics(chat_id, general_id=1)
        db.set_cursor(chat_id, 0, max(cursor, db.get_cursor(chat_id, 0)))
    return {"new": total_new, "seen": total_seen, "cursor": cursor}


async def backfill_window(
    client: Any,
    db: DB,
    days: int,
    dry_run: bool = False,
) -> dict:
    """Досинк истории за окно N дней по всем чатам индекса (date-режим).

    iter_messages(offset_date=..., reverse=True) — только offset_date, любой
    ненулевой offset_id молча его перекрывает; limit=None держит автоматический
    пейсинг 1с (иначе FloodWait-шторм). Граница exclusive — +1с делает сообщение
    на границе включённым. meta:backfill не пишется: это окно, а не курсор;
    вставки идемпотентны, курсоры sync не трогаем.
    """
    boundary = datetime.now(timezone.utc) - timedelta(seconds=days * 86400 + 1)
    chats = [r["id"] for r in db.conn.execute("SELECT id FROM chats ORDER BY id")]
    forums = {r["id"] for r in db.conn.execute(
        "SELECT id FROM chats WHERE is_forum=1")}
    total_new = total_seen = 0
    for cid in chats:
        rows: list[dict[str, Any]] = []
        audio_ids: set[int] = set()
        attempt = 0
        try:
            while True:  # FloodWait: повторяем чат
                rows = []
                audio_ids = set()
                try:
                    async for msg in client.iter_messages(
                            cid, offset_date=boundary, reverse=True, limit=None):
                        tid = topic_of_message(msg) if cid in forums else None
                        if tid is None:
                            tid = 1
                        if not dry_run:
                            rows.append(message_row(msg, cid, tid, client))
                            if media_mod.is_transcribable(msg):
                                audio_ids.add(int(msg.id))
                        total_seen += 1
                    break
                except _FloodWait as exc:
                    attempt += 1
                    if attempt > 3:
                        raise
                    await _flood_sleep(exc, attempt)
        except Exception as exc:
            print(f"  ! {cid}: окно не прочитано ({exc})", file=sys.stderr)
            continue
        if dry_run:
            print(f"  {cid}: окно {days}д — {total_seen} сообщений (dry-run)", flush=True)
            continue
        if not rows:
            continue
        new = db.insert_messages(rows)
        for row in rows:
            db.insert_refs(cid, row["msg_id"],
                           extract_refs(row["text"], row["fwd_from"]))
        for row in rows:
            if row["msg_id"] in audio_ids:
                media_mod.enqueue(db, cid, row["msg_id"], row["media"])
        for row in rows:
            if row["edited_ts"] is not None and db.update_message(row):
                db.insert_refs(cid, row["msg_id"],
                               extract_refs(row["text"], row["fwd_from"]))
        db.conn.commit()
        total_new += new
        print(f"  {cid}: окно {days}д — {len(rows)} сообщений, новых {new}", flush=True)
    if not dry_run:
        for cid in chats:
            db.recount_topics(cid)
        db.conn.commit()
    return {"chats": len(chats), "seen": total_seen, "new": total_new, "days": days}


async def sync_watched(
    client: Any,
    db: DB,
    *,
    limit: Optional[int] = None,
    days: Optional[int] = None,
    backfill_chunk: int = 0,
    dry_run: bool = False,
) -> list[dict]:
    """Синхронизировать только то, что добавлено в watch-лист."""
    reports = []
    seen_chats: set[int] = set()
    for w in db.watch_list():
        chat_id = w["chat_id"]
        if chat_id in seen_chats:
            continue
        seen_chats.add(chat_id)
        try:
            entity = await client.get_entity(chat_id)
        except Exception as exc:
            print(f"  ! {chat_id}: не удалось получить чат ({exc})", file=sys.stderr)
            continue
        rep = await sync_chat(
            client, db, entity, limit=limit, days=days, dry_run=dry_run,
        )
        if backfill_chunk:
            rep["backfill"] = await backfill(
                client, db, chat_id, forum=rep["is_forum"],
                start_id=0, chunk_size=backfill_chunk, dry_run=dry_run,
            )
        reports.append(rep)
    return reports


async def select_chats(
    client: Any, names: list[str], pattern: Optional[str], forums_only: bool = False,
    folder: Optional[int] = None, include_channels: bool = False,
    folder_peers: Optional[list[int]] = None,
) -> list[Any]:
    """folder: id папки Telegram — отбор «как я накликал в приложении».

    Для папок каналы-вещания тоже считаются нужными (в папку их кладут осознанно),
    поэтому при folder include_channels включается автоматически.
    """
    if folder is not None:
        include_channels = True
        try:
            dialogs = [d async for d in client.iter_dialogs(folder=folder)]
        except _FolderIdInvalid:
            # слой 229+: getDialogs отклоняет folder_id — состав берём из include_peers (getDialogFilters)
            dialogs = []
            peers = list(folder_peers or [])
            try:
                got = await client.get_entity(peers)
                dialogs = list(got) if isinstance(got, (list, tuple)) else [got]
            except Exception:
                for pid in peers:
                    try:
                        dialogs.append(await client.get_entity(pid))
                    except Exception as exc:
                        print(f"  ! чат папки {pid} не получен: {exc}", file=sys.stderr)
            print(f"  папка {folder}: getDialogs отклонил folder_id, "
                  f"беру {len(dialogs)} чатов из состава папки")
    else:
        dialogs = await client.get_dialogs()
    picked = []
    rx = re.compile(pattern, re.IGNORECASE) if pattern else None
    wanted = {n.lower().lstrip("@") for n in names}
    for d in dialogs:
        entity = getattr(d, "entity", d)
        named = (str(entity.id) in names or str(marked_id(entity)) in names
                 or (getattr(entity, "username", None) or "").lower() in wanted)
        # канал-вещание берём, если его назвали явно или он лежит в отобранной папке:
        # в обоих случаях это осознанный выбор пользователя
        allowed = include_channels or named
        if allowed:
            if _label(entity) in ("ChatForbidden", "ChannelForbidden"):
                continue
            if not (is_group(entity) or getattr(entity, "broadcast", False)):
                continue
        elif not is_group(entity):
            continue
        if forums_only and not is_forum(entity):
            continue
        if names:
            if named:
                picked.append(entity)
                continue
            if rx is None:
                continue
        if rx and not rx.search(getattr(entity, "title", "") or ""):
            continue
        picked.append(entity)
    if names:
        found = ({str(getattr(e, "username", None)).lower() for e in picked}
                 | {str(e.id) for e in picked} | {str(marked_id(e)) for e in picked})
        missing = [n for n in names if n.lstrip("@").lower() not in found and n not in found]
        if missing:
            print(f"  ! не найдено в диалогах: {', '.join(missing)}", file=sys.stderr)
    return picked


async def run(
    *,
    db_path: str,
    client: Any = None,
    names: Optional[list[str]] = None,
    pattern: Optional[str] = None,
    topics_only: bool = False,
    folder: Optional[int] = None,
    limit: Optional[int] = None,
    days: Optional[int] = None,
    backfill_chunk: int = 0,
    backfill_days: int = 0,
    watched_only: bool = False,
    dry_run: bool = False,
) -> dict:
    own_client = False
    folders_saved = 0
    if client is None:
        if not HAVE_TELETHON:
            raise SystemExit("нужен Telethon: pip install telethon")
        client = make_client()
        await client.start(**start_kwargs())
        own_client = True

    reports = []
    with DB(db_path) as db:
        folder_peers = None
        # папки нужны не только для записи: при folder их состав — фолбэк select_chats
        if not dry_run or folder is not None:
            try:
                fl = await fetch_folders(client)
                if not dry_run:
                    db.save_folders(fl)
                    folders_saved = len(fl)
                if folder is not None:
                    folder_peers = next((f["peers"] for f in fl if f["id"] == folder), None)
                    if folder_peers is None:
                        print(f"  ! папка {folder} не найдена в аккаунте", file=sys.stderr)
            except Exception as exc:  # папки — best effort, не роняем синк
                print(f"  ! папки не получены: {exc}", file=sys.stderr)
        if backfill_days:
            rep = await backfill_window(client, db, backfill_days, dry_run=dry_run)
            print(f"окно-бэкфилл: чатов {rep['chats']}, просмотрено {rep['seen']}, "
                  f"новых {rep['new']}")
            if own_client:
                await client.disconnect()
            return {"backfill_window": rep, "chats": [], "dry_run": dry_run}
        if watched_only:
            reports = await sync_watched(
                client, db, limit=limit, days=days,
                backfill_chunk=backfill_chunk, dry_run=dry_run,
            )
            print(f"групп в watch-листе: {len({r['chat_id'] for r in reports})}")
        else:
            chats = await select_chats(
                client, names or [], pattern, forums_only=topics_only, folder=folder,
                folder_peers=folder_peers,
            )
            print(f"чатов к синхронизации: {len(chats)}")
            for chat in chats:
                rep = await sync_chat(
                    client, db, chat, limit=limit, days=days, dry_run=dry_run,
                )
                if backfill_chunk:
                    rep["backfill"] = await backfill(
                        client, db, rep["chat_id"], forum=rep["is_forum"],
                        chunk_size=backfill_chunk, dry_run=dry_run,
                    )
                reports.append(rep)
        for rep in reports:
            top = rep["topics"][:8]
            parts = ", ".join(f"{t['title'] or 'без темы'}:{t['new']}" for t in top)
            rest = len(rep["topics"]) - len(top)
            tail = f" … ещё тем: {rest}" if rest > 0 else ""
            bf = f"  backfill+{rep['backfill']['new']}" if rep.get("backfill") else ""
            print(f"  {rep['chat_id']}  {rep['title']!r}  новых {rep['new_messages']}  "
                  f"[{parts}{tail}]{bf}")
    if own_client:
        await client.disconnect()
    return {"chats": reports, "dry_run": dry_run}


def main() -> None:
    ap = argparse.ArgumentParser(description="Слить Telegram-группы и темы в локальный SQLite")
    ap.add_argument("--db", default=os.environ.get("TG_DB", "tg_index.sqlite"))
    ap.add_argument("--chat", action="append", default=[], help="@username или id; можно несколько раз")
    ap.add_argument("--pattern", help="регулярка по названию группы")
    ap.add_argument("--topics", action="store_true", help="только группы-форумы с темами")
    ap.add_argument("--limit", type=int, help="максимум сообщений на одну тему")
    ap.add_argument("--days", type=int, help="окно в днях (по умолчанию всё)")
    ap.add_argument("--folder", type=int,
                    help="id папки Telegram: синхронизировать то, что лежит в ней")
    ap.add_argument("--backfill", type=int, metavar="CHUNK",
                    help="дососать историю снизу вверх чанками по N сообщений "
                         "(для каналов с историей в годы); прогресс сохраняется")
    ap.add_argument("--backfill-days", type=int, metavar="N",
                    help="досинкать историю за окно N дней по всем чатам индекса "
                         "(date-режим, без курсоров)")
    ap.add_argument("--watched", action="store_true",
                    help="синхронизировать только группы из watch-листа")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    result = asyncio.run(
        run(
            db_path=args.db,
            names=args.chat,
            pattern=args.pattern,
            topics_only=args.topics,
            folder=args.folder,
            limit=args.limit,
            days=args.days,
            backfill_chunk=args.backfill or 0,
            backfill_days=args.backfill_days or 0,
            watched_only=args.watched,
            dry_run=args.dry_run,
        )
    )
    print(json.dumps({"total_new": sum(r["new_messages"] for r in result["chats"]),
                      "chats": len(result["chats"]), "dry_run": result["dry_run"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
