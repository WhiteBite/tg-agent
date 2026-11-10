"""Реестр функциональных модулей tg-agent.

Модуль — dict-контракт с полями:
  id        — str, уникальный идентификатор
  version   — int, версия схемы модуля
  schema    — fn(conn) с идемпотентным DDL; вызывается из миграции db.py (v7+)
  workspace — {(method, path): fn(db, params|body)} поверх workspace-API;
              GET-функции получают (db, params), POST — (db, body)
  mcp       — [{name, description, fn, gate?}] — MCP-инструменты; confirm
               живёт в самой функции модуля; gate (необяз.) — какой env-гейт
               сервера регистрирует инструмент: "downloads" (умолчание,
               TG_ALLOW_DOWNLOADS), "forwards" (TG_ALLOW_FORWARDS), "any"
               (любой из них) или имя произвольной env-переменной (напр.
               TG_ALLOW_X)
  runner    — fn(job_id) -> dict, запуск фонового исполнителя задания

Отключение: TG_MODULES_OFF="downloads,..." убирает модуль из реестра —
ни маршрутов, ни инструментов, ни исполнителя; схема такого модуля
не создаётся и задания его типа не выполняются. Файл выключенного модуля
не импортируется вовсе. TG_EXTRA_MODULES=<путь к .py-модулю со словарём
MODULES> добавляет сторонние модули поверх встроенных.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable

_OFF = {s.strip() for s in os.environ.get("TG_MODULES_OFF", "").split(",")
        if s.strip()}


def _validate(module: dict) -> None:
    def fail(reason: str) -> None:
        raise ValueError(f"module {module.get('id')}: {reason}")

    if not isinstance(module.get("id"), str) or not module["id"]:
        fail("id должен быть непустой строкой")
    if not isinstance(module.get("version"), int) or isinstance(module.get("version"), bool):
        fail("version должен быть int")
    for key in ("schema", "runner"):
        fn = module.get(key)
        if fn is not None and not callable(fn):
            fail(f"{key} должен быть вызываемым")
    workspace = module.get("workspace") or {}
    if not isinstance(workspace, dict):
        fail("workspace должен быть dict")
    for key, fn in workspace.items():
        if (not isinstance(key, tuple) or len(key) != 2
                or key[0] not in ("GET", "POST")
                or not isinstance(key[1], str)
                or not key[1].strip() or key[1].strip() != key[1]
                or key[1].startswith("/") or key[1].endswith("/")):
            fail(f"неверный ключ маршрута: {key!r}")
        if not callable(fn):
            fail(f"маршрут {key!r} не вызываемый")
    mcp = module.get("mcp") or []
    if not isinstance(mcp, list):
        fail("mcp должен быть list")
    for spec in mcp:
        if (not isinstance(spec, dict)
                or not isinstance(spec.get("name"), str)
                or not spec["name"].startswith("tg_")
                or not isinstance(spec.get("description"), str)
                or not spec.get("description")
                or not callable(spec.get("fn"))):
            fail(f"неверный mcp-спека: {spec.get('name')!r}")
        gate = spec.get("gate")
        if gate is not None and (not isinstance(gate, str) or not gate.strip()):
            fail(f"неверный gate mcp-спеки: {spec.get('name')!r}")


def _extra_modules() -> dict[str, dict]:
    path = os.environ.get("TG_EXTRA_MODULES")
    if not path:
        return {}
    spec = importlib.util.spec_from_file_location("tg_extra_modules", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"TG_EXTRA_MODULES: не py-модуль: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = getattr(mod, "MODULES", None)
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        return {m["id"]: m for m in raw}
    raise ValueError("TG_EXTRA_MODULES: MODULES должен быть dict или list")


MODULES: dict[str, dict] = {}
EXTRA_ERROR: dict | None = None

if "downloads" not in _OFF:
    from modules.downloads import MODULE as _DOWNLOADS

    _validate(_DOWNLOADS)
    MODULES[_DOWNLOADS["id"]] = _DOWNLOADS

try:
    for _mod in _extra_modules().values():
        if _mod["id"] in _OFF:
            continue
        _validate(_mod)
        if _mod["id"] in MODULES:
            raise ValueError(f"id {_mod['id']!r} уже занят встроенным модулем")
        MODULES[_mod["id"]] = _mod
except Exception as exc:
    EXTRA_ERROR = {"ok": False, "error": f"TG_EXTRA_MODULES: {exc}"}


def ensure_schema(conn) -> None:
    for mod in MODULES.values():
        mod["schema"](conn)


def workspace_routes() -> dict[tuple[str, str], Callable]:
    out: dict[tuple[str, str], Callable] = {}
    for mod in MODULES.values():
        out.update(mod.get("workspace") or {})
    return out


def mcp_specs() -> list[dict]:
    out: list[dict] = []
    for mod in MODULES.values():
        out.extend(mod.get("mcp") or [])
    return out


def spawn_runner(module_id: str, job_id: int) -> dict:
    mod = MODULES.get(module_id)
    if mod is None or not callable(mod.get("runner")):
        return {"ok": False, "error": f"нет исполнителя заданий у модуля {module_id!r}"}
    return mod["runner"](job_id)
