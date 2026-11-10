#!/usr/bin/env python3
"""Выгрузка извлечённых сущностей во внешние системы (file / 101 / notion).

    python3 export.py                                  # JSONL рядом с базой
    python3 export.py --system file --out /tmp/export.jsonl
    python3 export.py --system 101                     # REST-провайдер, нужны EXPORT_101_*
    python3 export.py --system notion                  # REST-провайдер, нужны NOTION_TOKEN/NOTION_DATABASE_ID

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
пишется в integrations.external_id, курсор двигается только после 2xx.
notion — REST API Notion (stdlib urllib): NOTION_TOKEN + NOTION_DATABASE_ID,
страницы задач в базе (POST /v1/pages, при наличии external_id — PATCH
/v1/pages/{id}); kinds из NOTION_KINDS (task,promise,approval), переименование
полей — NOTION_MAP (JSON {"title":"Name","body":"Notes","due_ts":"Due",
"status":"Status","owner":"Owner"}), NOTION_API_VERSION (2026-03-11),
NOTION_DRY=1 — показать без сети; 429/529 ретраятся с Retry-After, 5xx — только
GET, 4xx не ретраится; без кредов — словарь-ошибка, исключений не бросает.
TG_EXTRA_PROVIDERS=<путь к .py-модулю со словарём PROVIDERS> — сторонний реестр
мержится поверх встроенных; битый модуль бросает ImportError/FileNotFoundError.
Обратная синхронизация: python3 export.py --system X --pull [--apply]. Тянет
изменения только по уже связанным объектам (integrations.external_id), мержит
base/local/remote из state_json, пишет снимок в external_evidence и событие
origin='external' с цитатой из снимка; по умолчанию превью, применение — за
TG_ALLOW_PULL=1 и --apply; реестр PULLERS читается из того же TG_EXTRA_PROVIDERS.
"""

from __future__ import annotations

import hashlib
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

from env import load_env

load_env()

from db import DB, clean_text, sanitize_title

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))


class _EntityFailed(Exception):
    """Сбой одной сущности: остальные выгружаются, курсор этой не двигается."""


def _pending_events(db: DB, ent: dict, synced: dict[int, int], now: int,
                    system: str | None = None) -> list[dict]:
    sql = "SELECT * FROM entity_events WHERE entity_id=? AND ts<?"
    args: list = [ent["id"], now]
    if system is not None:
        # эхо-гашение: изменение, пришедшее pull'ом из этой же системы, назад не шлём
        sql += (" AND NOT (origin='external' AND external_evidence_id IN "
                "(SELECT id FROM external_evidence WHERE system=?))")
        args.append(system)
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
        events = _pending_events(db, ent, synced, now, system)
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
               timeout: float, extra_headers: dict | None = None) -> dict:
    data = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None else None)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
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
                    if ent["kind"] in kinds and _pending_events(db, ent, synced, now, "101"))
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


class _ApiNotion:
    """REST-клиент Notion: Bearer-токен и фиксированная Notion-Version."""

    def __init__(self, token: str, version: str, timeout: float):
        self.token = token
        self.version = version
        self.timeout = timeout

    def call(self, method: str, path: str, payload: dict | None) -> dict:
        url = "https://api.notion.com/v1/" + path.lstrip("/")
        return _http_json(url, method, payload, self.token, self.timeout,
                          {"Notion-Version": self.version})


