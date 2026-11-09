#!/usr/bin/env python3
"""Ингест через Telegram Bot API в тот же локальный SQLite-индекс, что и sync.py.

Бот добавлен в группы, модуль опрашивает getUpdates и пишет сообщения в общую
таблицу messages (via='bot'), поэтому bot- и user-путь сходятся в одной базе.
Курсор апдейтов — meta:bot:offset. В Telegram ничего не отправляет.

    TG_BOT_TOKEN=... python3 bot_ingest.py
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from db import DB, clean_text, extract_refs
from env import load_env
from sanitize import sanitize_name

load_env()

OFFSET_KEY = "bot:offset"

_SERVICE_KEYS = frozenset({
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo",
    "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
    "channel_chat_created", "migrate_to_chat_id", "migrate_from_chat_id",
    "pinned_message", "video_chat_started", "video_chat_ended",
    "video_chat_scheduled", "video_chat_participants_invited",
    "message_auto_delete_timer_changed", "forum_topic_created",
    "forum_topic_closed", "forum_topic_reopened", "forum_topic_edited",
    "general_forum_topic_hidden", "general_forum_topic_unhidden",
    "write_access_allowed", "proximity_alert_triggered", "boost_added",
    "chat_background_set", "users_shared", "chat_shared",
})


def _base_url() -> str:
    return os.environ.get("TG_BOT_URL", "https://api.telegram.org").rstrip("/")


def _api(token: str, method: str, params: dict[str, Any], timeout: int) -> dict:
    url = f"{_base_url()}/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_service(msg: dict) -> bool:
    return any(k in msg for k in _SERVICE_KEYS)


def _message_row(update: dict) -> dict | None:
    msg = update.get("message")
    if not msg:
        return None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    date = msg.get("date")
    ts = int(date) if date is not None else None
    date_iso = (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                if ts is not None else None)
    frm = msg.get("from") or {}
    name = " ".join(p for p in (frm.get("first_name"), frm.get("last_name")) if p)
    if not name:
        username = frm.get("username")
        name = f"@{username}" if username else None
    if name:
        name = sanitize_name(name)
    text = msg.get("text") or msg.get("caption")
    if text:
        text = clean_text(text)
    fwd = msg.get("forward_from_chat") or msg.get("forward_from") or {}
    reply = msg.get("reply_to_message") or {}
    return {
        "chat_id": chat_id,
        "msg_id": msg.get("message_id"),
        "topic_id": msg.get("message_thread_id") or 1,
        "ts": ts,
        "date_iso": date_iso,
        "sender_id": frm.get("id"),
        "sender_name": name,
        "is_out": 0,
        "is_service": 0,
        "text": text,
        "media": None,
        "reply_to": reply.get("message_id"),
        "views": None,
        "forwards": None,
        "reactions": [],
        "fwd_from": fwd.get("id"),
        "edited_ts": msg.get("edit_date"),
        "deleted": False,
        "raw_json": json.dumps(update, ensure_ascii=False, default=str),
        "via": "bot",
    }


def handle_update(db: DB, update: dict) -> int:
    row = _message_row(update)
    if row is None:
        return 0
    if _is_service(update["message"]):
        return 0
    if not db.insert_message(row):
        return 0
    db.conn.execute(
        "UPDATE messages SET via='bot' WHERE chat_id=? AND msg_id=?",
        (row["chat_id"], row["msg_id"]),
    )
    db.conn.commit()
    db.insert_refs(row["chat_id"], row["msg_id"],
                   extract_refs(row["text"], row["fwd_from"]))
    return 1


def run(db_path: str, token: str | None = None, once: bool = False,
        limit: int = 100, timeout: int = 25) -> dict:
    token = token or os.environ.get("TG_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "TG_BOT_TOKEN not set", "handled": 0, "offset": 0}
    handled = 0
    with DB(db_path) as db:
        saved = db.get_meta(OFFSET_KEY)
        offset = int(saved) if saved else 0
        while True:
            params = {"offset": offset, "limit": int(limit), "timeout": int(timeout)}
            try:
                resp = _api(token, "getUpdates", params, int(timeout) + 5)
            except Exception as exc:
                return {"ok": False, "error": str(exc), "handled": handled, "offset": offset}
            if not resp.get("ok"):
                return {"ok": False, "error": resp.get("description") or "getUpdates failed",
                        "handled": handled, "offset": offset}
            updates = resp.get("result") or []
            for u in updates:
                handled += handle_update(db, u)
                uid = u.get("update_id")
                if uid is not None:
                    offset = max(offset, int(uid) + 1)
            if updates:
                db.set_meta(OFFSET_KEY, str(offset))
            if once:
                break
        return {"ok": True, "handled": handled, "offset": offset}
