#!/usr/bin/env python3
"""Офлайн-тесты bot_ingest.py: синтетические апдейты Bot API -> общий индекс.

Что реально исполняется:
  1. _message_row: форумная тема (message_thread_id) и General (topic_id=1)
  2. handle_update: дубликат (chat_id, msg_id) не даёт второй строки
  3. handle_update: не-сообщение (edited_message / пустой апдейт) пропускается
  4. via='bot' проставлен на вставленной строке
  5. run без TG_BOT_TOKEN — структурная ошибка, сеть не трогается
  6. run с подставным _api: getUpdates -> вставка, курсор bot:offset двигается
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""
os.environ.pop("TG_BOT_TOKEN", None)

sys.path.insert(0, str(Path(__file__).parent))

from db import DB

import bot_ingest

FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def forum_update(update_id: int = 100, msg_id: int = 10) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": msg_id,
            "date": 1700000000,
            "chat": {"id": -1001234567890, "type": "supergroup", "title": "Форум"},
            "message_thread_id": 42,
            "from": {"id": 555, "first_name": "Иван", "last_name": "Петров",
                     "username": "ivan"},
            "text": "привет из темы",
        },
    }


def general_update(update_id: int = 101, msg_id: int = 11) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": msg_id,
            "date": 1700000001,
            "chat": {"id": -1001234567890, "type": "supergroup", "title": "Форум"},
            "from": {"id": 556, "first_name": "Пётр"},
            "text": "в общем",
        },
    }


def test_message_row(root: Path) -> None:
    print("=== _message_row: тема форума и General ===")
    row = bot_ingest._message_row(forum_update())
    check(row is not None and row["topic_id"] == 42,
          "форумное сообщение -> topic_id из message_thread_id",
          str(row and row["topic_id"]))
    check(row["chat_id"] == -1001234567890 and row["msg_id"] == 10,
          "chat_id/msg_id из chat.id/message_id",
          f"{row['chat_id']}/{row['msg_id']}")
    check(row["sender_id"] == 555 and row["sender_name"] == "Иван Петров",
          "sender_id/sender_name из from",
          f"{row['sender_id']}/{row['sender_name']}")
    check(row["is_out"] == 0 and row["is_service"] == 0 and row["via"] == "bot",
          "is_out=0, is_service=0, via='bot'",
          f"{row['is_out']}/{row['is_service']}/{row['via']}")
    check(row["ts"] == 1700000000 and row["date_iso"].startswith("2023-11-14"),
          "ts/date_iso из date", f"{row['ts']}/{row['date_iso']}")

    row2 = bot_ingest._message_row(general_update())
    check(row2 is not None and row2["topic_id"] == 1,
          "General без message_thread_id -> topic_id 1",
          str(row2 and row2["topic_id"]))

    check(bot_ingest._message_row({"update_id": 1}) is None,
          "апдейт без message -> None")


def test_handle_update(root: Path) -> None:
    print("=== handle_update: вставка, дубликат, не-сообщение, via ===")
    db_path = root / "handle.sqlite"
    with DB(db_path) as db:
        n = bot_ingest.handle_update(db, forum_update())
        check(n == 1, "новое сообщение -> 1", str(n))

        n = bot_ingest.handle_update(db, forum_update())
        check(n == 0, "дубликат (chat_id, msg_id) -> 0", str(n))
        cnt = db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=? AND msg_id=?",
            (-1001234567890, 10)).fetchone()[0]
        check(cnt == 1, "дубликат не даёт второй строки", str(cnt))

        n = bot_ingest.handle_update(db, {"update_id": 102, "edited_message": {
            "message_id": 10, "chat": {"id": -1001234567890}}})
        check(n == 0, "edited_message (не message) -> 0", str(n))

        n = bot_ingest.handle_update(db, {"update_id": 103})
        check(n == 0, "пустой апдейт -> 0", str(n))

        n = bot_ingest.handle_update(db, {"update_id": 104, "message": {
            "message_id": 12, "date": 1700000002,
            "chat": {"id": -1001234567890, "type": "supergroup"},
            "new_chat_members": [{"id": 1, "first_name": "X"}]}})
        check(n == 0, "сервисное сообщение -> 0", str(n))

        row = db.conn.execute(
            "SELECT via, topic_id, sender_name FROM messages "
            "WHERE chat_id=? AND msg_id=?", (-1001234567890, 10)).fetchone()
        check(row is not None and row["via"] == "bot",
              "via='bot' проставлен на вставленной строке",
              str(row and row["via"]))
        check(row["topic_id"] == 42, "topic_id сохранён в БД", str(row["topic_id"]))

        n = bot_ingest.handle_update(db, general_update())
        check(n == 1, "General вставляется", str(n))
        row2 = db.conn.execute(
            "SELECT topic_id, via FROM messages WHERE chat_id=? AND msg_id=?",
            (-1001234567890, 11)).fetchone()
        check(row2["topic_id"] == 1 and row2["via"] == "bot",
              "General -> topic_id 1, via='bot'",
              f"{row2['topic_id']}/{row2['via']}")


def test_run_no_token(root: Path) -> None:
    print("=== run без TG_BOT_TOKEN: структурная ошибка, сеть не трогается ===")
    db_path = root / "notoken.sqlite"
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("сеть не должна вызываться")

    saved = bot_ingest._api
    bot_ingest._api = boom
    try:
        out = bot_ingest.run(str(db_path), once=True)
    finally:
        bot_ingest._api = saved
    check(out.get("ok") is False and bool(out.get("error")),
          "run без токена -> {ok: False, error: ...}",
          str(out))
    check(called["n"] == 0, "run без токена не зовёт транспорт", str(called["n"]))


def test_run_transport(root: Path) -> None:
    print("=== run с подставным _api: getUpdates -> вставка + курсор ===")
    db_path = root / "transport.sqlite"
    calls: list[tuple] = []

    def fake_api(token, method, params, timeout):
        calls.append((token, method, dict(params), timeout))
        return {"ok": True, "result": [forum_update(100, 10), general_update(101, 11)]}

    saved = bot_ingest._api
    bot_ingest._api = fake_api
    try:
        out = bot_ingest.run(str(db_path), token="T", once=True)
    finally:
        bot_ingest._api = saved

    check(out.get("ok") is True and out.get("handled") == 2,
          "run: ok, обработано 2 апдейта", str(out))
    check(out.get("offset") == 102, "run: offset = max(update_id)+1", str(out.get("offset")))
    check(calls and calls[0][1] == "getUpdates" and calls[0][2].get("offset") == 0,
          "транспорт вызван getUpdates с offset=0",
          str(calls[0] if calls else None))

    with DB(db_path) as db:
        check(db.get_meta("bot:offset") == "102",
              "курсор bot:offset сохранён в meta", str(db.get_meta("bot:offset")))
        rows = db.conn.execute(
            "SELECT msg_id, topic_id, via FROM messages ORDER BY msg_id").fetchall()
        check(len(rows) == 2 and all(r["via"] == "bot" for r in rows),
              "обе строки в индексе с via='bot'",
              str([dict(r) for r in rows]))
        check([r["topic_id"] for r in rows] == [42, 1],
              "topic_id: тема 42 и General 1",
              str([r["topic_id"] for r in rows]))

    calls.clear()
    bot_ingest._api = fake_api
    try:
        out2 = bot_ingest.run(str(db_path), token="T", once=True)
    finally:
        bot_ingest._api = saved
    check(calls and calls[0][2].get("offset") == 102,
          "повторный run стартует с сохранённого offset",
          str(calls[0][2] if calls else None))
    check(out2.get("handled") == 0, "повторный run: дубликаты не вставляются",
          str(out2))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_bot_ingest_"))
    try:
        test_message_row(tmp)
        test_handle_update(tmp)
        test_run_no_token(tmp)
        test_run_transport(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"провалов: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
