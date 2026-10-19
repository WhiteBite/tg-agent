#!/usr/bin/env python3
"""Свежесть конвейера: watchdog печатает OK/STALE по метке pipeline:last_ok_ts,
pipeline.run ставит метку только при прогоне без упавших шагов."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""
os.environ["EXTRACT_PROVIDER"] = "stub"
os.environ["DIGEST_PROVIDER"] = "stub"
os.environ["MEDIA_PROVIDER"] = "stub"

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from db import DB  # noqa: E402

KEY = "pipeline:last_ok_ts"
FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def seed_stamp(db_path: Path, value: str) -> None:
    with DB(db_path) as db:
        db.set_meta(KEY, value)


def read_stamp(db_path: Path) -> str | None:
    with DB(db_path) as db:
        return db.get_meta(KEY)


def run_watchdog(db_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(HERE / "watchdog.py"),
         "--db", str(db_path), *extra],
        cwd=str(HERE), capture_output=True, text=True,
        encoding="utf-8", timeout=60)


def test_watchdog_fresh(tmp: Path) -> None:
    print("=== watchdog: свежая метка -> OK, exit 0 ===")
    db_path = tmp / "fresh.sqlite"
    seed_stamp(db_path, str(int(time.time())))
    proc = run_watchdog(db_path)
    check(proc.returncode == 0 and proc.stdout.strip() == "OK",
          "watchdog: свежая метка в пределах max-age -> OK/0",
          f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr[:120]!r}")


def test_watchdog_stale(tmp: Path) -> None:
    print("=== watchdog: метка 2ч назад при --max-age 60 -> STALE, exit 1 ===")
    db_path = tmp / "stale.sqlite"
    seed_stamp(db_path, str(int(time.time()) - 7200))
    proc = run_watchdog(db_path, "--max-age", "60")
    check(proc.returncode == 1 and "STALE" in proc.stdout and "120 мин" in proc.stdout,
          "watchdog: устаревшая метка -> STALE/1 с возрастом в минутах",
          f"rc={proc.returncode} out={proc.stdout!r}")


def test_watchdog_absent(tmp: Path) -> None:
    print("=== watchdog: метки нет -> STALE, exit 1 ===")
    db_path = tmp / "absent.sqlite"
    with DB(db_path):
        pass
    proc = run_watchdog(db_path)
    check(proc.returncode == 1 and "STALE" in proc.stdout,
          "watchdog: отсутствие метки -> STALE/1",
          f"rc={proc.returncode} out={proc.stdout!r}")


def test_watchdog_default_max_age(tmp: Path) -> None:
    print("=== watchdog: дефолт max-age 90 (89 мин — OK, 91 мин — STALE) ===")
    db_path = tmp / "default_age.sqlite"
    now = int(time.time())
    seed_stamp(db_path, str(now - 89 * 60))
    proc = run_watchdog(db_path)
    check(proc.returncode == 0 and proc.stdout.strip() == "OK",
          "watchdog: 89 мин без --max-age -> OK/0",
          f"rc={proc.returncode} out={proc.stdout!r}")
    seed_stamp(db_path, str(now - 91 * 60))
    proc = run_watchdog(db_path)
    check(proc.returncode == 1 and "STALE" in proc.stdout,
          "watchdog: 91 мин без --max-age -> STALE/1",
          f"rc={proc.returncode} out={proc.stdout!r}")


def test_pipeline_stamps(tmp: Path) -> None:
    print("=== pipeline.run: успешный прогон ставит метку ===")
    import pipeline as pipeline_mod
    db_path = tmp / "stamp.sqlite"
    before = int(time.time())
    rep = asyncio.run(pipeline_mod.run(
        str(db_path), client=None, skip_sync=True, skip_media=True))
    after = int(time.time())
    check(all(res.get("ok") for res in rep.values()),
          "pipeline: офлайн-прогон без упавших шагов", str(rep))
    stamp = read_stamp(db_path)
    ok = stamp is not None and stamp.isdigit() and before <= int(stamp) <= after
    check(ok, "pipeline: успешный прогон ставит pipeline:last_ok_ts",
          f"stamp={stamp} before={before} after={after}")


def test_pipeline_failed_no_stamp(tmp: Path) -> None:
    print("=== pipeline.run: прогон с упавшим шагом не трогает метку ===")
    import pipeline as pipeline_mod
    db_path = tmp / "nofail.sqlite"
    seed_stamp(db_path, "111")

    def boom(*_a, **_k):
        raise RuntimeError("шаг упал")

    saved = pipeline_mod.extract_mod.run
    pipeline_mod.extract_mod.run = boom
    try:
        rep = asyncio.run(pipeline_mod.run(
            str(db_path), client=None, skip_sync=True, skip_media=True))
    finally:
        pipeline_mod.extract_mod.run = saved
    check(rep["extract"]["ok"] is False,
          "pipeline: упавший шаг отражён в отчёте", str(rep.get("extract")))
    check(read_stamp(db_path) == "111",
          "pipeline: прогон с FAIL-шагом не переставляет метку",
          f"stamp={read_stamp(db_path)}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_watchdog_"))
    try:
        test_watchdog_fresh(tmp)
        test_watchdog_stale(tmp)
        test_watchdog_absent(tmp)
        test_watchdog_default_max_age(tmp)
        test_pipeline_stamps(tmp)
        test_pipeline_failed_no_stamp(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"провалов: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
