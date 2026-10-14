#!/usr/bin/env python3
"""Само-уведомления: отправка текста в свои Saved Messages.

    python notify.py "текст"                 # dry-run: показать, что ушло бы
    python notify.py --send "текст"          # реальная отправка (нужен TG_ALLOW_SEND=1)
    echo "текст" | python notify.py --send   # из stdin, построчно

Единственный модуль репозитория, который умеет писать в Telegram; MCP-сервер
его не импортирует, агент отправки по-прежнему не видит. Реальная отправка
только при TG_ALLOW_SEND=1; TG_NOTIFY_CAP (по умолчанию 10) ограничивает число
сообщений за вызов. Telethon импортируется лишь на пути реальной отправки.
"""

from __future__ import annotations

import json
import os
import sys

ALLOWED = ("1", "true", "yes")


def _allowed() -> bool:
    return os.environ.get("TG_ALLOW_SEND", "").strip().lower() in ALLOWED


def _send(messages: list[str]) -> dict:
    import asyncio

    from sync import make_client, start_kwargs

    async def _run() -> tuple[int, list[str]]:
        client = make_client()
        await client.start(**start_kwargs())
        sent = 0
        errors: list[str] = []
        try:
            for text in messages:
                try:
                    await client.send_message("me", text)
                    sent += 1
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            await client.disconnect()
        return sent, errors

    sent, errors = asyncio.run(_run())
    if errors:
        return {"ok": False, "sent": sent, "errors": errors}
    return {"ok": True, "sent": sent}


def notify(messages: list[str], dry_run: bool = True) -> dict:
    if dry_run:
        return {"ok": True, "dry_run": True, "planned": list(messages)}
    if not _allowed():
        return {"ok": False, "error": "send заблокирован: нужен TG_ALLOW_SEND=1"}
    cap = int(os.environ.get("TG_NOTIFY_CAP", "10"))
    to_send = list(messages)[:cap]
    truncated = max(len(messages) - cap, 0)
    result = _send(to_send)
    result["truncated"] = truncated
    return result


def main() -> None:
    args = sys.argv[1:]
    send = "--send" in args
    texts = [a for a in args if not a.startswith("--")]
    if not texts:
        texts = [ln for ln in sys.stdin.read().splitlines() if ln.strip()]
    print(json.dumps(notify(texts, dry_run=not send), ensure_ascii=False))


if __name__ == "__main__":
    main()
