#!/usr/bin/env python3
"""Создание и настройка чатов Telegram: группы, супергруппы, каналы, форумы.

    python manage.py create --title "Моя группа"
    python manage.py create --title "Канал" --type channel --about "про ИИ"
    python manage.py create --title "Форум" --type supergroup --forum
    python manage.py edit --chat @mygroup --title "Новое название"
    python manage.py edit --chat @mygroup --username "" --photo avatar.png
    python manage.py toggle --chat @mygroup --forum on --invites off
    python manage.py rights --chat @mygroup --flag send-messages=off
    python manage.py topic-create --chat @myforum --title "Новая тема"
    python manage.py topic-close --chat @myforum --topic 42

Третий пишущий модуль репозитория (после notify.py и folders.py): создаёт
чаты и правит их настройки (title/about/photo/username, тумблеры, дефолтные
права); контент не трогает. Реальные операции только при
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


def _label(obj: Any) -> str:
    return type(obj).__name__


RIGHTS_FIELDS = ("view_messages", "send_messages", "send_media", "send_stickers",
                 "send_gifs", "send_games", "send_inline", "embed_links",
                 "send_polls", "change_info", "invite_users", "pin_messages",
                 "manage_topics")


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


async def set_title(client: Any, ref: Any, title: str) -> dict:
    if not _allowed():
        return _blocked()
    ent = await _resolve(client, ref)
    from telethon.tl import functions
    if _label(ent) == "Chat":
        await client(functions.messages.EditChatTitleRequest(chat_id=ent.id, title=title))
    else:
        await client(functions.channels.EditTitleRequest(ent, title))
    return {"ok": True}


async def set_about(client: Any, ref: Any, about: str) -> dict:
    if not _allowed():
        return _blocked()
    ent = await _resolve(client, ref)
    if _label(ent) == "Chat":
        return {"ok": False, "error": "about только у супергрупп/каналов"}
    from telethon.tl import functions
    await client(functions.messages.EditChatAboutRequest(peer=ent, about=about))
    return {"ok": True}


async def set_photo(client: Any, ref: Any, path: str) -> dict:
    if not _allowed():
        return _blocked()
    if not Path(path).is_file():
        return {"ok": False, "error": f"нет файла: {path}"}
    ent = await _resolve(client, ref)
    import telethon.utils
    from telethon.tl import functions
    uploaded = await client.upload_file(path)
    photo = telethon.utils.get_input_chat_photo(uploaded)
    if _label(ent) == "Chat":
        await client(functions.messages.EditChatPhotoRequest(ent.id, photo))
    else:
        await client(functions.channels.EditPhotoRequest(ent, photo))
    return {"ok": True}


async def set_username(client: Any, ref: Any, username: str) -> dict:
    if not _allowed():
        return _blocked()
    ent = await _resolve(client, ref)
    if _label(ent) == "Chat":
        return {"ok": False, "error": "username только у супергрупп/каналов"}
    from telethon.tl import functions
    if username:
        free = await client(functions.channels.CheckUsernameRequest(ent, username))
        if free is False:
            return {"ok": False, "error": f"username недоступен: {username}"}
        await client(functions.channels.UpdateUsernameRequest(ent, username))
    else:
        await client(functions.channels.DeactivateAllUsernamesRequest(ent))
    return {"ok": True}


async def toggle_pre_history(client: Any, ref: Any, enabled: bool) -> dict:
    if not _allowed():
        return _blocked()
    ent = await _resolve(client, ref)
    if _label(ent) != "Channel" or bool(getattr(ent, "broadcast", False)):
        return {"ok": False, "error": "pre history только у супергрупп"}
    from telethon.tl import functions
    await client(functions.channels.TogglePreHistoryHiddenRequest(ent, enabled))
    return {"ok": True}


async def toggle_invites(client: Any, ref: Any, enabled: bool) -> dict:
    if not _allowed():
        return _blocked()
    ent = await _resolve(client, ref)
    from telethon.tl import functions
    from telethon.tl.types import ChatBannedRights
    cur = getattr(ent, "default_banned_rights", None)
    flags = {f: bool(getattr(cur, f, False)) for f in RIGHTS_FIELDS}
    flags["invite_users"] = not enabled
    await client(functions.messages.EditChatDefaultBannedRightsRequest(
        ent, ChatBannedRights(None, **flags)))
    return {"ok": True}


async def toggle_signatures(client: Any, ref: Any, enabled: bool) -> dict:
    if not _allowed():
        return _blocked()
    ent = await _resolve(client, ref)
    if _label(ent) != "Channel":
        return {"ok": False, "error": "signatures только у каналов/супергрупп"}
    from telethon.tl import functions
    await client(functions.channels.ToggleSignaturesRequest(
        ent, signatures_enabled=enabled, profiles_enabled=None))
    return {"ok": True}


async def toggle_forum(client: Any, ref: Any, enabled: bool,
                       *, db_path: str | None = None) -> dict:
    if not _allowed():
        return _blocked()
    ent = await _resolve(client, ref)
    if _label(ent) != "Channel" or bool(getattr(ent, "broadcast", False)):
        return {"ok": False, "error": "forum только у супергрупп"}
    from telethon.tl import functions
    await client(functions.channels.ToggleForumRequest(ent, enabled, tabs=False))
    if db_path:
        import db as db_mod
        with db_mod.DB(db_path) as db:
            db.upsert_chat(
                chat_id=sync_mod.marked_id(ent),
                title=getattr(ent, "title", None) or "",
                username=getattr(ent, "username", None),
                kind=sync_mod.kind_of(ent),
                is_forum=enabled,
                members=getattr(ent, "participants_count", None))
    return {"ok": True}


async def set_default_rights(client: Any, ref: Any, rights: dict[str, bool],
                             *, db_path: str | None = None) -> dict:
    if not _allowed():
        return _blocked()
    unknown = sorted(set(rights) - set(RIGHTS_FIELDS))
    if unknown:
        return {"ok": False, "error": f"неизвестные права: {', '.join(unknown)}"}
    ent = await _resolve(client, ref)
    from telethon.tl import functions
    from telethon.tl.types import ChatBannedRights
    flags = {f: bool(v) for f, v in rights.items()}
    await client(functions.messages.EditChatDefaultBannedRightsRequest(
        ent, ChatBannedRights(None, **flags)))
    return {"ok": True}


async def topic_create(client: Any, ref: Any, title: str, *,
                       icon_color: int | None = None,
                       db_path: str | None = None) -> dict:
    if not _allowed():
        return _blocked()
    ent = await _resolve(client, ref)
    if not getattr(ent, "forum", False):
        return {"ok": False, "error": "темы только у форума"}
    import random
    from telethon.tl import functions
    req = functions.messages.CreateForumTopicRequest(
        peer=ent, title=title, icon_color=icon_color,
        random_id=random.getrandbits(63))
    resp = await client(req)
    topic_id = 0
    for u in getattr(resp, "updates", None) or []:
        msg = getattr(u, "message", None)
        if msg is not None and _label(getattr(msg, "action", None)) == "MessageActionTopicCreated":
            topic_id = msg.id
    if not topic_id:
        for m in getattr(resp, "messages", None) or []:
            if _label(getattr(m, "action", None)) == "MessageActionTopicCreated":
                topic_id = m.id
    if not topic_id:
        return {"ok": False, "error": "не удалось извлечь topic_id из ответа"}
    chat_id = sync_mod.marked_id(ent)
    if db_path:
        import db as db_mod
        with db_mod.DB(db_path) as db:
            db.upsert_topic(chat_id, topic_id, title, closed=False)
            db.conn.commit()
    return {"ok": True, "chat_id": chat_id, "topic_id": topic_id, "title": title}


async def topic_edit(client: Any, ref: Any, topic_id: int, *,
                     title: str | None = None, closed: bool | None = None,
                     db_path: str | None = None) -> dict:
    if not _allowed():
        return _blocked()
    if title is None and closed is None:
        return {"ok": False, "error": "нужен title или closed"}
    ent = await _resolve(client, ref)
    if not getattr(ent, "forum", False):
        return {"ok": False, "error": "темы только у форума"}
    from telethon.tl import functions
    await client(functions.messages.EditForumTopicRequest(
        peer=ent, topic_id=topic_id, title=title, closed=closed))
    if db_path:
        import db as db_mod
        chat_id = sync_mod.marked_id(ent)
        with db_mod.DB(db_path) as db:
            row = db.conn.execute(
                "SELECT title, closed FROM topics WHERE chat_id = ? AND topic_id = ?",
                (chat_id, topic_id)).fetchone()
            new_title = title if title is not None else (row["title"] if row else "")
            new_closed = closed if closed is not None else (bool(row["closed"]) if row else False)
            db.upsert_topic(chat_id, topic_id, new_title, closed=new_closed)
            db.conn.commit()
    return {"ok": True}


async def _cmd_edit(client: Any, args: argparse.Namespace) -> dict:
    if args.title is not None:
        res = await set_title(client, args.chat, args.title)
        if not res.get("ok"):
            return res
    if args.about is not None:
        res = await set_about(client, args.chat, args.about)
        if not res.get("ok"):
            return res
    if args.photo is not None:
        res = await set_photo(client, args.chat, args.photo)
        if not res.get("ok"):
            return res
    if args.username is not None:
        res = await set_username(client, args.chat, args.username)
        if not res.get("ok"):
            return res
    return {"ok": True}


async def _cmd_toggle(client: Any, args: argparse.Namespace) -> dict:
    if args.pre_history is not None:
        res = await toggle_pre_history(client, args.chat, args.pre_history)
        if not res.get("ok"):
            return res
    if args.invites is not None:
        res = await toggle_invites(client, args.chat, args.invites)
        if not res.get("ok"):
            return res
    if args.signatures is not None:
        res = await toggle_signatures(client, args.chat, args.signatures)
        if not res.get("ok"):
            return res
    if args.forum is not None:
        res = await toggle_forum(client, args.chat, args.forum, db_path=args.db)
        if not res.get("ok"):
            return res
    return {"ok": True}


async def _cmd_rights(client: Any, args: argparse.Namespace) -> dict:
    rights = dict(args.flag or [])
    return await set_default_rights(client, args.chat, rights, db_path=args.db)


def _on_off(value: str) -> bool:
    if value == "on":
        return True
    if value == "off":
        return False
    raise argparse.ArgumentTypeError("нужно on или off")


def _flag_pair(value: str) -> tuple[str, bool]:
    name, sep, val = value.partition("=")
    name = name.strip().replace("-", "_")
    if not sep or val not in ("on", "off") or name not in RIGHTS_FIELDS:
        raise argparse.ArgumentTypeError(
            "нужно --flag имя=on|off, имена: "
            + ", ".join(f.replace("_", "-") for f in RIGHTS_FIELDS))
    return name, val == "on"


async def _amain(args: argparse.Namespace) -> int:
    client = sync_mod.make_client()
    await client.start(**sync_mod.start_kwargs())
    try:
        if args.cmd == "create":
            res = await create_chat(client, args.title, kind=args.type,
                                    about=args.about, forum=args.forum,
                                    db_path=args.db)
        elif args.cmd == "edit":
            res = await _cmd_edit(client, args)
        elif args.cmd == "toggle":
            res = await _cmd_toggle(client, args)
        elif args.cmd == "topic-create":
            res = await topic_create(client, args.chat, args.title,
                                     icon_color=args.icon_color, db_path=args.db)
        elif args.cmd == "topic-edit":
            res = await topic_edit(client, args.chat, args.topic,
                                   title=args.title, closed=args.closed,
                                   db_path=args.db)
        elif args.cmd == "topic-close":
            res = await topic_edit(client, args.chat, args.topic,
                                   closed=True, db_path=args.db)
        elif args.cmd == "topic-open":
            res = await topic_edit(client, args.chat, args.topic,
                                   closed=False, db_path=args.db)
        else:
            res = await _cmd_rights(client, args)
    finally:
        await client.disconnect()
    print(res)
    return 0 if res.get("ok") else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Создание и настройка чатов Telegram")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create", help="новый чат: группа / супергруппа / канал / форум")
    p.add_argument("--title", required=True, help="название чата")
    p.add_argument("--type", choices=("group", "supergroup", "channel"),
                   default="supergroup", help="тип чата")
    p.add_argument("--about", default="", help="описание (канал / супергруппа)")
    p.add_argument("--forum", action="store_true", help="форум (только supergroup)")
    p.add_argument("--db", default=DB_PATH)

    p = sub.add_parser("edit", help="настройки чата: title / about / photo / username")
    p.add_argument("--chat", required=True, help="чат: @username / id / ссылка")
    p.add_argument("--title", help="новое название")
    p.add_argument("--about", help="новое описание")
    p.add_argument("--photo", help="путь к файлу нового фото")
    p.add_argument("--username", help="новый username; пустая строка убирает все")
    p.add_argument("--db", default=DB_PATH)

    p = sub.add_parser("toggle", help="тумблеры чата")
    p.add_argument("--chat", required=True, help="чат: @username / id / ссылка")
    p.add_argument("--pre-history", type=_on_off, help="скрытая предыстория (supergroup)")
    p.add_argument("--invites", type=_on_off, help="инвайты участников")
    p.add_argument("--signatures", type=_on_off, help="подписи под сообщениями")
    p.add_argument("--forum", type=_on_off, help="форум (supergroup)")
    p.add_argument("--db", default=DB_PATH)

    p = sub.add_parser("rights", help="дефолтные права участников")
    p.add_argument("--chat", required=True, help="чат: @username / id / ссылка")
    p.add_argument("--flag", action="append", type=_flag_pair, metavar="NAME=on|off",
                   help="право и значение, повторяется; имена: "
                        + ", ".join(f.replace("_", "-") for f in RIGHTS_FIELDS))
    p.add_argument("--db", default=DB_PATH)

    p = sub.add_parser("topic-create", help="новая тема форума")
    p.add_argument("--chat", required=True, help="форум: @username / id / ссылка")
    p.add_argument("--title", required=True, help="название темы")
    p.add_argument("--icon-color", type=int, help="цвет иконки темы")
    p.add_argument("--db", default=DB_PATH)

    p = sub.add_parser("topic-edit", help="правка темы форума")
    p.add_argument("--chat", required=True, help="форум: @username / id / ссылка")
    p.add_argument("--topic", type=int, required=True, help="id темы")
    p.add_argument("--title", help="новое название темы")
    p.add_argument("--closed", type=_on_off, help="закрыть/открыть тему")
    p.add_argument("--db", default=DB_PATH)

    p = sub.add_parser("topic-close", help="закрыть тему форума")
    p.add_argument("--chat", required=True, help="форум: @username / id / ссылка")
    p.add_argument("--topic", type=int, required=True, help="id темы")
    p.add_argument("--db", default=DB_PATH)

    p = sub.add_parser("topic-open", help="открыть тему форума")
    p.add_argument("--chat", required=True, help="форум: @username / id / ссылка")
    p.add_argument("--topic", type=int, required=True, help="id темы")
    p.add_argument("--db", default=DB_PATH)

    args = ap.parse_args()
    if args.cmd == "edit" and all(v is None for v in
                                   (args.title, args.about, args.photo, args.username)):
        ap.error("edit: нужен хотя бы один из --title --about --photo --username")
    if args.cmd == "toggle" and all(v is None for v in
                                    (args.pre_history, args.invites,
                                     args.signatures, args.forum)):
        ap.error("toggle: нужен хотя бы один из --pre-history --invites "
                 "--signatures --forum")
    if args.cmd == "topic-edit" and args.title is None and args.closed is None:
        ap.error("topic-edit: нужен хотя бы один из --title --closed")
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
