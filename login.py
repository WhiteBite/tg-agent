#!/usr/bin/env python3
"""Логин в Telegram один раз: сканируешь QR — сессия сохраняется навсегда.

    python login.py             # QR в терминале: Telegram -> Настройки -> Устройства -> Подключить
    python login.py --phone     # классика: телефон + код (если QR не подходит)
    python login.py --export    # после входа напечатать TELEGRAM_SESSION_STRING

Сессия пишется в tg_agent_session.session (или TELEGRAM_SESSION_NAME из .env);
все переменные читаются из .env, если он есть (см. .env.example).
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from env import load_env  # noqa: E402

load_env()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("нет TELEGRAM_API_ID / TELEGRAM_API_HASH: заполни .env "
                         "(шаблон .env.example, ключи — https://my.telegram.org/apps)")
    sess = os.environ.get("TELEGRAM_SESSION_STRING")
    name = os.environ.get("TELEGRAM_SESSION_NAME", "tg_agent_session")
    return TelegramClient(StringSession(sess) if sess else name, int(api_id), api_hash), name


async def _qr_login(client) -> None:
    import qrcode

    qr = await client.qr_login()
    print("Открой Telegram → Настройки → Устройства → Подключить устройство")
    print("и наведи камеру на код (живёт ~1 минуту, обновится сам):\n")
    while True:
        img = qrcode.QRCode(border=1)
        img.add_data(qr.url)
        img.print_ascii(invert=True)
        try:
            await qr.wait(timeout=15)
            return
        except asyncio.TimeoutError:
            await qr.recreate()
            print("\nкод обновлён, наведи снова:")


async def _sign_in(client) -> None:
    from telethon.errors import SessionPasswordNeededError

    try:
        await _qr_login(client)
    except ImportError:
        print("нет библиотеки qrcode (pip install qrcode) — переходим к телефону")
        await client.start(phone=os.environ.get("TELEGRAM_PHONE"))
    except SessionPasswordNeededError:
        print("QR принят, включена 2FA — нужен пароль")
    except Exception as exc:
        print(f"QR не сработал ({exc}) — переходим к телефону")
        if not client.is_connected():
            await client.connect()
        await client.start(phone=os.environ.get("TELEGRAM_PHONE"))


async def _finish(client, export: bool, session_name: str) -> None:
    from telethon.sessions import StringSession

    if not await client.is_user_authorized():
        pw = getpass.getpass("2FA-пароль: ")
        await client.sign_in(password=pw)

    me = await client.get_me()
    sp = Path(session_name + ".session")
    print(f"\nготово: {getattr(me, 'first_name', None)} (id={me.id})")
    print(f"сессия: {sp.resolve()} — это доступ к аккаунту, никому не отдавай")
    if export:
        print("\nTELEGRAM_SESSION_STRING (для серверов/контейнеров):")
        print(StringSession.save(client.session))
    await client.disconnect()


def main() -> None:
    use_phone = "--phone" in sys.argv
    export = "--export" in sys.argv

    async def run() -> None:
        client, name = _client()
        await client.connect()
        if await client.is_user_authorized():
            print("уже авторизован — входить не нужно")
        elif use_phone:
            await client.start(phone=os.environ.get("TELEGRAM_PHONE"))
        else:
            await _sign_in(client)
        await _finish(client, export, name)

    asyncio.run(run())


if __name__ == "__main__":
    main()
