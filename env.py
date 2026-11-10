"""Загрузка .env из папки проекта: KEY=VALUE без затирания уже заданных переменных.

Креды и пути задаются один раз в .env (шаблон — .env.example), и все входные
скрипты (sync/live/watch/doctor/server/ui/digest/login) подхватывают их сами.
Файл может быть в UTF-8, UTF-8 с BOM (notepad) или UTF-16 (Out-File в Windows PowerShell).

Профили конфигурации: TG_PROFILE=<имя> включает набор значений из профиля —
env-файла `.env.<имя>` (или TG_PROFILE_FILE) и/или meta-ключа
`config:profile:<имя>`. Профиль применяется только к ключам, которых не было в
реальном окружении процесса до load_env(); без TG_PROFILE — полный no-op.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / ".env"
DEFAULT_DB = Path(__file__).parent / "tg_index.sqlite"

# Профиль не вправе менять ключи, управляющие его же разрешением и выбором базы (рекурсия).
_BOOTSTRAP_KEYS = frozenset({"TG_DB", "TG_PROFILE", "TG_PROFILE_FILE", "TG_ENV_FILE"})


def harden_stdio() -> None:
    """Консоль Windows по умолчанию cp1251: эмодзи в названиях чатов роняли
    print (UnicodeEncodeError) и обрывали sync/live/digest на ровном месте.
    Режим replace сохраняет кириллицу и не падает ни на одном символе."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _read_text(p: Path) -> str:
    raw = p.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def load_env(path: Path | None = None) -> None:
    harden_stdio()
    pre = set(os.environ)
    if path is None:
        override = os.environ.get("TG_ENV_FILE")
        if override is not None:
            if not override:
                return  # TG_ENV_FILE="" — не читать .env вообще (тесты/CI)
            path = Path(override)
        else:
            path = DEFAULT_PATH
    p = path
    if p.exists():
        for line in _read_text(p).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    _apply_profile(p, pre)


def _parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


def _profile_file(main_path: Path, name: str) -> Path:
    override = os.environ.get("TG_PROFILE_FILE")
    if override:
        return Path(override)
    return main_path.parent / f".env.{name}"


def _read_meta_profile(name: str) -> dict[str, str]:
    db_path = os.environ.get("TG_DB") or str(DEFAULT_DB)
    p = Path(db_path)
    if not p.exists():
        return {}
    try:
        conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"config:profile:{name}",)
        ).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    if not row or not row[0]:
        return {}
    try:
        data = json.loads(row[0])
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _apply_profile(main_path: Path, pre: set[str]) -> None:
    name = os.environ.get("TG_PROFILE")
    if not name:
        return
    values: dict[str, str] = {}
    values.update(_read_meta_profile(name))
    pf = _profile_file(main_path, name)
    if pf.exists():
        values.update(_parse_env_text(_read_text(pf)))
    for key, val in values.items():
        if key in _BOOTSTRAP_KEYS or key in pre:
            continue
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