def _notion_properties(ent: dict, field_map: dict) -> dict:
    props: dict = {}
    for src, dst in field_map.items():
        val = ent.get(src)
        if val in (None, ""):
            continue
        if src == "title":
            props[dst] = {"title": [{"text": {"content": str(val)}}]}
        elif src == "due_ts":
            props[dst] = {"date": {"start": time.strftime("%Y-%m-%d", time.gmtime(int(val)))}}
        elif src == "status":
            props[dst] = {"select": {"name": str(val)}}
        else:
            props[dst] = {"rich_text": [{"text": {"content": str(val)}}]}
    return props


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _provider_notion(db: DB, out: str | None) -> dict:
    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not token or not database_id:
        return {"ok": False, "error": "notion: нет кредов — задайте NOTION_TOKEN "
                                      "и NOTION_DATABASE_ID"}
    version = os.environ.get("NOTION_API_VERSION") or "2026-03-11"
    timeout = float(os.environ.get("NOTION_TIMEOUT") or 30)
    kinds = {k.strip() for k in
             (os.environ.get("NOTION_KINDS") or "task,promise,approval").split(",") if k.strip()}
    dry = os.environ.get("NOTION_DRY", "").lower() in ("1", "true", "yes")
    field_map = {"title": "Name", "body": "Notes", "due_ts": "Due",
                 "status": "Status", "owner": "Owner"}
    if os.environ.get("NOTION_MAP"):
        field_map = json.loads(os.environ["NOTION_MAP"])
    retries = int(os.environ.get("NOTION_RETRIES") or 3)
    backoff = [float(x) for x in
               (os.environ.get("NOTION_BACKOFF") or "1,5,15").split(",")
               if x.strip()] or [0.0]
    api = _ApiNotion(token, version, timeout)
    synced = {r["entity_id"]: r["synced_ts"] for r in db.conn.execute(
        "SELECT entity_id, synced_ts FROM integrations WHERE system='notion'")}
    try:
        external = {r["entity_id"]: r["external_id"] for r in db.conn.execute(
            "SELECT entity_id, external_id FROM integrations WHERE system='notion'")}
    except sqlite3.OperationalError:
        external = {}  # старые базы без колонки external_id

    if dry:
        now = int(time.time())
        would = sum(1 for ent in (dict(r) for r in db.conn.execute("SELECT * FROM entities"))
                    if ent["kind"] in kinds and _pending_events(db, ent, synced, now, "notion"))
        return {"ok": True, "dry": True, "would_export": would, "database_id": database_id}

    def _retry(method: str, path: str, payload: dict | None) -> dict:
        err = ""
        for attempt in range(retries + 1):
            try:
                return api.call(method, path, payload)
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}"
                if not (e.code in (429, 529) or (500 <= e.code < 600 and method == "GET")):
                    raise _EntityFailed(err) from None
                if attempt < retries:
                    delay = _retry_after(e)
                    time.sleep(delay if delay is not None
                               else backoff[min(attempt, len(backoff) - 1)])
                    continue
            except OSError as e:
                err = f"сеть/таймаут: {e}"
            if attempt < retries:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
        raise _EntityFailed(f"{err} (попыток: {retries + 1})")

    def emit(ent: dict, events: list[dict], now: int) -> str | None:
        if ent["kind"] not in kinds:
            return None
        props = _notion_properties(ent, field_map)
        ext = external.get(ent["id"])
        if ext:
            _retry("PATCH", f"pages/{ext}", {"properties": props})
            return ext
        result = _retry("POST", "pages",
                        {"parent": {"database_id": database_id}, "properties": props})
        page_id = result.get("id")
        return str(page_id) if page_id else None

    exported, failed, error = _export_entities(db, "notion", emit)
    res: dict = {"ok": not failed, "exported": exported, "database_id": database_id}
    if failed:
        res["failed"] = failed
        res["error"] = f"notion: {error}"
    return res


PROVIDERS = {"file": _provider_file, "101": _provider_101, "notion": _provider_notion}
PULLERS: dict[str, Callable[[DB, dict], dict]] = {}


