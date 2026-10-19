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
101 — REST API 101 (JWT): EXPORT_101_URL (https://app.101-group.ru), auth —
EXPORT_101_TOKEN либо EXPORT_101_LOGIN + EXPORT_101_PASSWORD (sign-in
/api/auth/sign-in/, refresh пары на 401 через /api/auth/refresh-token/).
Сущности kinds из EXPORT_101_KINDS (по умолчанию task,promise) пишутся задачами
в EXPORT_101_PATH (по умолчанию /api/tasks/v2/): create POST, при наличии
integrations.external_id — PATCH .../{guid}/; EXPORT_101_COMMENTS=1 дописывает
событие комментарием. Переименование полей под контракт 101 — EXPORT_101_MAP
(JSON {"title":"title","body":"description","due_ts":"deadline"}); EXPORT_101_DRY=1 —
только показать, что уедет (курсор не двигается). 5xx и сети ретраятся
EXPORT_101_RETRIES раз с EXPORT_101_BACKOFF (1,5,15), 4xx не ретраится; сбой одной
сущности не роняет остальные (ok=False, failed=[id]); external_id из ответа (guid)
пишется в integrations.external_id, курсор двигается только после 2xx. notion не
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


def _http_json(url: str, method: str, payload: dict | None, token: str | None,
               timeout: float) -> dict:
    data = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None else None)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return {}
    try:
        out = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return out if isinstance(out, dict) else {}


class _Api101:
    """JWT-клиент 101: sign-in по логину/паролю, refresh пары на 401."""

    def __init__(self, base: str, timeout: float):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token = os.environ.get("EXPORT_101_TOKEN")
        self.refresh = os.environ.get("EXPORT_101_REFRESH")
        self.login = os.environ.get("EXPORT_101_LOGIN")
        self.password = os.environ.get("EXPORT_101_PASSWORD")

    def _url(self, path: str) -> str:
        return self.base + "/" + path.lstrip("/")

    def _sign_in(self) -> None:
        d = _http_json(self._url("/api/auth/sign-in/"), "POST",
                       {"login": self.login, "password": self.password},
                       None, self.timeout)
        self.token = d.get("token") or self.token
        self.refresh = d.get("refresh_token") or self.refresh

    def _refresh(self) -> None:
        if not self.refresh:
            self._sign_in()
            return
        d = _http_json(self._url("/api/auth/refresh-token/"), "POST",
                       {"refreshToken": self.refresh}, None, self.timeout)
        self.token = d.get("token") or self.token
        self.refresh = d.get("refreshToken") or self.refresh

    def call(self, method: str, path: str, payload: dict | None) -> dict:
        if not self.token:
            self._sign_in()
        url = self._url(path)
        try:
            return _http_json(url, method, payload, self.token, self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            self._refresh()
            return _http_json(url, method, payload, self.token, self.timeout)


def _provider_101(db: DB, out: str | None) -> dict:
    base = os.environ.get("EXPORT_101_URL")
    if not base:
        return {"ok": False, "error": "101: нет кредов — задайте EXPORT_101_URL, "
                                      "EXPORT_101_TOKEN или EXPORT_101_LOGIN+EXPORT_101_PASSWORD"}
    has_auth = bool(os.environ.get("EXPORT_101_TOKEN")
                    or (os.environ.get("EXPORT_101_LOGIN")
                        and os.environ.get("EXPORT_101_PASSWORD")))
    if not has_auth:
        return {"ok": False, "error": "101: нет кредов — задайте EXPORT_101_TOKEN "
                                      "или EXPORT_101_LOGIN + EXPORT_101_PASSWORD"}
    timeout = float(os.environ.get("EXPORT_101_TIMEOUT") or 30)
    path = os.environ.get("EXPORT_101_PATH") or "/api/tasks/v2/"
    kinds = {k.strip() for k in
             (os.environ.get("EXPORT_101_KINDS") or "task,promise").split(",") if k.strip()}
    dry = os.environ.get("EXPORT_101_DRY", "").lower() in ("1", "true", "yes")
    comments = os.environ.get("EXPORT_101_COMMENTS", "").lower() in ("1", "true", "yes")
    field_map = {"title": "title", "body": "description", "due_ts": "deadline"}
    if os.environ.get("EXPORT_101_MAP"):
        field_map = json.loads(os.environ["EXPORT_101_MAP"])
    retries = int(os.environ.get("EXPORT_101_RETRIES") or 3)
    backoff = [float(x) for x in
               (os.environ.get("EXPORT_101_BACKOFF") or "1,5,15").split(",")
               if x.strip()] or [0.0]
    api = _Api101(base, timeout)
    synced = {r["entity_id"]: r["synced_ts"] for r in db.conn.execute(
        "SELECT entity_id, synced_ts FROM integrations WHERE system='101'")}
    try:
        external = {r["entity_id"]: r["external_id"] for r in db.conn.execute(
            "SELECT entity_id, external_id FROM integrations WHERE system='101'")}
    except sqlite3.OperationalError:
        external = {}  # старые базы без колонки external_id

    if dry:
        now = int(time.time())
        would = sum(1 for ent in (dict(r) for r in db.conn.execute("SELECT * FROM entities"))
                    if ent["kind"] in kinds and _pending_events(db, ent, synced, now))
        return {"ok": True, "dry": True, "would_export": would, "url": api._url(path)}

    def _retry(method: str, p: str, payload: dict | None) -> dict:
        err = ""
        for attempt in range(retries + 1):
            try:
                return api.call(method, p, payload)
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}"
                if not 500 <= e.code < 600:
                    raise _EntityFailed(err) from None
            except OSError as e:
                err = f"сеть/таймаут: {e}"
            if attempt < retries:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
        raise _EntityFailed(f"{err} (попыток: {retries + 1})")

    def emit(ent: dict, events: list[dict], now: int) -> str | None:
        if ent["kind"] not in kinds:
            return None
        ext = external.get(ent["id"])
        body = {dst: ent[src] for src, dst in field_map.items()
                if ent.get(src) not in (None, "")}
        if ext:
            _retry("PATCH", f"{path}{ext}/", body)
            if comments:
                text = "обновление: " + "; ".join(
                    str(e.get("quote") or e["op"]) for e in events)[:500]
                _retry("POST", f"{path}{ext}/comments/", {"text": text})
            return ext
        result = _retry("POST", path, body)
        guid = result.get("guid") or result.get("id")
        return str(guid) if guid else None

    exported, failed, error = _export_entities(db, "101", emit)
    res: dict = {"ok": not failed, "exported": exported, "url": api._url(path)}
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
