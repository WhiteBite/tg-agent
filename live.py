#!/usr/bin/env python3
"""Live-режим: новые сообщения, правки, удаления и реакции Telegram пишутся в
индекс в реальном времени (правки — через edited_ts, удаления — флаг deleted,
реакции — агрегат-каунты в messages.reactions).

    python3 live.py

Работает рядом с ui.py: UI опрашивает БД (/api/poll), поэтому между
процессами никаких труб — SQLite (WAL) общая. Пишет только в БД,
в Telegram не отправляет ничего.

Какие чаты слушает: всё, что уже есть в индексе (chats) + watch-лист.
Новые чаты добавляешь обычным sync.py.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env

load_env()

import accounts as accounts_mod
import alerts as alerts_mod
import proclock
import sync as sync_mod
from db import DB, extract_refs

import media as media_mod

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))


def collect_ids(db_path: str = DB_PATH) -> tuple[set[int], set[int]]:
    """(ids чатов, ids форумов) — что слушаем: всё из индекса + watch-лист."""
    with DB(db_path) as db:
        chats = db.chats(limit=2000)
        ids = {c["id"] for c in chats} | {w["chat_id"] for w in db.watch_list()}
        forums = {c["id"] for c in chats if c["is_forum"]}
    return ids, forums


def handle_message(db: DB, msg, chat_id: int, forum: bool) -> bool:
    """Одно новое сообщение -> строка индекса (+refs). Возвращает, вставилась ли."""
    tid = sync_mod.topic_of_message(msg) if forum else None
    if tid is None:
        tid = 1
    action = getattr(msg, "action", None)
    if action is not None and sync_mod._label(action) == "MessageActionTopicCreate":
        try:  # новая тема — сразу с заголовком, не дожидаясь следующего sync
            db.upsert_topic(chat_id, tid,
                            str(getattr(action, "title", None) or f"тема {tid}"), False)
            db.conn.commit()
        except Exception as exc:
            print(f"  ! тема {tid} не сохранена: {exc}", file=sys.stderr)
    row = sync_mod.message_row(msg, chat_id, tid, None)
    if not db.insert_message(row):
        return False
    db.insert_refs(chat_id, row["msg_id"], extract_refs(row["text"], row["fwd_from"]))
    if media_mod.is_transcribable(msg):
        media_mod.enqueue(db, chat_id, row["msg_id"], row["media"])
    return True


def handle_edit(db: DB, msg, chat_id: int, forum: bool) -> bool:
    """Правка сообщения -> обновление строки индекса (+refs); сообщения,
    которых ещё нет, вставляются целиком. False — если менять нечего."""
    tid = sync_mod.topic_of_message(msg) if forum else None
    if tid is None:
        tid = 1
    row = sync_mod.message_row(msg, chat_id, tid, None)
    if not db.update_message(row) and not db.insert_message(row):
        return False
    db.insert_refs(chat_id, row["msg_id"], extract_refs(row["text"], row["fwd_from"]))
    if media_mod.is_transcribable(msg):
        media_mod.enqueue(db, chat_id, row["msg_id"], row["media"])
    return True


def handle_delete(db: DB, chat_id: int, msg_ids: list[int]) -> int:
    """Удаления -> флаг deleted; возвращает число помеченных строк."""
    n = 0
    for mid in msg_ids:
        if db.mark_deleted(chat_id, mid):
            n += 1
    return n


def reaction_items(update) -> list[dict]:  # noqa: ANN
    """Агрегат-каунты из UpdateMessageReactions -> [{"emoji","count"}].
    Снятие всех реакций даёт пустой список; count<=0 отбрасывается."""
    items: list[dict] = []
    for rc in getattr(getattr(update, "reactions", None), "results", None) or []:
        count = getattr(rc, "count", 0) or 0
        if count <= 0:
            continue
        r = getattr(rc, "reaction", None)
        items.append({"emoji": getattr(r, "emoticon", None) or str(r),
                      "count": int(count)})
    return items


def handle_reactions(db: DB, chat_id: int, msg_id: int, items: list[dict]) -> bool:
    """Реакции -> снапшот messages.reactions. False, если сообщения нет в индексе."""
    return db.update_reactions(chat_id, msg_id, items)


def observe_chat(db: DB, account_id: str | None, chat_id: int) -> None:
    """Отмечает чат видимым аккаунту; в одиночном режиме — no-op."""
    if account_id is None:
        return
    db.observe_chat_account(chat_id, account_id)
    db.conn.commit()


async def main(account: str | None = None) -> None:
    account_id = accounts_mod.validate(account)
    lock_account = account_id if accounts_mod.is_multi() else None
    lock = proclock.acquire_instance_lock(
        proclock.lock_path(str(DB_PATH), "live", lock_account))
    if lock is None:
        raise SystemExit("live.py уже запущен (lockfile занят)")
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    from telethon.tl import types as tl_types
    from telethon.utils import get_peer_id

    cfg = accounts_mod.resolve(account_id)
    api_id = cfg["api_id"]
    api_hash = cfg["api_hash"]
    if not api_id or not api_hash:
        raise SystemExit("нужны TELEGRAM_API_ID / TELEGRAM_API_HASH")
    sess = cfg["session_string"]
    client = TelegramClient(
        StringSession(sess) if sess else cfg["session_name"],
        int(api_id), api_hash,
        connection_retries=None,  # None у Telethon = реконнекты без предела: обрыв связи не должен убивать демона
    )
    await client.start()

    state: dict = {}
    state["ids"], state["forums"] = collect_ids()
    state["refreshed"] = time.monotonic()
    db = DB(DB_PATH)
    obs_account = account_id if accounts_mod.is_multi() else None
    if obs_account is not None:
        db.upsert_account(account_id, label=cfg["label"])
        db.conn.commit()

    print(f"live[{account_id}]: слушаю {len(state['ids'])} чатов "
          f"({len(state['forums'])} форумов). Ctrl-C для стопа.")

    def known(cid: int) -> bool:
        if cid in state["ids"]:
            return True
        # чат могли добавить в индекс/лист после старта — перечитываем не чаще раза в 30с
        now = time.monotonic()
        if now - state["refreshed"] > 30:
            state["ids"], state["forums"] = collect_ids()
            state["refreshed"] = now
        return cid in state["ids"]

    @client.on(events.NewMessage)
    async def on_new(event):  # noqa: ANN
        cid = getattr(event, "chat_id", None)
        if cid is None or not known(cid):
            return
        try:
            if handle_message(db, event.message, cid, cid in state["forums"]):
                observe_chat(db, obs_account, cid)
                print(f"  + {cid}/{event.message.id}", flush=True)
                await alerts_mod.maybe_alert(
                    client, db, cid,
                    sync_mod.topic_of_message(event.message) if cid in state["forums"] else 1,
                    event.message.message or "")
        except Exception as exc:  # не роняем демона на одном кривом сообщении
            print(f"  ! {cid}: {exc}", file=sys.stderr)

    @client.on(events.MessageEdited)
    async def on_edit(event):  # noqa: ANN
        cid = getattr(event, "chat_id", None)
        if cid is None or not known(cid):
            return
        try:
            if handle_edit(db, event.message, cid, cid in state["forums"]):
                observe_chat(db, obs_account, cid)
                print(f"  ~ {cid}/{event.message.id}", flush=True)
        except Exception as exc:
            print(f"  ! {cid}: {exc}", file=sys.stderr)

    @client.on(events.MessageDeleted)
    async def on_delete(event):  # noqa: ANN
        # в личных чатах и малых группах Telegram не сообщает, где удалили — chat_id None
        cid = getattr(event, "chat_id", None)
        ids = list(getattr(event, "deleted_ids", None) or [])
        if cid is None or not ids or not known(cid):
            return
        try:
            n = handle_delete(db, cid, ids)
            if n:
                print(f"  - {cid}: {n} удалено", flush=True)
        except Exception as exc:
            print(f"  ! {cid}: {exc}", file=sys.stderr)

    # в Telethon 1.45 нет events.MessageReaction — снимаем агрегат-каунты с сырого UpdateMessageReactions
    @client.on(events.Raw(types=tl_types.UpdateMessageReactions))
    async def on_reaction(update):  # noqa: ANN
        try:
            cid = get_peer_id(update.peer)
        except Exception:
            return
        if not known(cid):
            return
        try:
            items = reaction_items(update)
            if handle_reactions(db, cid, update.msg_id, items):
                observe_chat(db, obs_account, cid)
                print(f"  r {cid}/{update.msg_id}", flush=True)
        except Exception as exc:
            print(f"  ! {cid}: {exc}", file=sys.stderr)

    try:
        await client.run_until_disconnected()
    finally:
        db.close()


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Live-режим: новые сообщения в индекс")
    ap.add_argument("--account", default=None,
                    help="имя аккаунта из TG_ACCOUNTS (по умолчанию неявный default)")
    args = ap.parse_args()
    asyncio.run(main(args.account))


if __name__ == "__main__":
    _cli()
