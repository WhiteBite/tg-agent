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
from env import load_env, write_env  # noqa: E402
from sync import start_kwargs  # noqa: E402

load_env()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def ensure_creds(env_path: Path | None = None) -> None:
    """Нет ключей — спрашивает один раз и сохраняет в .env, дальше никогда."""
    if os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH"):
        return
    print("Ключей ещё нет. Возьми на https://my.telegram.org/apps -> API development tools")
    print("(Log in → Telegram Core → Create new application, ключи появятся справа).\n")
    api_id = input("api_id: ").strip()
    api_hash = input("api_hash: ").strip()
    if not (api_id.isdigit() and api_hash):
        raise SystemExit("ключи не похожи на настоящие — сохрани сам через .env и повтори")
    write_env({"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": api_hash}, path=env_path)
    load_env(env_path)
    print("\nсохранено в .env: при следующих запусках не спросит")


def _client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
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
        await client.start(**start_kwargs())
    except SessionPasswordNeededError:
        print("QR принят, включена 2FA — нужен пароль")
    except Exception as exc:
        print(f"QR не сработал ({exc}) — переходим к телефону")
        if not client.is_connected():
            await client.connect()
        await client.start(**start_kwargs())


def _needs_password(client) -> bool:
    """QR уже импортирован (auth_key есть), но юзер не авторизован -> ждём облачный пароль."""
    return client.session.auth_key is not None


async def _password_info(client):
    """(подсказка, есть_резервная_почта) из account.GetPassword; (None, False) при сбое."""
    try:
        from telethon.tl import functions

        pw = await client(functions.account.GetPasswordRequest())
        return (getattr(pw, "hint", None) or None, bool(getattr(pw, "has_recovery", False)))
    except Exception:
        return None, False


async def _password_hint(client):
    return (await _password_info(client))[0]


async def _has_recovery(client) -> bool:
    return (await _password_info(client))[1]


async def _password_flow(client) -> bool:
    """Спрашивает 2FA-пароль до трёх раз; True — вошли."""
    from telethon.errors import PasswordHashInvalidError

    hint, recovery = await _password_info(client)
    if hint:
        print(f"подсказка к паролю (её задавал ты сам): {hint!r}")
    if recovery:
        print("есть резервная почта: если пароль забыт — Настройки → Приватность → "
              "Двухэтапная проверка → «Забыли пароль?»")

    fails = 0
    while fails < 3:
        pw = getpass.getpass("2FA-пароль (Telegram → Настройки → Приватность → "
                             "Двухэтапная проверка): ")
        if not pw:
            print("пустой ввод — введи пароль (Ctrl-C чтобы выйти)")
            continue
        try:
            await client.sign_in(password=pw)
            return True
        except PasswordHashInvalidError:
            fails += 1
            print(f"пароль не подошёл ({fails}/3); проверь раскладку — ввод не отображается")
    return False


async def _finish(client, export: bool, session_name: str) -> None:
    from telethon.sessions import StringSession

    if not await client.is_user_authorized():
        if not await _password_flow(client):
            raise SystemExit("пароль не подошёл 3 раза. Сбрось/смени его в Telegram "
                             "(Настройки → Приватность → Двухэтапная проверка) и запусти login.py заново")

    me = await client.get_me()
    sp = Path(session_name + ".session")
    print(f"\nготово: {getattr(me, 'first_name', None)} (id={me.id})")
    print(f"сессия: {sp.resolve()} — это доступ к аккаунту, никому не отдавай")
    if export:
        print("\nTELEGRAM_SESSION_STRING (для серверов/контейнеров):")
        print(StringSession.save(client.session))
    await client.disconnect()


async def run_login(*, use_phone: bool = False, export: bool = False) -> None:
    """Полный вход: ключи -> сессия (QR/телефон только если нужно). Для start.py тоже."""
    ensure_creds()
    client, name = _client()
    await client.connect()
    if await client.is_user_authorized():
        print("уже авторизован — входить не нужно")
    elif use_phone:
        await client.start(**start_kwargs())
    elif _needs_password(client):
        print("QR уже принят ранее — остался только пароль")
    else:
        await _sign_in(client)
    await _finish(client, export, name)


def main() -> None:
    use_phone = "--phone" in sys.argv
    export = "--export" in sys.argv

    try:
        asyncio.run(run_login(use_phone=use_phone, export=export))
    except (EOFError, ValueError) as exc:
        raise SystemExit(f"не получилось спросить телефон/код ({exc}) — "
                         "запусти в терминале и проверь ключи в .env")


if __name__ == "__main__":
    main()
