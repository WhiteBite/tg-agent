#!/usr/bin/env python3
"""Живые алерты поверх live.py: новое сообщение совпало с правилом
(слово-триггер или упоминание) — уходит в Saved Messages.

    TG_ALERTS=1 TG_ALERT_WORDS=холява,раздача TG_ALERT_MENTIONS=myname
    TG_ALERT_COOLDOWN=300

Отправка идёт через уже подключённый клиент live-демона: второй клиент на
том же session-файле конфликтовал бы его SQLite. Реальная отправка только
при TG_ALLOW_SEND=1, иначе dry-run в лог. Пауза на пару (чат, причина),
чтобы одно и то же правило не строчило потоком.
"""

from __future__ import annotations

import os
import sys
import time

from db import DB, clean_text, extract_refs

ALLOWED = ("1", "true", "yes")
_cooldown: dict[tuple[int, str], float] = {}


def _parse_list(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _send_allowed() -> bool:
    return os.environ.get("TG_ALLOW_SEND", "").strip().lower() in ALLOWED


def _alerts_enabled() -> bool:
    if os.environ.get("TG_ALERTS", "").strip().lower() not in ALLOWED:
        return False
    return bool(_parse_list(os.environ.get("TG_ALERT_WORDS"))
                or _parse_list(os.environ.get("TG_ALERT_MENTIONS")))


def config() -> dict:
    return {
        "words": _parse_list(os.environ.get("TG_ALERT_WORDS")),
        "mentions": [m.lstrip("@").lower()
                     for m in _parse_list(os.environ.get("TG_ALERT_MENTIONS"))],
        "cooldown": int(os.environ.get("TG_ALERT_COOLDOWN", "300") or 300),
    }


def match(text: str, cfg: dict) -> list[str]:
    lowered = (text or "").casefold()
    reasons = [f"слово: {w}" for w in cfg["words"] if w.casefold() in lowered]
    refs = extract_refs(text)
    reasons += [f"упоминание: @{m}" for m in cfg["mentions"] if ("mention", m) in refs]
    return reasons


def format_alert(chat_title: str, topic_title: str | None, snippet: str,
                 reasons: list[str]) -> str:
    place = f"«{chat_title}»"
    if topic_title:
        place += f" / «{topic_title}»"
    return f"АЛЕРТ [{'; '.join(reasons)}] — {place}\n{snippet}"


async def maybe_alert(client, db: DB, chat_id: int, topic_id: int,
                      text: str) -> list[str]:
    if not _alerts_enabled():
        return []
    cfg = config()
    reasons = match(text, cfg)
    if not reasons:
        return []
    now = time.monotonic()
    fresh = [r for r in reasons
             if now - _cooldown.get((chat_id, r), 0.0) >= cfg["cooldown"]]
    if not fresh:
        return []
    for r in fresh:
        _cooldown[(chat_id, r)] = now
    chat = db.conn.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
    topic = db.conn.execute("SELECT title FROM topics WHERE chat_id=? AND topic_id=?",
                            (chat_id, topic_id)).fetchone()
    snippet = (clean_text(text) or "").strip()[:200]
    msg = format_alert(chat["title"] if chat else str(chat_id),
                       topic["title"] if topic else None, snippet, fresh)
    if not _send_allowed():
        print(f"  алерт (dry-run, нужен TG_ALLOW_SEND=1): {msg}", flush=True)
        return fresh
    try:
        await client.send_message("me", msg)
    except Exception as exc:
        print(f"  ! алерт не отправлен: {exc}", file=sys.stderr)
        return fresh
    print(f"  алерт: {'; '.join(fresh)} -> Saved Messages ({chat_id})", flush=True)
    return fresh


ENABLED = _alerts_enabled()
