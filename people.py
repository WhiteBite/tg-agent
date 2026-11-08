#!/usr/bin/env python3
"""Справочник людей (ручная связь): ключ → имя, роль, Telegram ID, GUID в 101.

    python3 people.py list
    python3 people.py set --key ivan --name "Иван" --role прораб --ids 123,456
    python3 people.py remove --key ivan

Хранится в meta как ws:person:<key> — тот же формат, что читает рабочее
пространство. Связь справочная: она не меняет исходного автора сообщений.
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
PREFIX = "ws:person:"


def load_people(db: DB) -> list[dict]:
    out = []
    for r in db.conn.execute("SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
                             (PREFIX + "%",)):
        try:
            data = json.loads(r["value"]) if r["value"] else {}
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            out.append({"key": r["key"][len(PREFIX):], **data})
    return out


def resolve_owner(db: DB, name_or_id) -> dict | None:
    if name_or_id is None:
        return None
    needle = str(name_or_id).strip()
    if not needle:
        return None
    folded = needle.casefold()
    people = load_people(db)
    for person in people:
        name = person.get("display_name")
        if isinstance(name, str) and name.casefold() == folded:
            return person
    for person in people:
        ids = person.get("telegram_sender_ids")
        if isinstance(ids, list) and any(str(i) == needle for i in ids):
            return person
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="справочник людей в рабочем пространстве")
    ap.add_argument("--db", default=DB_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="показать всех")
    s = sub.add_parser("set", help="создать или обновить")
    s.add_argument("--key", required=True, help="ключ человека (латиницей)")
    s.add_argument("--name", default="", help="отображаемое имя")
    s.add_argument("--role", default="", help="роль в проекте")
    s.add_argument("--ids", default="", help="Telegram sender ID через запятую")
    s.add_argument("--crm", default="", help="GUID участника в 101")
    r = sub.add_parser("remove", help="удалить связь")
    r.add_argument("--key", required=True)
    args = ap.parse_args()
    with DB(args.db) as db:
        if args.cmd == "list":
            print(json.dumps(load_people(db), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "remove":
            with db.conn:
                db.conn.execute("DELETE FROM meta WHERE key=?", (PREFIX + args.key,))
            print(json.dumps({"ok": True, "key": args.key}, ensure_ascii=False))
            return 0
        ids: list[int] = []
        for part in args.ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError:
                print(f"ошибка: Telegram ID «{part}» не целое число", file=sys.stderr)
                return 2
        data = {"display_name": args.name, "role": args.role,
                "crm_member_guid": args.crm, "telegram_sender_ids": ids}
        db.set_meta(PREFIX + args.key, json.dumps(data, ensure_ascii=False))
        print(json.dumps({"ok": True, "key": args.key, "ids": ids}, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    sys.exit(main())
