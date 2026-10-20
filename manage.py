#!/usr/bin/env python3
"""Создание чатов Telegram: группы, супергруппы, каналы, форумы.

    python manage.py create --title "Моя группа"
    python manage.py create --title "Канал" --type channel --about "про ИИ"
    python manage.py create --title "Форум" --type supergroup --forum

Третий пишущий модуль репозитория (после notify.py и folders.py): создаёт
чаты, контент и настройки не трогает. Реальное создание только при
TG_ALLOW_MANAGE=1 — без него любая операция возвращает ошибку. Созданный чат
сразу регистрируется в индексе (created_by_me=1, my_role=owner). MCP-сервер
этот модуль не импортирует — агент создания не видит.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

import sync as sync_mod  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

ALLOWED = ("1", "true", "yes")


def _allowed() -> bool:
    return os.environ.get("TG_ALLOW_MANAGE", "").strip().lower() in ALLOWED


def _blocked() -> dict:
    return {"ok": False, "error": "manage заблокирован: нужен TG_ALLOW_MANAGE=1"}


async def _resolve(client: Any, ref: Any) -> Any:
    if isinstance(ref, int):
        return ref
    return await client.get_entity(ref)


def _updates_chats(res: Any) -> list[Any]:
    inner = getattr(res, "updates", None)
    if inner is not None and hasattr(inner, "chats"):
        res = inner
    chats = getattr(res, "chats", None)
    if chats is None:
        raise ValueError(f"в ответе нет чатов: {type(res).__name__}")
    return list(chats)


async def create_chat(client: Any, title: str, *, kind: str = "supergroup",
                      about: str = "", forum: bool = False,
                      db_path: str | None = None) -> dict:
    if not _allowed():
        return _blocked()
    if kind not in ("group", "supergroup", "channel"):
        return {"ok": False, "error": f"неизвестный kind: {kind!r}"}
    if forum and kind != "supergroup":
        return {"ok": False, "error": "forum только с kind=supergroup"}
    if not title.strip():
        return {"ok": False, "error": "нужен title"}
    from telethon.tl import functions
    if kind == "group":
        req = functions.messages.CreateChatRequest(users=[], title=title.strip())
    else:
        broadcast = kind == "channel" or None
        mega = kind == "supergroup" or None
        forum_flag = True if forum else None
        req = functions.channels.CreateChannelRequest(
            title=title.strip(), about=about, broadcast=broadcast,
            megagroup=mega, forum=forum_flag)
    resp = await client(req)
    chats = _updates_chats(resp)
    if not chats:
        return {"ok": False, "error": "сервер не вернул chat"}
    ent = chats[0]
    chat_id = sync_mod.marked_id(ent)
    chat_kind = sync_mod.kind_of(ent)
    is_forum = bool(getattr(ent, "forum", False)) or forum
    registered = False
    if db_path:
        import db as db_mod
        with db_mod.DB(db_path) as db:
            db.upsert_chat(
                chat_id=chat_id, title=ent.title,
                username=getattr(ent, "username", None),
                kind=chat_kind, is_forum=is_forum,
                members=getattr(ent, "participants_count", None),
                created_by_me=1, my_role="owner")
        registered = True
    return {"ok": True, "chat_id": chat_id, "title": ent.title,
            "kind": chat_kind, "is_forum": is_forum, "registered": registered}


async def _amain(args: argparse.Namespace) -> int:
    client = sync_mod.make_client()
    await client.start(**sync_mod.start_kwargs())
    try:
        res = await create_chat(client, args.title, kind=args.type,
                                about=args.about, forum=args.forum,
                                db_path=args.db)
    finally:
        await client.disconnect()
    print(res)
    return 0 if res.get("ok") else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Создание чатов Telegram")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create", help="новый чат: группа / супергруппа / канал / форум")
    p.add_argument("--title", required=True, help="название чата")
    p.add_argument("--type", choices=("group", "supergroup", "channel"),
                   default="supergroup", help="тип чата")
    p.add_argument("--about", default="", help="описание (канал / супергруппа)")
    p.add_argument("--forum", action="store_true", help="форум (только supergroup)")
    p.add_argument("--db", default=DB_PATH)
    sys.exit(asyncio.run(_amain(ap.parse_args())))


if __name__ == "__main__":
    main()
