#!/usr/bin/env python3
"""Профили конфигурации: TG_PROFILE, env-файл .env.<имя>, meta config:profile:<имя>."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

import env as env_mod
from db import DB

FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


@contextlib.contextmanager
def env_guard(**set_env):
    saved = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.startswith("TG_") or k in ("FOO", "BAR", "A", "B", "C", "D"):
                del os.environ[k]
        os.environ.update({k: str(v) for k, v in set_env.items()})
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def make_db(path: Path, name: str, values: dict) -> None:
    with DB(path) as db:
        db.set_meta(f"config:profile:{name}", json.dumps(values, ensure_ascii=False))


def test_unset_noop(tmp: Path) -> None:
    print("=== TG_PROFILE не задан -> no-op ===")
    env_file = tmp / ".env"
    env_file.write_text("FOO=base\n", encoding="utf-8")
    db = tmp / "noop.sqlite"
    make_db(db, "p1", {"FOO": "meta", "BAR": "x"})
    with env_guard(TG_DB=str(db)):
        env_mod.load_env(path=env_file)
        check(os.environ.get("FOO") == "base",
              "unset: базовый .env применён", os.environ.get("FOO"))
        check("BAR" not in os.environ,
              "unset: meta-профиль не применён", os.environ.get("BAR"))
    ghost = tmp / "ghost.sqlite"
    with env_guard(TG_DB=str(ghost)):
        env_mod.load_env(path=env_file)
        check(not ghost.exists(), "unset: БД не открывается и не создаётся")


def test_meta_applied(tmp: Path) -> None:
    print("=== meta-профиль применён ===")
    env_file = tmp / ".env"
    if env_file.exists():
        env_file.unlink()
    db = tmp / "meta.sqlite"
    make_db(db, "p1", {"FOO": "meta", "BAR": "x"})
    with env_guard(TG_PROFILE="p1", TG_DB=str(db)):
        env_mod.load_env(path=env_file)
        check(os.environ.get("FOO") == "meta", "meta: FOO применён", os.environ.get("FOO"))
        check(os.environ.get("BAR") == "x", "meta: BAR применён", os.environ.get("BAR"))


def test_envfile_applied(tmp: Path) -> None:
    print("=== env-файл профиля применён ===")
    env_file = tmp / ".env"
    if env_file.exists():
        env_file.unlink()
    (tmp / ".env.p1").write_text("FOO=file\nBAR=file2\n", encoding="utf-8")
    ghost = tmp / "ghost2.sqlite"
    with env_guard(TG_PROFILE="p1", TG_DB=str(ghost)):
        env_mod.load_env(path=env_file)
        check(os.environ.get("FOO") == "file", "env-file: FOO применён", os.environ.get("FOO"))
        check(os.environ.get("BAR") == "file2", "env-file: BAR применён", os.environ.get("BAR"))


def test_process_env_wins(tmp: Path) -> None:
    print("=== окружение процесса важнее профиля ===")
    env_file = tmp / ".env"
    if env_file.exists():
        env_file.unlink()
    (tmp / ".env.p1").write_text("FOO=file\n", encoding="utf-8")
    with env_guard(TG_PROFILE="p1", FOO="proc"):
        env_mod.load_env(path=env_file)
        check(os.environ.get("FOO") == "proc",
              "process env: FOO не перетёрт профилем", os.environ.get("FOO"))


def test_bootstrap_guard(tmp: Path) -> None:
    print("=== профиль не может задать bootstrap-ключи ===")
    env_file = tmp / ".env"
    if env_file.exists():
        env_file.unlink()
    (tmp / ".env.p1").write_text(
        "TG_DB=/evil\nTG_PROFILE=evil\nTG_PROFILE_FILE=/evil\nTG_ENV_FILE=/evil\nFOO=ok\n",
        encoding="utf-8")
    real_db = tmp / "real.sqlite"
    with env_guard(TG_PROFILE="p1", TG_DB=str(real_db)):
        env_mod.load_env(path=env_file)
        check(os.environ.get("TG_DB") == str(real_db),
              "bootstrap: TG_DB не изменён", os.environ.get("TG_DB"))
        check(os.environ.get("TG_PROFILE") == "p1",
              "bootstrap: TG_PROFILE не изменён", os.environ.get("TG_PROFILE"))
        check(os.environ.get("TG_PROFILE_FILE") != "/evil",
              "bootstrap: TG_PROFILE_FILE не задан профилем", os.environ.get("TG_PROFILE_FILE"))
        check(os.environ.get("TG_ENV_FILE") != "/evil",
              "bootstrap: TG_ENV_FILE не задан профилем", os.environ.get("TG_ENV_FILE"))
        check(os.environ.get("FOO") == "ok",
              "bootstrap: обычный ключ применён", os.environ.get("FOO"))


def test_precedence(tmp: Path) -> None:
    print("=== env-файл профиля важнее meta; профиль важнее базового .env ===")
    env_file = tmp / ".env"
    env_file.write_text("FOO=base\nBASE_ONLY=b\n", encoding="utf-8")
    (tmp / ".env.p1").write_text("FOO=file\nFILE_ONLY=f\n", encoding="utf-8")
    db = tmp / "prec.sqlite"
    make_db(db, "p1", {"FOO": "meta", "META_ONLY": "m"})
    with env_guard(TG_PROFILE="p1", TG_DB=str(db)):
        env_mod.load_env(path=env_file)
        check(os.environ.get("FOO") == "file",
              "precedence: env-file > meta", os.environ.get("FOO"))
        check(os.environ.get("META_ONLY") == "m",
              "precedence: meta применён", os.environ.get("META_ONLY"))
        check(os.environ.get("FILE_ONLY") == "f",
              "precedence: env-file применён", os.environ.get("FILE_ONLY"))
        check(os.environ.get("BASE_ONLY") == "b",
              "precedence: базовый .env применён", os.environ.get("BASE_ONLY"))


def test_full_precedence(tmp: Path) -> None:
    print("=== полная цепочка: process > env-file > meta > base .env ===")
    env_file = tmp / ".env"
    env_file.write_text("A=base\nB=base\nC=base\nD=base\n", encoding="utf-8")
    (tmp / ".env.p1").write_text("B=file\nC=file\n", encoding="utf-8")
    db = tmp / "full.sqlite"
    make_db(db, "p1", {"C": "meta", "D": "meta"})
    with env_guard(TG_PROFILE="p1", TG_DB=str(db), A="proc"):
        env_mod.load_env(path=env_file)
        check(os.environ.get("A") == "proc", "full: A process env", os.environ.get("A"))
        check(os.environ.get("B") == "file", "full: B env-file", os.environ.get("B"))
        check(os.environ.get("C") == "file", "full: C env-file > meta", os.environ.get("C"))
        check(os.environ.get("D") == "meta", "full: D meta > base", os.environ.get("D"))


def test_profile_file_override(tmp: Path) -> None:
    print("=== TG_PROFILE_FILE переопределяет путь env-файла профиля ===")
    env_file = tmp / ".env"
    if env_file.exists():
        env_file.unlink()
    custom = tmp / "custom.env"
    custom.write_text("FOO=custom\n", encoding="utf-8")
    with env_guard(TG_PROFILE="p1", TG_PROFILE_FILE=str(custom)):
        env_mod.load_env(path=env_file)
        check(os.environ.get("FOO") == "custom",
              "TG_PROFILE_FILE: значения из указанного файла", os.environ.get("FOO"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_profile_"))
    try:
        test_unset_noop(tmp)
        test_meta_applied(tmp)
        test_envfile_applied(tmp)
        test_process_env_wins(tmp)
        test_bootstrap_guard(tmp)
        test_precedence(tmp)
        test_full_precedence(tmp)
        test_profile_file_override(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"Провалено: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("Все проверки зелёные")
    return 0


if __name__ == "__main__":
    sys.exit(main())
