#!/usr/bin/env python3
"""Live-режим: новые сообщения Telegram пишутся в индекс в реальном времени.

    python3 live.py

Работает рядом с ui.py: UI опрашивает БД (/api/poll), поэтому между
процессами никаких труб — SQLite (WAL) общая. Пишет только в БД,
в Telegram не отправляет ничего.

Какие чаты слушает: всё, что уже есть в индексе (chats) + watch-лист.
Новые чаты добавляешь обычным sync.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB, extract_refs  # noqa: E402
import sync as sync_mod  # noqa: E402
import media as media_mod  # noqa: E402

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
    if action is not None and sync_mod._label(action) == "MessageActionTopicCreated":
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
    media_mod.enqueue(db, chat_id, row["msg_id"], row["media"])
    return True


async def main() -> None:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("нужны TELEGRAM_API_ID / TELEGRAM_API_HASH")
    sess = os.environ.get("TELEGRAM_SESSION_STRING")
    client = TelegramClient(
        StringSession(sess) if sess else os.environ.get("TELEGRAM_SESSION_NAME", "tg_agent_session"),
        int(api_id), api_hash,
    )
    await client.start()

    state: dict = {}
    state["ids"], state["forums"] = collect_ids()
    state["refreshed"] = time.monotonic()
    db = DB(DB_PATH)

    print(f"live: слушаю {len(state['ids'])} чатов ({len(state['forums'])} форумов). Ctrl-C для стопа.")

    @client.on(events.NewMessage)
    async def on_new(event):  # noqa: ANN
        cid = getattr(event, "chat_id", None)
        if cid is None:
            return
        if cid not in state["ids"]:
            # чат могли добавить в индекс/лист после старта — перечитываем не чаще раза в 30с
            now = time.monotonic()
            if now - state["refreshed"] > 30:
                state["ids"], state["forums"] = collect_ids()
                state["refreshed"] = now
            if cid not in state["ids"]:
                return
        try:
            if handle_message(db, event.message, cid, cid in state["forums"]):
                print(f"  + {cid}/{event.message.id}", flush=True)
        except Exception as exc:  # не роняем демона на одном кривом сообщении
            print(f"  ! {cid}: {exc}", file=sys.stderr)

    try:
        await client.run_until_disconnected()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
