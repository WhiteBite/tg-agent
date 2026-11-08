#!/usr/bin/env python3
"""Живые алерты поверх live.py: новое сообщение совпало с правилом
(слово-триггер или упоминание) — уходит в Saved Messages.

    TG_ALERTS=1 TG_ALERT_WORDS=холява,раздача TG_ALERT_MENTIONS=myname
    TG_ALERT_COOLDOWN=300

Отправка идёт через уже подключённый клиент live-демона: второй клиент на
том же session-файле конфликтовал бы его SQLite. Реальная отправка только
при TG_ALLOW_SEND=1, иначе dry-run в лог. Пауза на пару (чат, причина),
чтобы одно и то же правило не строчило потоком.

    python3 alerts.py            # действующие правила (meta или окружение)
    python3 alerts.py --text "нужна холява"   # какие правила сработают
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB, clean_text, extract_refs  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))
ALLOWED = ("1", "true", "yes")
_cooldown: dict[tuple[int, str], float] = {}


def _parse_list(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _send_allowed() -> bool:
    return os.environ.get("TG_ALLOW_SEND", "").strip().lower() in ALLOWED


def _gate_on() -> bool:
    return os.environ.get("TG_ALERTS", "").strip().lower() in ALLOWED


def config(db: DB | None = None) -> dict:
    words = _parse_list(os.environ.get("TG_ALERT_WORDS"))
    mentions = [m.lstrip("@").lower() for m in _parse_list(os.environ.get("TG_ALERT_MENTIONS"))]
    source = "env"
    if db is not None:
        raw = db.get_meta("ws:alerts")
        if raw:
            try:
                stored = json.loads(raw)
            except (ValueError, TypeError):
                stored = None
            if isinstance(stored, dict):
                words = [str(w) for w in stored.get("words") or []]
                mentions = [str(m).lstrip("@").lower() for m in stored.get("mentions") or []]
                source = "ui"
    return {
        "words": words,
        "mentions": mentions,
        "cooldown": int(os.environ.get("TG_ALERT_COOLDOWN", "300") or 300),
        "source": source,
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
    if not _gate_on():
        return []
    cfg = config(db)
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description="живые алерты: показать действующие правила или проверить текст")
    ap.add_argument("--text", help="проверить текст сообщения по текущим правилам")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()
    with DB(args.db) as db:
        cfg = config(db)
    if args.text is None:
        print(json.dumps({"gate": _gate_on(), "source": cfg["source"],
                          "words": cfg["words"], "mentions": cfg["mentions"],
                          "cooldown": cfg["cooldown"]}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({"reasons": match(args.text, cfg)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
