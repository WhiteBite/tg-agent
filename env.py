"""Загрузка .env из папки проекта: KEY=VALUE без затирания уже заданных переменных.

Креды и пути задаются один раз в .env (шаблон — .env.example), и все входные
скрипты (sync/live/watch/doctor/server/ui/digest/login) подхватывают их сами.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path | None = None) -> None:
    p = path if path is not None else Path(__file__).parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
