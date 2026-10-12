#!/usr/bin/env python3
"""Watch-лист: отметить темы/каналы, за которыми агент должен следить.

Это и есть «визуально выделить нужное». Два способа отбора:

  1. Папка Telegram. Создай в приложении папку (Settings → Folders), положи туда
     нужные каналы/группы — и синхронизируй её целиком:  sync.py --folder <id>
     Отмечать ничего не нужно: что в папке, то и читает агент.
  2. Точечно по темам (этот файл) — когда группа одна, а тем в ней десятки:

     python3 watch.py topics --chat @mygroup        # показать темы с цифрами
     python3 watch.py add --chat @mygroup --topic 123456 --alias "ML-новости" --pin
     python3 watch.py add --chat @mygroup --topic 789 --alias "Релизы"
     python3 watch.py list
     python3 watch.py remove --chat @mygroup --topic 789

Дальше агент в opencode видит только отмеченное: инструмент tg_watch показывает
ему список с твоими алиасами.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from typing import Optional

from db import DB


def _fmt_ts(ts: Optional[int]) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "—"


async def cmd_topics(args) -> None:
    """Показать темы группы прямо из Telegram с цифрами: сколько сообщений, когда последнее."""
    import sync as sync_mod
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("нужны TELEGRAM_API_ID и TELEGRAM_API_HASH (https://my.telegram.org/apps)")
    sess = os.environ.get("TELEGRAM_SESSION_STRING")
    client = TelegramClient(
        StringSession(sess) if sess else os.environ.get("TELEGRAM_SESSION_NAME", "tg_agent_session"),
        int(api_id), api_hash,
    )
    await client.start()
    entity = await client.get_entity(args.chat)
    chat_id = sync_mod.marked_id(entity)
    print(f"{chat_id}  {getattr(entity, 'title', '')!r}  "
          f"форум={sync_mod.is_forum(entity)}  username={getattr(entity, 'username', None)}")

    if sync_mod.is_forum(entity):
        topics = await sync_mod.list_topics(client, entity)
        print(f"\n{'topic_id':>10}  {'сообщ.':>7}  {'последнее':>10}  {'':>2} название")
        for t in sorted(topics, key=lambda x: -(x["messages"] or 0)):
            print(f"{t['topic_id']:>10}  {t['messages'] or 0:>7}  "
                  f"{'закрыта' if t['closed'] else '':>10}  {t['title']}")
        print(f"\n      1  (General: сообщения без темы — всегда есть)")
    else:
        print("\nГруппа без тем. Добавляй как псевдотему 0:")
        print(f"  python3 watch.py add --chat {args.chat} --topic 0")

    with DB(args.db) as db:
        watched = {w["topic_id"] for w in db.watch_list(chat_id)}
    if watched:
        print(f"\nуже в watch-листе: {sorted(watched)}")
    await client.disconnect()


def cmd_add(args) -> None:
    chat_id = args.chat_id
    if chat_id is None:
        # разрешаем @username, если группа уже есть в индексе
        with DB(args.db) as db:
            found = db.chats(query=args.chat.lstrip("@"), limit=5)
        exact = [c for c in found if (c["username"] or "").lower() == args.chat.lstrip("@").lower()]
        if not exact:
            raise SystemExit(
                f"группа {args.chat!r} не найдена в индексе. Сначала sync.py --chat {args.chat}, "
                f"либо передай --chat-id <числовой id> (его печатает watch.py topics)."
            )
        chat_id = exact[0]["id"]
    with DB(args.db) as db:
        db.watch_add(chat_id, args.topic, alias=args.alias, pinned=args.pin)
        print(f"+ {chat_id}/{args.topic}" + (f"  alias={args.alias!r}" if args.alias else ""))
        _print_watch(db, chat_id)


def cmd_remove(args) -> None:
    if args.chat_id is None:
        raise SystemExit("для remove нужен --chat-id (числовой)")
    with DB(args.db) as db:
        ok = db.watch_remove(args.chat_id, args.topic)
        print("- убрано" if ok else "! не было в списке")


def _print_watch(db: DB, chat_id: Optional[int] = None) -> None:
    rows = db.watch_list(chat_id)
    if not rows:
        print("(watch-лист пуст)")
        return
    print(f"\n{'chat_id':>14}  {'topic':>7}  {'всего':>7}  {'новых':>6}  {'последнее':>10}  алиас / тема")
    for w in rows:
        label = w["alias"] or w["topic_title"] or "без темы"
        pin = "★ " if w["pinned"] else "  "
        print(f"{w['chat_id']:>14}  {w['topic_id']:>7}  {w['total'] or 0:>7}  "
              f"{w['new_since_read'] or 0:>6}  {_fmt_ts(w['last_ts']):>10}  {pin}{label}")


def cmd_list(args) -> None:
    with DB(args.db) as db:
        _print_watch(db)


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch-лист тем/каналов для агента")
    ap.add_argument("--db", default=os.environ.get("TG_DB", "tg_index.sqlite"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("topics", help="показать темы группы из Telegram с цифрами")
    t.add_argument("--chat", required=True, help="@username или числовой id")

    a = sub.add_parser("add", help="добавить тему в watch-лист")
    a.add_argument("--chat", help="@username (если группа уже в индексе)")
    a.add_argument("--chat-id", type=int, help="числовой id группы")
    a.add_argument("--topic", type=int, required=True, help="id темы (0/1 = General, 0 = группа без тем)")
    a.add_argument("--alias", help="человеческое имя — его увидит агент")
    a.add_argument("--pin", action="store_true", help="показывать первым")

    r = sub.add_parser("remove", help="убрать из watch-листа")
    r.add_argument("--chat-id", type=int, required=True)
    r.add_argument("--topic", type=int, required=True)

    sub.add_parser("list", help="показать watch-лист")

    args = ap.parse_args()
    if args.cmd == "topics":
        asyncio.run(cmd_topics(args))
    elif args.cmd == "add":
        cmd_add(args)
    elif args.cmd == "remove":
        cmd_remove(args)
    else:
        cmd_list(args)


if __name__ == "__main__":
    main()
