#!/usr/bin/env python3
"""Проверки notion-провайдера export.py против локального http.server (127.0.0.1).

Реально исполняются: отсутствие кредов (NOTION_TOKEN/NOTION_DATABASE_ID) —
словарь-ошибка без исключения и без сети; NOTION_DRY без HTTP и без движения
курсора; create POST /v1/pages с parent.database_id и properties из сущности,
запись id страницы в integrations.external_id, курсор только после 2xx;
update PATCH /v1/pages/{id} без POST; фильтр NOTION_KINDS; переименование
полей NOTION_MAP; ретрай 429 с Retry-After и отсутствие ретрая на 400.
Живой api.notion.com не зовётся: export._http_json подменяется на маршрут
к фейк-серверу.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""
os.environ.pop("TG_EXTRA_PROVIDERS", None)

sys.path.insert(0, str(Path(__file__).parent))

import export as export_mod
from db import DB

FAILURES: list[str] = []

ENV_KEYS = ("NOTION_TOKEN", "NOTION_DATABASE_ID", "NOTION_MAP", "NOTION_KINDS",
            "NOTION_API_VERSION", "NOTION_DRY", "NOTION_TIMEOUT", "NOTION_RETRIES",
            "NOTION_BACKOFF")


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def set_env(**kv: str) -> None:
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["NOTION_BACKOFF"] = "0"
    for k, v in kv.items():
        os.environ[k] = v


def add_event(db_path: Path, entity_id: int, ts: int, quote: str, msg_id: int) -> int:
    with DB(db_path) as db:
        cur = db.conn.execute(
            "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
            "VALUES(?, 2, ?, 'update', -100999, ?, ?)", (entity_id, ts, msg_id, quote))
        db.conn.commit()
        return cur.lastrowid


def cursor_of(db_path: Path, entity_id: int, system: str = "notion") -> int | None:
    with DB(db_path) as db:
        row = db.conn.execute(
            "SELECT synced_ts FROM integrations WHERE entity_id=? AND system=?",
            (entity_id, system)).fetchone()
        return row["synced_ts"] if row else None


def external_id_of(db_path: Path, entity_id: int) -> str | None:
    with DB(db_path) as db:
        row = db.conn.execute(
            "SELECT external_id FROM integrations WHERE entity_id=? AND system='notion'",
            (entity_id,)).fetchone()
        return row["external_id"] if row else None


def seed_entities(db_path: Path, now: int, ids: tuple[int, ...],
                  kind: str = "task", body: str = "") -> None:
    with DB(db_path) as db:
        for eid in ids:
            db.conn.execute(
                "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, body, "
                "created_ts, updated_ts) "
                "VALUES(?, 'construction', -100999, 7, ?, ?, ?, ?, ?)",
                (eid, kind, f"Сущность {eid}", body, now, now))
        db.conn.commit()


class NotionHandler(http.server.BaseHTTPRequestHandler):
    """Фейк-сервер Notion: POST /v1/pages и PATCH /v1/pages/{id}.

    Статус можно форсировать глобально (status + fail_first): первые
    fail_first запросов получают status, дальше 200. retry_after уходит
    заголовком Retry-After на неуспешный ответ.
    """

    requests: list[dict] = []
    status: int = 200
    fail_first: int | None = None
    retry_after: str | None = None
    response_body: bytes = b""
    page_seq: int = 0
    count: int = 0

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            out = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            out = {}
        return out if isinstance(out, dict) else {}

    def _capture(self, body: dict) -> None:
        NotionHandler.requests.append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "version": self.headers.get("Notion-Version"),
            "ctype": self.headers.get("Content-Type"),
            "body": body,
        })

    def _send(self, code: int, payload: bytes, retry_after: str | None = None) -> None:
        self.send_response(code)
        if retry_after is not None:
            self.send_header("Retry-After", retry_after)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _code(self) -> int:
        if (NotionHandler.fail_first is not None
                and NotionHandler.count > NotionHandler.fail_first):
            return 200
        return NotionHandler.status

    def _handle(self, body: dict) -> None:
        NotionHandler.count += 1
        code = self._code()
        if code != 200:
            self._send(code, b"", retry_after=NotionHandler.retry_after)
            return
        if self.command == "POST":
            NotionHandler.page_seq += 1
            payload = NotionHandler.response_body or \
                f'{{"id": "page-{NotionHandler.page_seq}"}}'.encode()
            self._send(200, payload)
            return
        self._send(200, b"{}")

    def do_POST(self) -> None:
        body = self._body()
        self._capture(body)
        self._handle(body)

    def do_PATCH(self) -> None:
        body = self._body()
        self._capture(body)
        self._handle(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def reset_capture() -> None:
    NotionHandler.requests = []
    NotionHandler.status = 200
    NotionHandler.fail_first = None
    NotionHandler.retry_after = None
    NotionHandler.response_body = b""
    NotionHandler.page_seq = 0
    NotionHandler.count = 0


_real_http_json = export_mod._http_json
FAKE_BASE = ""


def _fake_http_json(url: str, method: str, payload: dict | None, token: str | None,
                    timeout: float, extra_headers: dict | None = None) -> dict:
    routed = url.replace("https://api.notion.com/v1", FAKE_BASE + "/v1", 1)
    return _real_http_json(routed, method, payload, token, timeout, extra_headers)


def test_missing_creds(db_path: Path) -> None:
    print("=== notion: без кредов — словарь-ошибка, без исключения и сети ===")
    reset_capture()
    set_env()
    res = export_mod.run(str(db_path), system="notion")
    check(res.get("ok") is False and isinstance(res.get("error"), str)
          and "NOTION_TOKEN" in res["error"],
          "notion без кредов: структурированная ошибка называет NOTION_TOKEN", str(res))
    check(len(NotionHandler.requests) == 0,
          "notion без кредов: ни одного HTTP-запроса", str(NotionHandler.requests))
    with DB(db_path) as db:
        n = db.conn.execute(
            "SELECT COUNT(*) FROM integrations WHERE system='notion'").fetchone()[0]
    check(n == 0, "notion без кредов: курсор integrations не тронут", str(n))

    set_env(NOTION_TOKEN="tok")
    res = export_mod.run(str(db_path), system="notion")
    check(res.get("ok") is False and "NOTION_DATABASE_ID" in res.get("error", ""),
          "notion: токен есть, базы нет — ошибка называет NOTION_DATABASE_ID", str(res))


def test_dry(db_path: Path, base: str) -> None:
    print("=== notion: NOTION_DRY — без сети и без движения курсора ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1, 2))
    seed_entities(db_path, now, (3,), kind="question")
    seed_entities(db_path, now, (4,))
    for eid, mid in ((1, 221), (2, 222), (3, 223)):
        add_event(db_path, eid, now - 100, f"событие {eid}", mid)
    set_env(NOTION_TOKEN="tok", NOTION_DATABASE_ID="db-1", NOTION_DRY="1")
    res = export_mod.run(str(db_path), system="notion")
    check(res.get("ok") is True and res.get("dry") is True and res.get("would_export") == 2,
          "notion dry: would_export считает только нужные kind с событиями", str(res))
    check(len(NotionHandler.requests) == 0,
          "notion dry: ни одного HTTP-запроса", str(NotionHandler.requests))
    check(cursor_of(db_path, 1) is None and cursor_of(db_path, 2) is None,
          "notion dry: курсор не двинулся", f"{cursor_of(db_path, 1)} {cursor_of(db_path, 2)}")
    set_env(NOTION_TOKEN="tok", NOTION_DATABASE_ID="db-1")
    res2 = export_mod.run(str(db_path), system="notion")
    check(res2.get("ok") is True and len(NotionHandler.requests) == 2,
          "notion dry: после dry реальный прогон выгружает то же самое", str(res2))


def test_create(db_path: Path, base: str) -> None:
    print("=== notion: create POST /v1/pages, parent, properties, external_id, курсор ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    eid = add_event(db_path, 1, now - 100, "создать", 301)
    set_env(NOTION_TOKEN="tok", NOTION_DATABASE_ID="db-xyz")
    res = export_mod.run(str(db_path), system="notion")
    check(res.get("ok") is True and res.get("exported") == 1,
          "notion create: сущность выгружена", str(res))
    check(len(NotionHandler.requests) == 1,
          "notion create: ровно один запрос", str(len(NotionHandler.requests)))
    req = NotionHandler.requests[0]
    check(req["method"] == "POST" and req["path"] == "/v1/pages",
          "notion create: POST /v1/pages", str((req["method"], req["path"])))
    check(req["auth"] == "Bearer tok",
          "notion create: заголовок Authorization: Bearer <token>", str(req["auth"]))
    check(req["version"] == "2026-03-11",
          "notion create: заголовок Notion-Version по умолчанию", str(req["version"]))
    check(req["ctype"] == "application/json",
          "notion create: заголовок Content-Type: application/json", str(req["ctype"]))
    check(req["body"].get("parent") == {"database_id": "db-xyz"},
          "notion create: parent.database_id из NOTION_DATABASE_ID",
          str(req["body"].get("parent")))
    props = req["body"].get("properties", {})
    check(props.get("Name") == {"title": [{"text": {"content": "Сущность 1"}}]},
          "notion create: properties.Name — title из сущности", str(props))
    check(external_id_of(db_path, 1) == "page-1",
          "notion create: id страницы записан в integrations.external_id",
          str(external_id_of(db_path, 1)))
    check(cursor_of(db_path, 1) == eid,
          "notion create: курсор двинулся после 2xx", str(cursor_of(db_path, 1)))


def test_update(db_path: Path, base: str) -> None:
    print("=== notion: update PATCH /v1/pages/{id}, без POST ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    add_event(db_path, 1, now - 100, "первое", 401)
    set_env(NOTION_TOKEN="tok", NOTION_DATABASE_ID="db-xyz")
    res = export_mod.run(str(db_path), system="notion")
    check(res.get("ok") is True and external_id_of(db_path, 1) == "page-1",
          "notion update: первая выгрузка создала страницу", str(res))
    add_event(db_path, 1, now - 50, "второе", 402)
    res2 = export_mod.run(str(db_path), system="notion")
    last = NotionHandler.requests[-1]
    check(res2.get("ok") is True and res2.get("exported") == 1
          and last["method"] == "PATCH" and last["path"] == "/v1/pages/page-1",
          "notion update: новое событие — PATCH /v1/pages/{id}", str(last))
    check([r["method"] for r in NotionHandler.requests] == ["POST", "PATCH"],
          "notion update: повторный прогон не делает POST",
          str([r["method"] for r in NotionHandler.requests]))
    check("parent" not in last["body"] and "properties" in last["body"],
          "notion update: тело PATCH — только properties", str(last["body"]))


def test_kinds(db_path: Path, base: str) -> None:
    print("=== notion: NOTION_KINDS — чужие kind не уходят ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    seed_entities(db_path, now, (2,), kind="question")
    add_event(db_path, 1, now - 100, "задача", 501)
    add_event(db_path, 2, now - 100, "вопрос", 502)
    set_env(NOTION_TOKEN="tok", NOTION_DATABASE_ID="db-xyz", NOTION_KINDS="task")
    res = export_mod.run(str(db_path), system="notion")
    titles = [r["body"]["properties"]["Name"]["title"][0]["text"]["content"]
              for r in NotionHandler.requests]
    check(res.get("ok") is True and titles == ["Сущность 1"],
          "notion kinds: выгружена только сущность из NOTION_KINDS", str(titles))
    check(cursor_of(db_path, 2) is not None,
          "notion kinds: пропущенный kind не застревает (курсор двинут)",
          str(cursor_of(db_path, 2)))
    res2 = export_mod.run(str(db_path), system="notion")
    check(res2.get("ok") is True and len(NotionHandler.requests) == 1,
          "notion kinds: повторный прогон ничего не доотправляет", str(res2))


def test_map(db_path: Path, base: str) -> None:
    print("=== notion: NOTION_MAP переименовывает поля ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,), body="текст задачи")
    add_event(db_path, 1, now - 100, "карта", 601)
    set_env(NOTION_TOKEN="tok", NOTION_DATABASE_ID="db-xyz",
            NOTION_MAP='{"title": "Задача", "body": "Описание"}')
    res = export_mod.run(str(db_path), system="notion")
    props = NotionHandler.requests[0]["body"]["properties"]
    check(res.get("ok") is True and set(props) == {"Задача", "Описание"},
          "notion map: ключи properties переименованы по NOTION_MAP", str(props))
    check(props.get("Задача") == {"title": [{"text": {"content": "Сущность 1"}}]}
          and props.get("Описание") == {"rich_text": [{"text": {"content": "текст задачи"}}]},
          "notion map: значения полей сохранены", str(props))


def test_retry_429(db_path: Path, base: str) -> None:
    print("=== notion: 429 с Retry-After ретраится, затем успех ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    eid = add_event(db_path, 1, now - 100, "ретрай", 701)
    set_env(NOTION_TOKEN="tok", NOTION_DATABASE_ID="db-xyz", NOTION_RETRIES="3")
    NotionHandler.status = 429
    NotionHandler.fail_first = 1
    NotionHandler.retry_after = "0"
    try:
        res = export_mod.run(str(db_path), system="notion")
    finally:
        NotionHandler.status = 200
        NotionHandler.fail_first = None
        NotionHandler.retry_after = None
    check(res.get("ok") is True and res.get("exported") == 1
          and len(NotionHandler.requests) == 2,
          "notion 429: одна неудача + успех, сущность выгружена", str(res))
    check(all(r["method"] == "POST" and r["path"] == "/v1/pages"
              for r in NotionHandler.requests),
          "notion 429: обе попытки — один и тот же запрос",
          str([(r["method"], r["path"]) for r in NotionHandler.requests]))
    check(cursor_of(db_path, 1) == eid,
          "notion 429: курсор двинулся только после успеха", str(cursor_of(db_path, 1)))


def test_400_no_retry(db_path: Path, base: str) -> None:
    print("=== notion: 400 не ретраится (один запрос) ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    add_event(db_path, 1, now - 100, "плохой", 801)
    set_env(NOTION_TOKEN="tok", NOTION_DATABASE_ID="db-xyz", NOTION_RETRIES="3")
    NotionHandler.status = 400
    try:
        res = export_mod.run(str(db_path), system="notion")
    finally:
        NotionHandler.status = 200
    check(res.get("ok") is False and res.get("failed") == [1]
          and "400" in str(res.get("error", "")),
          "notion 400: ok=False, failed=[1], ошибка называет код", str(res))
    check(len(NotionHandler.requests) == 1,
          "notion 400: ровно один запрос, без ретраев", str(len(NotionHandler.requests)))
    check(cursor_of(db_path, 1) is None,
          "notion 400: курсор не двинулся", str(cursor_of(db_path, 1)))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_export_notion_"))

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NotionHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    global FAKE_BASE
    FAKE_BASE = f"http://127.0.0.1:{srv.server_address[1]}"
    export_mod._http_json = _fake_http_json
    try:
        test_missing_creds(tmp / "creds.sqlite")
        test_dry(tmp / "dry.sqlite", FAKE_BASE)
        test_create(tmp / "create.sqlite", FAKE_BASE)
        test_update(tmp / "update.sqlite", FAKE_BASE)
        test_kinds(tmp / "kinds.sqlite", FAKE_BASE)
        test_map(tmp / "map.sqlite", FAKE_BASE)
        test_retry_429(tmp / "retry.sqlite", FAKE_BASE)
        test_400_no_retry(tmp / "bad.sqlite", FAKE_BASE)
    finally:
        export_mod._http_json = _real_http_json
        srv.shutdown()
        srv.server_close()

    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
