#!/usr/bin/env python3
"""Валидация схем пакетов на импорте packs: обязательные поля, ops<->enum, плейсхолдеры."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

import packs  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def expect_valueerror(pack: dict, label: str) -> None:
    pack_id = str(pack.get("id"))
    try:
        packs._validate(pack)
    except ValueError as exc:
        check(pack_id in str(exc), label, str(exc))
    else:
        check(False, label, "ValueError не поднята")


def expect_ok(pack: dict, label: str) -> None:
    try:
        packs._validate(pack)
    except ValueError as exc:
        check(False, label, str(exc))
    else:
        check(True, label)


def test_import_yields_both_packs() -> None:
    check(set(packs.PACKS) == {"construction", "services"},
          "PACKS содержит оба валидных пакета", str(sorted(packs.PACKS)))


def test_real_packs_pass() -> None:
    for name in ("construction", "services"):
        expect_ok(packs.PACKS[name], f"реальный пакет {name} проходит _validate без изменений")


def test_validate_does_not_mutate() -> None:
    for name in ("construction", "services"):
        snapshot = copy.deepcopy(packs.PACKS[name])
        packs._validate(packs.PACKS[name])
        check(packs.PACKS[name] == snapshot, f"_validate не мутирует пакет {name}")


def test_broken_packs_rejected() -> None:
    base = packs.PACKS["construction"]

    no_kinds = copy.deepcopy(base)
    del no_kinds["kinds"]
    expect_valueerror(no_kinds, "kinds отсутствует -> ValueError с id пакета")

    empty_kinds = copy.deepcopy(base)
    empty_kinds["kinds"] = {}
    expect_valueerror(empty_kinds, "kinds пустой -> ValueError с id пакета")

    enum_wider = copy.deepcopy(base)
    enum_wider["json_schema"]["properties"]["ops"]["items"]["properties"]["op"]["enum"].append("start")
    expect_valueerror(enum_wider, "enum схемы шире ops -> ValueError с id пакета")

    ops_wider = copy.deepcopy(base)
    ops_wider["ops"] = ops_wider["ops"] + ["start"]
    expect_valueerror(ops_wider, "ops шире enum схемы -> ValueError с id пакета")

    unknown_op = copy.deepcopy(base)
    unknown_op["ops"] = ["new", "explode"]
    expect_valueerror(unknown_op, "ops вне известного набора -> ValueError")

    no_enum = copy.deepcopy(base)
    del no_enum["json_schema"]["properties"]["ops"]["items"]["properties"]["op"]["enum"]
    expect_valueerror(no_enum, "enum ops отсутствует в json_schema -> ValueError")

    bad_template = copy.deepcopy(base)
    bad_template["prompt_template"] = bad_template["prompt_template"].replace("{messages}", "<messages>")
    expect_valueerror(bad_template, "prompt_template без {messages} -> ValueError с id пакета")

    for field, bad_value, label in [
        ("id", 7, "id не str"),
        ("version", "1", "version не int"),
        ("version", True, "version bool отклоняется"),
        ("backfill_days", 14.0, "backfill_days не int"),
        ("field_policies", [], "field_policies не dict"),
        ("ops", "new", "ops не список"),
        ("json_schema", "schema", "json_schema не dict"),
    ]:
        mutated = copy.deepcopy(base)
        mutated[field] = bad_value
        expect_valueerror(mutated, f"{label} -> ValueError")


def test_known_ops_include_defensive_extras() -> None:
    extended = copy.deepcopy(packs.PACKS["services"])
    extended["ops"] = ["new", "update", "close", "reopen", "start", "block", "unblock"]
    extended["json_schema"]["properties"]["ops"]["items"]["properties"]["op"]["enum"] = list(extended["ops"])
    expect_ok(extended, "start/block/unblock входят в известный набор ops")


def test_import_in_subprocess(root: Path) -> None:
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent))
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "import packs; assert set(packs.PACKS) == {'construction', 'services'}"],
        cwd=str(root), env=env, capture_output=True, text=True)
    check(proc.returncode == 0, "import packs из чужого cwd проходит валидацию на импорте",
          proc.stderr.strip()[:300])


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="pack_schema_"))
    test_import_yields_both_packs()
    test_real_packs_pass()
    test_validate_does_not_mutate()
    test_broken_packs_rejected()
    test_known_ops_include_defensive_extras()
    test_import_in_subprocess(root)
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
