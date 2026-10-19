#!/usr/bin/env python3
"""Выгрузка извлечённых сущностей во внешние системы (file / 101 / notion).

    python3 export.py                                  # JSONL рядом с базой
    python3 export.py --system file --out /tmp/export.jsonl
    python3 export.py --system 101                     # REST-провайдер, нужны EXPORT_101_*
    python3 export.py --system notion                  # структурированная ошибка: нет API-доступа

Путь file-провайдера: --out, иначе TG_EXPORT_PATH, иначе export.jsonl рядом с базой.
Курсор каждого провайдера живёт в integrations (entity_id, system): synced_ts —
это «первый невыгруженный ts», выгружаются события с synced_ts <= ts < now;
сущности без новых событий не трогают курсор, без событий не выгружаются никогда.
101 — REST (JWT, app.101-group.ru): EXPORT_101_URL, EXPORT_101_TOKEN, путь
EXPORT_101_PATH (по умолчанию /entities), таймаут EXPORT_101_TIMEOUT; один POST
на сущность, курсор двигается только после 2xx. notion не реализован —
словарь-ошибка, исключений не бросает.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))


def _provider_file(db: DB, out: str | None) -> dict:
    path = Path(out or os.environ.get("TG_EXPORT_PATH")
                or str(Path(db.path).with_name("export.jsonl")))
    synced = {r["entity_id"]: r["synced_ts"] for r in db.conn.execute(
        "SELECT entity_id, synced_ts FROM integrations WHERE system='file'")}
    now = int(time.time())
    exported = 0
    ents = [dict(r) for r in db.conn.execute("SELECT * FROM entities ORDER BY id")]
    with path.open("a", encoding="utf-8") as fh:
        for ent in ents:
            sql = "SELECT * FROM entity_events WHERE entity_id=? AND ts<?"
            args: list = [ent["id"], now]
            if ent["id"] in synced:
                sql += " AND ts>=?"
                args.append(synced[ent["id"]])
            events = [dict(r) for r in db.conn.execute(sql + " ORDER BY ts, id", args)]
            if not events:
                continue
            fh.write(json.dumps({"entity": ent, "events": events,
                                 "exported_ts": now}, ensure_ascii=False) + "\n")
            # flush+commit на сущность: crash оставляет максимум один дубль строки
            fh.flush()
            db.conn.execute(
                "INSERT INTO integrations(entity_id, system, synced_ts) VALUES(?,?,?) "
                "ON CONFLICT(entity_id, system) DO UPDATE SET synced_ts=excluded.synced_ts",
                (ent["id"], "file", max(e["ts"] for e in events) + 1))
            db.conn.commit()
            exported += 1
    return {"ok": True, "exported": exported, "path": str(path)}


def _provider_101(db: DB, out: str | None) -> dict:
    base = os.environ.get("EXPORT_101_URL")
    token = os.environ.get("EXPORT_101_TOKEN")
    creds = {"EXPORT_101_URL": base, "EXPORT_101_TOKEN": token}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        return {"ok": False,
                "error": "101: нет кредов — задайте " + ", ".join(missing)}
    path = os.environ.get("EXPORT_101_PATH") or "/entities"
    url = base.rstrip("/") + "/" + path.lstrip("/")
    timeout = float(os.environ.get("EXPORT_101_TIMEOUT") or 30)
    synced = {r["entity_id"]: r["synced_ts"] for r in db.conn.execute(
        "SELECT entity_id, synced_ts FROM integrations WHERE system='101'")}
    now = int(time.time())
    exported = 0
    ents = [dict(r) for r in db.conn.execute("SELECT * FROM entities ORDER BY id")]
    for ent in ents:
        sql = "SELECT * FROM entity_events WHERE entity_id=? AND ts<?"
        args: list = [ent["id"], now]
        if ent["id"] in synced:
            sql += " AND ts>=?"
            args.append(synced[ent["id"]])
        events = [dict(r) for r in db.conn.execute(sql + " ORDER BY ts, id", args)]
        if not events:
            continue
        body = json.dumps({"entity": ent, "events": events, "exported_ts": now},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                pass
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"101: HTTP {e.code} на сущности {ent['id']}"}
        except OSError as e:
            return {"ok": False, "error": f"101: сеть/таймаут на сущности {ent['id']}: {e}"}
        # курсор двигаем только после 2xx: сбой даст максимум один дубль, потери нет
        db.conn.execute(
            "INSERT INTO integrations(entity_id, system, synced_ts) VALUES(?,?,?) "
            "ON CONFLICT(entity_id, system) DO UPDATE SET synced_ts=excluded.synced_ts",
            (ent["id"], "101", max(e["ts"] for e in events) + 1))
        db.conn.commit()
        exported += 1
    return {"ok": True, "exported": exported, "url": url}


def _stub(db: DB, out: str | None) -> dict:
    return {"ok": False, "error": "провайдер не реализован: нужен API-доступ"}


PROVIDERS = {"file": _provider_file, "101": _provider_101, "notion": _stub}


def run(db_path: str = DB_PATH, system: str = "file", out: str | None = None) -> dict:
    prov = PROVIDERS.get(system)
    if prov is None:
        return {"ok": False, "error": f"неизвестная система: {system}"}
    with DB(db_path) as db:
        return prov(db, out)


def _arg_value(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> None:
    args = sys.argv[1:]
    res = run(_arg_value(args, "--db") or DB_PATH,
              system=_arg_value(args, "--system") or "file",
              out=_arg_value(args, "--out"))
    if res.get("ok"):
        print(f"экспортировано: {res['exported']} -> {res.get('path') or res.get('url')}")
    else:
        print(f"ошибка: {res['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
