#!/usr/bin/env python3
"""Мультиаккаунт: разрешение конфигурации аккаунтов из окружения.

    TG_ACCOUNTS=work,home
    TG_ACCOUNT_WORK_SESSION=tg_agent_session_work
    TG_ACCOUNT_WORK_API_ID=123456
    TG_ACCOUNT_WORK_API_HASH=...
    TG_ACCOUNT_HOME_SESSION=tg_agent_session_home

Без TG_ACCOUNTS — единственный неявный аккаунт `default` из текущих
TELEGRAM_* (нулевая цена: ни строк в БД, ни лишних запросов). Аккаунт — это
свойство ингеста (какая сессия читает) и доступа к чату, а не измерение
сущностей: chat_id и msg_id каналов/супергрупп глобальны, поэтому два аккаунта
пишут одни и те же строки в общую БД и сущности сливаются штатным dedup_key.
"""

from __future__ import annotations

import os

DEFAULT = "default"


def _names() -> list[str]:
    raw = os.environ.get("TG_ACCOUNTS", "")
    return [n.strip() for n in raw.split(",") if n.strip()]


def is_multi() -> bool:
    """True, когда TG_ACCOUNTS задаёт хотя бы один явный аккаунт."""
    return bool(_names())


def account_names() -> list[str]:
    """Аккаунты для ингеста: явные из TG_ACCOUNTS либо неявный `default`."""
    return _names() or [DEFAULT]


def configured_accounts() -> list[str]:
    """Аккаунты для линзы UI: пусто, если мультиаккаунт не настроен (нулевая цена)."""
    return _names()


def _key(name: str, suffix: str) -> str:
    return f"TG_ACCOUNT_{name.upper()}_{suffix}"


def resolve(name: str | None = None) -> dict:
    """Параметры клиента аккаунта: id, session_string, session_name, api_id,
    api_hash, label, enabled. Неявный `default` берёт текущие TELEGRAM_*."""
    name = name or DEFAULT
    if name == DEFAULT and not is_multi():
        return {
            "id": DEFAULT,
            "session_string": os.environ.get("TELEGRAM_SESSION_STRING") or None,
            "session_name": os.environ.get("TELEGRAM_SESSION_NAME", "tg_agent_session"),
            "api_id": os.environ.get("TELEGRAM_API_ID"),
            "api_hash": os.environ.get("TELEGRAM_API_HASH"),
            "label": DEFAULT,
            "enabled": True,
        }
    return {
        "id": name,
        "session_string": os.environ.get(_key(name, "SESSION_STRING")) or None,
        "session_name": os.environ.get(_key(name, "SESSION")) or f"tg_agent_session_{name}",
        "api_id": os.environ.get(_key(name, "API_ID")) or os.environ.get("TELEGRAM_API_ID"),
        "api_hash": os.environ.get(_key(name, "API_HASH")) or os.environ.get("TELEGRAM_API_HASH"),
        "label": os.environ.get(_key(name, "LABEL")) or name,
        "enabled": os.environ.get(_key(name, "DISABLED"), "").strip().lower()
        not in ("1", "true", "yes"),
    }
