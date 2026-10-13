#!/usr/bin/env python3
"""Одна команда вместо десяти: логин (если нужно) -> синк -> визуал.

    python start.py

Первый раз: спросит ключи, покажет QR, предложит выбрать чаты списком по номерам.
Дальше: сам обновляет отмеченное и открывает визуал — без флагов и имён.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402
import login as login_mod  # noqa: E402
import sync as sync_mod  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))
UI_PORT = os.environ.get("PORT", "8899")
UI_URL = f"http://127.0.0.1:{UI_PORT}"

KIND_LABEL = {"forum": "форум с темами", "group": "группа", "channel": "канал"}


def chat_name(chat: dict) -> str:
    """Что скормить sync.select_chats: @username, иначе числовой id."""
    if chat.get("username"):
        return f"@{chat['username']}"
    return str(chat["id"])


def parse_pick(text: str, chats: list[dict]) -> list[str]:
    """Ответ визарда -> имена для sync: "1,3" | "all" | "@user" | "" (отмена)."""
    text = (text or "").strip()
    if not text:
        return []
    if text.lower() in ("all", "все", "всё"):
        return [chat_name(c) for c in chats]
    picked: list[str] = []
    for part in text.split(","):
        part = part.strip()
        if part.isdigit():
            i = int(part)
            if 1 <= i <= len(chats):
                picked.append(chat_name(chats[i - 1]))
        elif part.startswith("@"):
            picked.append(part)
    return picked


def print_choices(chats: list[dict], total: int | None = None) -> None:
    total = total or len(chats)
    head = f"Что можно слить (группы — с темами, каналы — лентой)"
    if total > len(chats):
        head += f" — показаны {len(chats)} из {total}; любой другой чат вводи как @username"
    print(f"\n{head}:")
    for i, c in enumerate(chats, start=1):
        un = f"  @{c['username']}" if c.get("username") else ""
        print(f"  {i:>3}. [{KIND_LABEL.get(c['kind'], c['kind'])}] {c['title']!r}{un}")


async def pick_and_sync() -> None:
    client = sync_mod.make_client()
    await client.connect()
    try:
        chats = await sync_mod.list_chat_choices(client)
    finally:
        await client.disconnect()
    shown = chats[:40]
    print_choices(shown, total=len(chats))
    names = parse_pick(input("\nЧто слить? номера через запятую, all, @username "
                             "(Enter — пропустить): "), shown)
    raw = input("За сколько дней? (Enter — 30, 0 — вся история): ").strip()
    try:
        days = None if raw == "0" else (int(raw) if raw else 30)
    except ValueError:
        days = 30
    if names:
        await sync_mod.run(db_path=DB_PATH, names=names, days=days)
    else:
        print("ничего не выбрано — останется только визуал")


async def main() -> None:
    client = sync_mod.make_client()
    await client.connect()
    try:
        authorized = await client.is_user_authorized()
    finally:
        await client.disconnect()
    if not authorized:
        print("Нужно войти в Telegram — сейчас покажу QR.")
        await login_mod.run_login()

    with DB(DB_PATH) as db:
        watched = db.watch_list()
        n_msgs = db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    if n_msgs and watched:
        print(f"Обновляю отмеченное ({len(watched)} тем)…")
        await sync_mod.run(db_path=DB_PATH, watched_only=True)
    else:
        await pick_and_sync()

    if input("\nОткрыть визуал в браузере? [Y/n]: ").strip().lower() != "n":
        subprocess.Popen([sys.executable, str(HERE / "ui.py")], cwd=str(HERE),
                         env=dict(os.environ, TG_DB=DB_PATH))
        webbrowser.open(UI_URL)
        print(f"Визуал: {UI_URL}\nОтметь темы звёздочкой — их увидит агент.\n"
              f"В следующий раз просто: python start.py")
    else:
        print("ок — вернуться можно той же командой: python start.py")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (EOFError, ValueError) as exc:
        raise SystemExit(f"нужен интерактивный терминал ({exc})")