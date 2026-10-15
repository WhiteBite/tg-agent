#!/usr/bin/env python3
"""Lockfile одиночного инстанса для демонов (pipeline --loop, live.py).

Захватывается байт файла через msvcrt.locking (Windows) / fcntl.flock (прочее):
ОС сама снимает блокировку при смерти процесса, поэтому протухших lock-файлов
после крашей не остаётся. Держатель обязан хранить ссылку на возвращённый
файловый объект — сборщик мусора закроет его и снимет блокировку.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Optional


def acquire_instance_lock(path: str | Path) -> Optional[IO]:
    """Открытый lock-файл при успешном захвате, None если блокировка занята."""
    fh = open(path, "a+")
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh
