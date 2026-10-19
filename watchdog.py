#!/usr/bin/env python3
"""Watchdog свежести конвейера по метке pipeline:last_ok_ts.

    python watchdog.py --db tg_index.sqlite                # дефолт max-age 90 мин
    python watchdog.py --db tg_index.sqlite --max-age 60

Свежая метка (не старше max-age) -> печатает OK, exit 0.
Метка отсутствует или старая -> печатает STALE, exit 1.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))
KEY = "pipeline:last_ok_ts"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="проверка, что pipeline завершался успешно недавно")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--max-age", type=int, default=90, metavar="MIN",
                    help="максимальный возраст успешного завершения, минут")
    args = ap.parse_args()
    with DB(args.db) as db:
        raw = db.get_meta(KEY)
    try:
        last = int(raw)
    except (TypeError, ValueError):
        print("STALE: pipeline не завершался (метка отсутствует)")
        return 1
    age_min = (int(time.time()) - last) // 60
    if age_min <= args.max_age:
        print("OK")
        return 0
    print(f"STALE: pipeline не завершался {age_min} мин")
    return 1


if __name__ == "__main__":
    sys.exit(main())
