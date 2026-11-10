"""Реестр декларативных пакетов извлечения."""

import importlib.util
import os
from typing import NoReturn

from packs.construction import PACK as CONSTRUCTION
from packs.services import PACK as SERVICES

PACKS: dict[str, dict] = {"construction": CONSTRUCTION, "services": SERVICES}

_KNOWN_OPS = frozenset({"new", "update", "close", "reopen", "start", "block", "unblock", "link"})
_PROMPT_PLACEHOLDERS = ("{scope_header}", "{messages}", "{open_entities}")
_JSON_ENUM_PATH = ("properties", "ops", "items", "properties", "op", "enum")


def _validate(pack: dict) -> None:
    def fail(reason: str) -> NoReturn:
        raise ValueError(f"pack {pack.get('id')}: {reason}")

    if not isinstance(pack.get("id"), str):
        fail("id должен быть str")
    version = pack.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        fail("version должен быть int")
    kinds = pack.get("kinds")
    if not isinstance(kinds, dict) or not kinds:
        fail("kinds должен быть непустым dict")
    template = pack.get("prompt_template")
    if not isinstance(template, str):
        fail("prompt_template должен быть str")
    missing = [ph for ph in _PROMPT_PLACEHOLDERS if ph not in template]
    if missing:
        fail(f"prompt_template без плейсхолдеров: {missing}")
    ops = pack.get("ops")
    if not isinstance(ops, list):
        fail("ops должен быть списком")
    outside = [op for op in ops if op not in _KNOWN_OPS]
    if outside:
        fail(f"ops вне известного набора: {outside}")
    schema = pack.get("json_schema")
    if not isinstance(schema, dict):
        fail("json_schema должен быть dict")
    node: object = schema
    for key in _JSON_ENUM_PATH:
        if not isinstance(node, dict):
            fail("в json_schema нет пути properties/ops/items к enum операции")
        node = node.get(key)
    if not isinstance(node, list):
        fail("enum операции op в json_schema должен быть списком")
    if set(node) != set(ops):
        fail(f"enum op в json_schema != ops: {sorted(node)} vs {sorted(ops)}")
    backfill = pack.get("backfill_days")
    if not isinstance(backfill, int) or isinstance(backfill, bool):
        fail("backfill_days должен быть int")
    if not isinstance(pack.get("field_policies"), dict):
        fail("field_policies должен быть dict")


def _extra_packs() -> dict[str, dict]:
    path = os.environ.get("TG_EXTRA_PACKS")
    if not path:
        return {}
    spec = importlib.util.spec_from_file_location("tg_extra_packs", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"TG_EXTRA_PACKS: не py-модуль: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = getattr(mod, "PACKS", None)
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        return {p["id"]: p for p in raw}
    raise ValueError("TG_EXTRA_PACKS: PACKS должен быть dict или list")


for _pack in PACKS.values():
    _validate(_pack)

EXTRA_ERROR: dict | None = None
try:
    for _pack in _extra_packs().values():
        _validate(_pack)
        if _pack["id"] in PACKS:
            raise ValueError(f"id {_pack['id']!r} уже занят встроенным паком")
        PACKS[_pack["id"]] = _pack
except Exception as exc:
    EXTRA_ERROR = {"ok": False, "error": f"TG_EXTRA_PACKS: {exc}"}
