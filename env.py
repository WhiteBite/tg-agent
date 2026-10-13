"""Загрузка .env из папки проекта: KEY=VALUE без затирания уже заданных переменных.

Креды и пути задаются один раз в .env (шаблон — .env.example), и все входные
скрипты (sync/live/watch/doctor/server/ui/digest/login) подхватывают их сами.
Файл может быть в UTF-8, UTF-8 с BOM (notepad) или UTF-16 (Out-File в Windows PowerShell).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / ".env"


def _read_text(p: Path) -> str:
    raw = p.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def load_env(path: Path | None = None) -> None:
    p = path if path is not None else DEFAULT_PATH
    if not p.exists():
        return
    for line in _read_text(p).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def write_env(values: dict[str, str], path: Path | None = None) -> None:
    """Записывает ключи в .env, не трогая остальные строки."""
    p = path if path is not None else DEFAULT_PATH
    lines = _read_text(p).splitlines() if p.exists() else []
    have = set()
    out: list[str] = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if key in values:
            out.append(f"{key}={values[key]}")
            have.add(key)
        else:
            out.append(line)
    for key, val in values.items():
        if key not in have:
            out.append(f"{key}={val}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
