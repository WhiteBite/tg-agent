#!/usr/bin/env python3
"""Панель «Движок»: статус и управление фоновыми демонами (pipeline --loop, live).

    python3 engine.py status

Статус — по занятости lockfile (<db>.pipeline.lock / <db>.live.lock) и свежести
метки pipeline:last_ok_ts. Пуск/стоп — только при TG_ALLOW_ENGINE=1: старт
поднимает отвязанный процесс `python pipeline.py --loop N` / `python live.py`
и пишет pid в meta `engine:<name>:pid`; стоп шлёт terminate по pid из meta.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env

load_env()

import proclock
from db import DB

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))
ALLOWED = ("1", "true", "yes")
NAMES = ("pipeline", "live")
SCRIPTS = {"pipeline": "pipeline.py", "live": "live.py"}
STALE_MIN = int(os.environ.get("ENGINE_STALE_MIN") or 90)


def gate_on() -> bool:
    return os.environ.get("TG_ALLOW_ENGINE", "").strip().lower() in ALLOWED


def _lock_path(db_path: str | Path, name: str) -> str:
    return f"{db_path}.{name}.lock"


def _lock_held(db_path: str | Path, name: str) -> bool:
    fh = proclock.acquire_instance_lock(_lock_path(db_path, name))
    if fh is None:
        return True
    fh.close()
    return False


def status(db_path: str | Path = DB_PATH) -> dict:
    now = int(time.time())
    with DB(db_path) as db:
        last_ok = int(db.get_meta("pipeline:last_ok_ts") or 0)
        pids = {name: int(db.get_meta(f"engine:{name}:pid") or 0) for name in NAMES}
    engines = {name: {"running": _lock_held(db_path, name), "pid": pids[name] or None}
               for name in NAMES}
    engines["pipeline"].update(last_ok_ts=last_ok or None,
                               age_min=(now - last_ok) // 60 if last_ok else None,
                               stale=not last_ok or (now - last_ok) > STALE_MIN * 60)
    return {"enabled": gate_on(), "stale_min": STALE_MIN, "engines": engines}


def _spawn(name: str, minutes: int):
    args = [sys.executable, str(HERE / SCRIPTS[name])]
    if name == "pipeline":
        args += ["--loop", str(minutes)]
    kwargs: dict = {"cwd": str(HERE), "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def _terminate(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def _pid(db_path: str | Path, name: str) -> int:
    with DB(db_path) as db:
        return int(db.get_meta(f"engine:{name}:pid") or 0)


def _check(name: str) -> None:
    if not gate_on():
        raise PermissionError("Управление движком выключено: нужен TG_ALLOW_ENGINE=1")
    if name not in NAMES:
        raise ValueError("Движок: pipeline или live")


def start(db_path: str | Path = DB_PATH, name: str = "pipeline", minutes: int = 30) -> dict:
    _check(name)
    if _lock_held(db_path, name):
        return {"ok": True, "name": name, "already_running": True}
    proc = _spawn(name, int(minutes))
    with DB(db_path) as db:
        db.set_meta(f"engine:{name}:pid", str(proc.pid))
    return {"ok": True, "name": name, "pid": proc.pid}


def stop(db_path: str | Path = DB_PATH, name: str = "pipeline") -> dict:
    _check(name)
    pid = _pid(db_path, name)
    if not pid:
        return {"ok": True, "name": name, "stopped": False, "reason": "pid неизвестен — демон поднят вручную"}
    try:
        _terminate(pid)
    except OSError as exc:
        return {"ok": False, "name": name, "error": str(exc)}
    with DB(db_path) as db:
        db.set_meta(f"engine:{name}:pid", "")
    return {"ok": True, "name": name, "stopped": True, "pid": pid}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(status(DB_PATH), ensure_ascii=False, indent=2))
        return 0
    if cmd in ("start", "stop"):
        name = sys.argv[2] if len(sys.argv) > 2 else "pipeline"
        try:
            res = start(DB_PATH, name) if cmd == "start" else stop(DB_PATH, name)
        except (PermissionError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    print("engine.py status | start <pipeline|live> | stop <pipeline|live>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
