#!/usr/bin/env python3
"""Профили конфигурации: наборы env-значений в meta под ключом config:profile:<имя>.

    python3 profile.py set --name prod TG_ALLOW_SEND=1 EXTRACT_PROVIDER=openai
    python3 profile.py set --name prod --json '{"TG_ALLOW_SEND":"1"}'
    python3 profile.py list
    python3 profile.py show --name prod
    python3 profile.py remove --name prod

Профиль включается переменной TG_PROFILE=<имя>; значения применяет env.load_env()
только к ключам, которых не было в реальном окружении процесса. Ключи TG_DB,
TG_PROFILE, TG_PROFILE_FILE, TG_ENV_FILE профиль задавать не может.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env

load_env()

from db import DB

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))
PREFIX = "config:profile:"


def load_profiles(db: DB) -> list[dict]:
    out = []
    for r in db.conn.execute("SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
                             (PREFIX + "%",)):
        try:
            data = json.loads(r["value"]) if r["value"] else {}
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            out.append({"name": r["key"][len(PREFIX):], "values": data})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="профили конфигурации")
    ap.add_argument("--db", default=DB_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="показать все профили")
    s = sub.add_parser("set", help="создать или обновить профиль")
    s.add_argument("--name", required=True, help="имя профиля")
    s.add_argument("--json", default=None, help="значения одним JSON-объектом")
    s.add_argument("pairs", nargs="*", help="пары KEY=VAL")
    sh = sub.add_parser("show", help="показать один профиль")
    sh.add_argument("--name", required=True)
    r = sub.add_parser("remove", help="удалить профиль")
    r.add_argument("--name", required=True)
    args = ap.parse_args()
    with DB(args.db) as db:
        if args.cmd == "list":
            print(json.dumps(load_profiles(db), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "show":
            raw = db.get_meta(PREFIX + args.name)
            if raw is None:
                print(f"ошибка: профиль «{args.name}» не найден", file=sys.stderr)
                return 2
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                print(f"ошибка: значение профиля «{args.name}» не JSON", file=sys.stderr)
                return 2
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "remove":
            with db.conn:
                db.conn.execute("DELETE FROM meta WHERE key=?", (PREFIX + args.name,))
            print(json.dumps({"ok": True, "name": args.name}, ensure_ascii=False))
            return 0
        values: dict[str, str] = {}
        if args.json is not None:
            try:
                parsed = json.loads(args.json)
            except ValueError:
                print("ошибка: --json не разбирается как JSON", file=sys.stderr)
                return 2
            if not isinstance(parsed, dict):
                print("ошибка: --json должен быть объектом", file=sys.stderr)
                return 2
            values = {str(k): str(v) for k, v in parsed.items()}
        elif args.pairs:
            for pair in args.pairs:
                key, sep, val = pair.partition("=")
                if not sep or not key.strip():
                    print(f"ошибка: «{pair}» не пара KEY=VAL", file=sys.stderr)
                    return 2
                values[key.strip()] = val
        else:
            print("ошибка: нужен --json или пары KEY=VAL", file=sys.stderr)
            return 2
        db.set_meta(PREFIX + args.name, json.dumps(values, ensure_ascii=False))
        print(json.dumps({"ok": True, "name": args.name, "values": values},
                         ensure_ascii=False))
        return 0


if __name__ == "__main__":
    sys.exit(main())