def _load_extra_module():
    path = os.environ.get("TG_EXTRA_PROVIDERS")
    if not path:
        return None
    spec = importlib.util.spec_from_file_location("tg_extra_providers", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"TG_EXTRA_PROVIDERS: не py-модуль: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _extra_providers() -> dict[str, Callable[[DB, str | None], dict]]:
    mod = _load_extra_module()
    return dict(getattr(mod, "PROVIDERS", {}) or {}) if mod is not None else {}


def _extra_pullers() -> dict[str, Callable[[DB, dict], dict]]:
    mod = _load_extra_module()
    return dict(getattr(mod, "PULLERS", {}) or {}) if mod is not None else {}


_MERGEABLE_FIELDS = frozenset({"title", "body", "owner", "due_ts", "status"})


def _norm_val(value):
    return None if value in (None, "") else str(value)


def _sanitize_field(field: str, value):
    if not isinstance(value, str):
        return value
    if field == "title":
        return sanitize_title(clean_text(value))
    return clean_text(value)


def _sanitize_fields(fields: dict) -> dict:
    return {k: _sanitize_field(k, v) for k, v in fields.items()}


def _effective_revision(remote: dict) -> str:
    revision = str(remote.get("revision") or "")
    if revision:
        return revision
    snapshot = remote.get("snapshot") or ""
    return hashlib.sha1(snapshot.encode("utf-8")).hexdigest()


def _merge_fields(ent, base: dict, fields: dict) -> tuple[dict, list[dict]]:
    """3-way merge base/local/remote -> (принятые поля, конфликты)."""
    accepted: dict = {}
    conflicts: list[dict] = []
    for field, remote_val in fields.items():
        if field not in _MERGEABLE_FIELDS or field not in ent.keys():
            continue
        local_val = ent[field]
        base_val = base.get(field)
        rv, lv, bv = _norm_val(remote_val), _norm_val(local_val), _norm_val(base_val)
        if rv == bv:
            continue
        if lv == bv:
            accepted[field] = remote_val
        elif lv == rv:
            continue
        else:
            conflicts.append({"field": field, "local": local_val, "remote": remote_val})
    return accepted, conflicts


def _merge_conflicts(prior: list[dict], fresh: list[dict], fields: dict) -> list[dict]:
    """Незакрытые конфликты прошлых pull'ов живут, пока поле не сойдётся:
    поле из fields пересчитано (fresh или снято), отсутствующее — переносится."""
    fresh_by_field = {c["field"]: c for c in fresh}
    kept = [c for c in prior
            if c.get("field") not in fresh_by_field and c.get("field") not in fields]
    kept.extend(fresh_by_field.values())
    return kept


def _evidence_quote(snapshot: str, hint: str | None) -> str:
    if hint and hint in snapshot:
        return hint
    return snapshot[:500]


def _store_external_evidence(db: DB, system: str, external_id: str, revision: str,
                             snapshot: str) -> int:
    content_hash = hashlib.sha1(snapshot.encode("utf-8")).hexdigest()
    db.conn.execute(
        "INSERT INTO external_evidence(system, external_id, revision, snapshot, "
        "content_hash, receipt_ts) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(system, external_id, revision) DO UPDATE SET "
        "snapshot=excluded.snapshot, content_hash=excluded.content_hash, "
        "receipt_ts=excluded.receipt_ts",
        (system, external_id, revision, snapshot, content_hash, int(time.time())))
    return db.conn.execute(
        "SELECT id FROM external_evidence WHERE system=? AND external_id=? AND revision=?",
        (system, external_id, revision)).fetchone()["id"]


_EVIDENCE_KEEP = 20


def _prune_external_evidence(db: DB, system: str, external_id: str) -> None:
    db.conn.execute(
        "DELETE FROM external_evidence WHERE system=? AND external_id=? AND id NOT IN "
        "(SELECT id FROM external_evidence WHERE system=? AND external_id=? "
        "ORDER BY id DESC LIMIT ?)",
        (system, external_id, system, external_id, _EVIDENCE_KEEP))


def _apply_pull(db: DB, system: str, entity_id: int, external_id: str,
                remote: dict) -> bool:
    """Применяет удалённое состояние внутри BEGIN IMMEDIATE: доказательство,
    событие origin='external', state_json. Удалённые данные уже получены вне
    транзакции. Повтор той же ревизии событий не плодит."""
    revision = _effective_revision(remote)
    snapshot = clean_text(remote.get("snapshot") or "") or ""
    fields = _sanitize_fields(remote.get("fields") or {})
    now = int(time.time())
    db.conn.execute("BEGIN IMMEDIATE")
    try:
        row = db.conn.execute(
            "SELECT state_json FROM integrations WHERE entity_id=? AND system=?",
            (entity_id, system)).fetchone()
        if row is None:
            db.conn.rollback()
            return False
        state = json.loads(row["state_json"] or "{}")
        if state.get("revision") == revision:
            db.conn.rollback()
            return False
        ent = db.conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        if ent is None:
            db.conn.rollback()
            return False
        base = state.get("base") or {}
        accepted, conflicts = _merge_fields(ent, base, fields)
        merged_conflicts = _merge_conflicts(state.get("conflicts") or [], conflicts, fields)
        if accepted:
            ev_id = _store_external_evidence(db, system, external_id, revision, snapshot)
            _prune_external_evidence(db, system, external_id)
            hint = remote.get("quote")
            quote = _evidence_quote(snapshot, clean_text(hint) if hint else None)
            assigns = ", ".join(f"{f}=?" for f in accepted)
            db.conn.execute(f"UPDATE entities SET {assigns}, updated_ts=? WHERE id=?",
                            [*accepted.values(), now, entity_id])
            for field, remote_val in accepted.items():
                db.conn.execute(
                    "INSERT INTO entity_events(entity_id, run_id, ts, op, field, "
                    "from_val, to_val, chat_id, msg_id, quote, origin, "
                    "external_evidence_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (entity_id, None, now, "update", field,
                     None if ent[field] is None else str(ent[field]),
                     None if remote_val is None else str(remote_val),
                     None, None, quote, "external", ev_id))
            if "title" in accepted or "body" in accepted:
                db.conn.execute("DELETE FROM entity_vec WHERE entity_id=?", (entity_id,))
        new_base = dict(base)
        for field, remote_val in fields.items():
            if field not in _MERGEABLE_FIELDS:
                continue
            rv = _norm_val(remote_val)
            bv = _norm_val(base.get(field))
            lv = _norm_val(ent[field])
            # база — общий предок: на конфликте не двигается, иначе следующий pull примет удалённое за «неизменное»
            if rv == bv or lv == bv or lv == rv:
                new_base[field] = remote_val
        db.conn.execute(
            "UPDATE integrations SET state_json=? WHERE entity_id=? AND system=?",
            (json.dumps({"base": new_base, "revision": revision,
                         "conflicts": merged_conflicts,
                         "ack": {"revision": revision, "ts": now}}, ensure_ascii=False),
             entity_id, system))
        db.conn.commit()
        return bool(accepted)
    except Exception:
        db.conn.rollback()
        raise


