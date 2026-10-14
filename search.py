#!/usr/bin/env python3
"""Глобальный поиск Telegram: публичные каналы/группы/пользователи по слову.

    python3 search.py "нейросети"                # всё подряд
    python3 search.py "нейросети" --type channel # только каналы-вещания
    python3 search.py "ИИ" --type group --limit 30
    python3 search.py "заказчики" --type user

Обёртка над messages.SearchGlobalRequest: сервер Telegram сам ищет по
названиям и содержимому публичных чатов — это обычный API-запрос, не парсинг.
Результат — kind/title/username/id/подписчики; чтобы агент мог читать
найденное, добавь чат синком: python3 sync.py --chat @username — и отмечай
нужные темы через watch.py. Поиск только читает, ничего не отправляет.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

import sync as sync_mod  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

_KIND_RU = {
    "channel": "канал",
    "supergroup": "группа",
    "group": "группа",
    "user": "юзер",
}


def _marked(ch: Any) -> int:
    if type(ch).__name__ == "Channel":
        return -1000000000000 - int(ch.id)
    return -int(ch.id)


async def search_global(client: Any, query: str, limit: int = 20,
                        kind: Optional[str] = None) -> list[dict]:
    """Публичные чаты и пользователи по ключевому слову.

    kind: 'channel' (каналы-вещания), 'group' (группы/супергруппы),
    'user' или None — всё вместе. Возвращает список словарей
    {chat_id, title, username, kind, participants, verified}; chat_id —
    в marked-форме, как в индексе.
    """
    from telethon.tl import functions, types
    kwargs: dict = {}
    if kind == "channel":
        kwargs["broadcasts_only"] = True
    elif kind == "group":
        kwargs["groups_only"] = True
    elif kind == "user":
        kwargs["users_only"] = True
    elif kind is not None:
        raise ValueError(f"неизвестный тип: {kind}")
    res = await client(functions.messages.SearchGlobalRequest(
        q=query,
        filter=types.InputMessagesFilterEmpty(),
        min_date=None, max_date=None,
        offset_rate=0,
        offset_peer=types.InputPeerEmpty(),
        offset_id=0,
        limit=max(1, min(int(limit), 100)),
        **kwargs,
    ))
    out: list[dict] = []
    for ch in getattr(res, "chats", None) or []:
        title = getattr(ch, "title", None)
        cid = getattr(ch, "id", None)
        if title is None or cid is None:
            continue
        out.append({
            "chat_id": _marked(ch),
            "title": str(title),
            "username": getattr(ch, "username", None),
            "kind": sync_mod.kind_of(ch),
            "participants": getattr(ch, "participants_count", None),
            "verified": bool(getattr(ch, "verified", False)),
        })
    for u in getattr(res, "users", None) or []:
        uname = getattr(u, "username", None)
        if not uname:
            continue
        out.append({
            "chat_id": int(u.id),
            "title": " ".join(filter(None, (getattr(u, "first_name", None),
                                            getattr(u, "last_name", None)))),
            "username": uname,
            "kind": "user",
            "participants": None,
            "verified": bool(getattr(u, "verified", False)),
        })
    return out


async def _amain(args: argparse.Namespace) -> int:
    client = sync_mod.make_client()
    await client.start(**sync_mod.start_kwargs())
    try:
        rows = await search_global(client, args.query, limit=args.limit, kind=args.type)
    finally:
        await client.disconnect()
    if not rows:
        print("ничего не нашлось; попробуй другое слово или --type")
        return 0
    print(f"найдено: {len(rows)}")
    for r in rows:
        handle = f"@{r['username']}" if r["username"] else str(r["chat_id"])
        parts = [f"{_KIND_RU.get(r['kind'], r['kind']):10}", r["title"], handle]
        if r["participants"]:
            parts.append(f"{r['participants']} подписчиков")
        if r["verified"]:
            parts.append("verified")
        print("  " + "  ".join(str(p) for p in parts))
    print("синхронизировать найденное: python3 sync.py --chat @username")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Поиск публичных каналов/групп/пользователей Telegram")
    ap.add_argument("query", help="ключевое слово")
    ap.add_argument("--type", choices=["channel", "group", "user"],
                    help="сузить результат по типу")
    ap.add_argument("--limit", type=int, default=20, help="максимум результатов")
    sys.exit(asyncio.run(_amain(ap.parse_args())))


if __name__ == "__main__":
    main()
