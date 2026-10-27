"""Реестр функциональных модулей tg-agent.

Модуль — dict-контракт с полями:
  id        — str, уникальный идентификатор
  version   — int, версия схемы модуля
  schema    — fn(conn) с идемпотентным DDL; вызывается из миграции db.py (v7+)
  workspace — {(method, path): fn(db, params|body)} поверх workspace-API;
              GET-функции получают (db, params), POST — (db, body)
  mcp       — [{name, description, fn}] — MCP-инструменты; гейт и confirm
              живут в самой функции модуля
  runner    — fn(job_id) -> dict, запуск фонового исполнителя задания

Отключение: TG_MODULES_OFF="downloads,..." убирает модуль из реестра —
ни маршрутов, ни инструментов, ни исполнителя; схема такого модуля
не создаётся и задания его типа не выполняются.
"""

from __future__ import annotations

import os
from typing import Callable

from modules.downloads import MODULE as _DOWNLOADS


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


_OFF = {s.strip() for s in os.environ.get("TG_MODULES_OFF", "").split(",")
        if s.strip()}

MODULES: dict[str, dict] = {}
for _mod in (_DOWNLOADS,):
    if _mod["id"] not in _OFF:
        _validate(_mod)
        MODULES[_mod["id"]] = _mod


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