def _pull_entities(db: DB, system: str, fetch, apply: bool) -> dict:
    """Общий движок pull: только связанные объекты (integrations.external_id),
    удалённые данные тянутся вне транзакции, применение — в _apply_pull."""
    rows = [dict(r) for r in db.conn.execute(
        "SELECT entity_id, external_id, state_json FROM integrations "
        "WHERE system=? AND external_id IS NOT NULL ORDER BY entity_id", (system,))]
    imported = would = 0
    failed: list[int] = []
    errors: list[str] = []
    cursor = None
    for r in rows:
        try:
            remote = fetch(r["entity_id"], r["external_id"])
        except _EntityFailed as exc:
            failed.append(r["entity_id"])
            errors.append(f"сущность {r['entity_id']}: {exc}")
            continue
        if remote is None:
            continue
        revision = _effective_revision(remote)
        cursor = revision
        state = json.loads(r["state_json"] or "{}")
        if state.get("revision") == revision:
            continue
        if apply:
            try:
                if _apply_pull(db, system, r["entity_id"], r["external_id"], remote):
                    imported += 1
            except Exception as exc:
                failed.append(r["entity_id"])
                errors.append(f"сущность {r['entity_id']}: {exc}")
        else:
            ent = db.conn.execute(
                "SELECT * FROM entities WHERE id=?", (r["entity_id"],)).fetchone()
            if ent is None:
                continue
            accepted, _ = _merge_fields(ent, state.get("base") or {},
                                        _sanitize_fields(remote.get("fields") or {}))
            if accepted:
                would += 1
    return {"imported": imported, "would": would, "failed": failed,
            "error": "; ".join(errors), "cursor": cursor}


def _pull_allowed() -> bool:
    return os.environ.get("TG_ALLOW_PULL", "").lower() in ("1", "true", "yes")


def _run_pull(db_path: str, system: str, apply: bool = False,
              confirm: bool = False) -> dict:
    puller = {**PULLERS, **_extra_pullers()}.get(system)
    if puller is None:
        return {"ok": False, "error": f"нет puller для системы: {system}"}
    if apply and not (confirm and _pull_allowed()):
        return {"ok": False, "preview": True, "imported": 0, "failed": [],
                "error": "применение pull требует TG_ALLOW_PULL=1 и confirm=True"}
    with DB(db_path) as db:
        res = puller(db, {"system": system, "apply": bool(apply)})
        if res.get("ok") and apply and res.get("cursor") is not None:
            # pull-курсор отдельно от push (synced_ts хранит id события)
            db.set_meta(f"pull_cursor:{system}", str(res["cursor"]))
            db.conn.commit()
    return res


def run(db_path: str = DB_PATH, system: str = "file", out: str | None = None,
        direction: str = "push", apply: bool = False, confirm: bool = False) -> dict:
    if direction == "pull":
        return _run_pull(db_path, system, apply=apply, confirm=confirm)
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
    direction = "pull" if "--pull" in args else "push"
    applying = "--apply" in args
    res = run(_arg_value(args, "--db") or DB_PATH,
              system=_arg_value(args, "--system") or "file",
              out=_arg_value(args, "--out"),
              direction=direction, apply=applying, confirm=applying)
    if res.get("ok"):
        if direction == "pull":
            if res.get("preview"):
                print(f"pull (превью): изменилось бы {res.get('would', 0)}")
            else:
                print(f"pull: применено {res.get('imported', 0)}")
        else:
            print(f"экспортировано: {res['exported']} -> {res.get('path') or res.get('url')}")
    else:
        print(f"ошибка: {res['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
