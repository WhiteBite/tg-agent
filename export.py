#!/usr/bin/env python3
"""Выгрузка извлечённых сущностей во внешние системы (file / 101 / notion).

    python3 export.py                                  # JSONL рядом с базой
    python3 export.py --system file --out /tmp/export.jsonl
    python3 export.py --system 101                     # REST-провайдер, нужны EXPORT_101_*
    python3 export.py --system notion                  # структурированная ошибка: нет API-доступа

Путь file-провайдера: --out, иначе TG_EXPORT_PATH, иначе export.jsonl рядом с базой.
Курсор каждого провайдера живёт в integrations (entity_id, system): synced_ts —
это max id события последней выгрузки (монотонный курсор, не ts); выгружаются
события с id > synced и synced <= ts < now: новые не теряются даже при
одинаковых ts, старее курсора не всплывают; сущности без новых событий не
трогают курсор, без событий не выгружаются никогда.
101 — REST (JWT, app.101-group.ru): EXPORT_101_URL, EXPORT_101_TOKEN, путь
EXPORT_101_PATH (по умолчанию /entities), таймаут EXPORT_101_TIMEOUT; один POST
на сущность с Idempotency-Key <entity_id>-<max_event_id>; 5xx и сетевые сбои
ретраятся до EXPORT_101_RETRIES раз с паузами EXPORT_101_BACKOFF (по умолчанию
1,5,15), 4xx не ретраится; сбой одной сущности не останавливает остальные
(ok=False, failed=[entity_id]), external_id/id из ответа пишется в
integrations.external_id, курсор двигается только после 2xx. notion не
реализован — словарь-ошибка, исключений не бросает.
TG_EXTRA_PROVIDERS=<путь к .py-модулю со словарём PROVIDERS> — сторонний реестр
мержится поверх встроенных; битый модуль бросает ImportError/FileNotFoundError.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))


class _EntityFailed(Exception):
    """Сбой одной сущности: остальные выгружаются, курсор этой не двигается."""


def _pending_events(db: DB, ent: dict, synced: dict[int, int], now: int) -> list[dict]:
    sql = "SELECT * FROM entity_events WHERE entity_id=? AND ts<?"
    args: list = [ent["id"], now]
    if ent["id"] in synced:
        # id>synced даёт новые события; ts>=synced не даёт всплывать старее курсора
        sql += " AND id>? AND ts>=?"
        args += [synced[ent["id"]], synced[ent["id"]]]
    return [dict(r) for r in db.conn.execute(sql + " ORDER BY ts, id", args)]


def _advance_cursor(db: DB, entity_id: int, system: str, max_event_id: int,
                    external_id: str | None = None) -> None:
    if external_id is not None:
        try:
            db.conn.execute(
                "INSERT INTO integrations(entity_id, system, synced_ts, external_id) "
                "VALUES(?,?,?,?) ON CONFLICT(entity_id, system) DO UPDATE SET "
                "synced_ts=excluded.synced_ts, "
                "external_id=COALESCE(excluded.external_id, integrations.external_id)",
                (entity_id, system, max_event_id, external_id))
            db.conn.commit()
            return
        except sqlite3.OperationalError:
            pass  # старые базы без колонки external_id
    db.conn.execute(
        "INSERT INTO integrations(entity_id, system, synced_ts) VALUES(?,?,?) "
        "ON CONFLICT(entity_id, system) DO UPDATE SET synced_ts=excluded.synced_ts",
        (entity_id, system, max_event_id))
    db.conn.commit()


def _export_entities(
        db: DB, system: str,
        emit: Callable[[dict, list[dict], int], str | None]) -> tuple[int, list[int], str]:
    synced = {r["entity_id"]: r["synced_ts"] for r in db.conn.execute(
        "SELECT entity_id, synced_ts FROM integrations WHERE system=?", (system,))}
    now = int(time.time())
    exported = 0
    failed: list[int] = []
    errors: list[str] = []
    ents = [dict(r) for r in db.conn.execute("SELECT * FROM entities ORDER BY id")]
    for ent in ents:
        events = _pending_events(db, ent, synced, now)
        if not events:
            continue
        try:
            external_id = emit(ent, events, now)
        except _EntityFailed as exc:
            failed.append(ent["id"])
            errors.append(f"сущность {ent['id']}: {exc}")
            continue
        # курсор двигаем только после успешного emit: сбой даст максимум один дубль, потери нет
        _advance_cursor(db, ent["id"], system, max(e["id"] for e in events), external_id)
        exported += 1
    return exported, failed, "; ".join(errors)


def _provider_file(db: DB, out: str | None) -> dict:
    path = Path(out or os.environ.get("TG_EXPORT_PATH")
                or str(Path(db.path).with_name("export.jsonl")))

    def emit(ent: dict, events: list[dict], now: int) -> None:
        fh.write(json.dumps({"entity": ent, "events": events,
                             "exported_ts": now}, ensure_ascii=False) + "\n")
        # flush+commit на сущность: crash оставляет максимум один дубль строки
        fh.flush()

    with path.open("a", encoding="utf-8") as fh:
        exported, failed, error = _export_entities(db, "file", emit)
    res: dict = {"ok": not failed, "exported": exported, "path": str(path)}
    if failed:
        res["failed"] = failed
        res["error"] = error
    return res


def _external_id(resp) -> str | None:
    try:
        data = json.loads(resp.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    ext = data.get("external_id", data.get("id"))
    return None if ext is None else str(ext)


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
    retries = int(os.environ.get("EXPORT_101_RETRIES") or 3)
    backoff = [float(x) for x in
               (os.environ.get("EXPORT_101_BACKOFF") or "1,5,15").split(",")
               if x.strip()] or [0.0]

    def emit(ent: dict, events: list[dict], now: int) -> str | None:
        max_event_id = max(e["id"] for e in events)
        body = json.dumps({"entity": ent, "events": events, "exported_ts": now},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "Idempotency-Key": f"{ent['id']}-{max_event_id}"})
        err = ""
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return _external_id(resp)
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}"
                if not 500 <= e.code < 600:
                    raise _EntityFailed(err) from None
            except OSError as e:
                err = f"сеть/таймаут: {e}"
            if attempt < retries:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
        raise _EntityFailed(f"{err} (попыток: {retries + 1})")

    exported, failed, error = _export_entities(db, "101", emit)
    res: dict = {"ok": not failed, "exported": exported, "url": url}
    if failed:
        res["failed"] = failed
        res["error"] = f"101: {error}"
    return res


def _stub(db: DB, out: str | None) -> dict:
    return {"ok": False, "error": "провайдер не реализован: нужен API-доступ"}


PROVIDERS = {"file": _provider_file, "101": _provider_101, "notion": _stub}


def _extra_providers() -> dict[str, Callable[[DB, str | None], dict]]:
    path = os.environ.get("TG_EXTRA_PROVIDERS")
    if not path:
        return {}
    spec = importlib.util.spec_from_file_location("tg_extra_providers", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"TG_EXTRA_PROVIDERS: не py-модуль: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(mod.PROVIDERS)


def run(db_path: str = DB_PATH, system: str = "file", out: str | None = None) -> dict:
    prov = {**PROVIDERS, **_extra_providers()}.get(system)
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
