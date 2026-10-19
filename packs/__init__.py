"""Реестр декларативных пакетов извлечения."""

from typing import NoReturn

from packs.construction import PACK as CONSTRUCTION
from packs.services import PACK as SERVICES

PACKS: dict[str, dict] = {"construction": CONSTRUCTION, "services": SERVICES}

_KNOWN_OPS = frozenset({"new", "update", "close", "reopen", "start", "block", "unblock"})
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


for _pack in PACKS.values():
    _validate(_pack)
