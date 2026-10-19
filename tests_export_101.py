#!/usr/bin/env python3
"""Проверки 101-провайдера export.py против локального http.server (127.0.0.1).

Реально исполняются: JWT-механика (Bearer-токен напрямую, sign-in по
логину/паролю через /api/auth/sign-in/, обновление пары на 401 через
/api/auth/refresh-token/), create POST и PATCH .../{guid}/ с комментарием
по EXPORT_101_COMMENTS, карта полей (только непустые поля, без обёртки
entity/events и без Idempotency-Key), фильтр kinds, dry-режим без сети,
курсор integrations по id события (двойное событие в одну секунду не
теряется), идемпотентность, ретраи 5xx (три неудачи потом успех,
исчерпание попыток), 4xx без ретрая и без блокировки остальных сущностей,
external_id (guid/id) из ответа, file-провайдер на id-курсоре, поведение на
сетевом сбое, отсутствие кредов, EXPORT_101_PATH, сторонний реестр
TG_EXTRA_PROVIDERS (merge поверх встроенных, пустой без env, битый путь —
исключение наружу). Живой 101 API не зовётся.
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

from db import DB  # noqa: E402
import export as export_mod  # noqa: E402

FAILURES: list[str] = []

ENV_KEYS = ("EXPORT_101_URL", "EXPORT_101_TOKEN", "EXPORT_101_REFRESH",
            "EXPORT_101_LOGIN", "EXPORT_101_PASSWORD", "EXPORT_101_PATH",
            "EXPORT_101_TIMEOUT", "EXPORT_101_RETRIES", "EXPORT_101_BACKOFF",
            "EXPORT_101_KINDS", "EXPORT_101_MAP", "EXPORT_101_DRY",
            "EXPORT_101_COMMENTS")


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def set_env(**kv: str) -> None:
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["EXPORT_101_BACKOFF"] = "0"
    for k, v in kv.items():
        os.environ[k] = v


def add_event(db_path: Path, entity_id: int, ts: int, quote: str, msg_id: int) -> int:
    with DB(db_path) as db:
        cur = db.conn.execute(
            "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
            "VALUES(?, 2, ?, 'update', -100999, ?, ?)", (entity_id, ts, msg_id, quote))
        db.conn.commit()
        return cur.lastrowid


def cursor_of(db_path: Path, entity_id: int, system: str = "101") -> int | None:
    with DB(db_path) as db:
        row = db.conn.execute(
            "SELECT synced_ts FROM integrations WHERE entity_id=? AND system=?",
            (entity_id, system)).fetchone()
        return row["synced_ts"] if row else None


def external_id_of(db_path: Path, entity_id: int) -> str | None:
    with DB(db_path) as db:
        row = db.conn.execute(
            "SELECT external_id FROM integrations WHERE entity_id=? AND system='101'",
            (entity_id,)).fetchone()
        return row["external_id"] if row else None


class CaptureHandler(http.server.BaseHTTPRequestHandler):
    """Фейк-сервер 101: auth-пара, задачи (create/PATCH/комментарии).

    Запросы задач (всё вне /api/auth/) идут через _handle_task: статус можно
    форсировать глобально (status + fail_first), по title сущности
    (title_status) или один раз отдать 401 (once_401) для проверки refresh.
    """

    requests: list[dict] = []
    status: int = 200
    fail_first: int | None = None
    title_status: dict[str, int] = {}
    response_body: bytes = b""
    once_401: bool = False
    guid_seq: int = 0
    task_count: int = 0

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            out = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            out = {}
        return out if isinstance(out, dict) else {}

    def _capture(self, body: dict) -> None:
        CaptureHandler.requests.append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "ctype": self.headers.get("Content-Type"),
            "idem": self.headers.get("Idempotency-Key"),
            "body": body,
        })

    def _send(self, code: int, payload: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _task_code(self, body: dict) -> int:
        if CaptureHandler.once_401:
            CaptureHandler.once_401 = False
            return 401
        forced = CaptureHandler.title_status.get(body.get("title"))
        if forced is not None:
            return forced
        after_fail = (CaptureHandler.fail_first is not None
                      and CaptureHandler.task_count > CaptureHandler.fail_first)
        return 200 if after_fail else CaptureHandler.status

    def _handle_task(self, body: dict) -> None:
        CaptureHandler.task_count += 1
        code = self._task_code(body)
        if code != 200:
            self._send(code, b"")
            return
        if self.path.endswith("/comments/"):
            self._send(200, b"{}")
            return
        if self.command == "PATCH":
            self._send(200, b"{}")
            return
        CaptureHandler.guid_seq += 1
        payload = CaptureHandler.response_body or \
            f'{{"guid": "g{CaptureHandler.guid_seq}"}}'.encode()
        self._send(200, payload)

    def do_POST(self) -> None:
        body = self._body()
        self._capture(body)
        if self.path == "/api/auth/sign-in/":
            self._send(200, b'{"token": "signin-token", "refresh_token": "refresh-abc"}')
            return
        if self.path == "/api/auth/refresh-token/":
            self._send(200, b'{"token": "fresh-token", "refreshToken": "refresh-new"}')
            return
        self._handle_task(body)

    def do_PATCH(self) -> None:
        body = self._body()
        self._capture(body)
        self._handle_task(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def reset_capture() -> None:
    CaptureHandler.requests = []
    CaptureHandler.status = 200
    CaptureHandler.fail_first = None
    CaptureHandler.title_status = {}
    CaptureHandler.response_body = b""
    CaptureHandler.once_401 = False
    CaptureHandler.guid_seq = 0
    CaptureHandler.task_count = 0


def seed(db_path: Path, now: int) -> None:
    with DB(db_path) as db:
        db.conn.execute(
            "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, body, due_ts, "
            "created_ts, updated_ts) VALUES(1, 'construction', -100999, 7, 'task', "
            "'Залить фундамент', 'выкопать траншею', ?, ?, ?)",
            (now + 86400, now, now))
        db.conn.execute(
            "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, "
            "created_ts, updated_ts) VALUES(2, 'construction', -100999, 7, 'task', "
            "'Заказать арматуру', ?, ?)", (now, now))
        db.conn.execute(
            "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, "
            "created_ts, updated_ts) VALUES(3, 'construction', -100999, 7, 'task', "
            "'Без событий', ?, ?)", (now, now))
        for eid, mid in ((1, 11), (2, 12)):
            db.conn.execute(
                "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
                "VALUES(?, 1, ?, 'new', -100999, ?, 'фраза')", (eid, now - 3600, mid))
        db.conn.commit()


def seed_entities(db_path: Path, now: int, ids: tuple[int, ...],
                  kind: str = "task") -> None:
    with DB(db_path) as db:
        for eid in ids:
            db.conn.execute(
                "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, "
                "created_ts, updated_ts) "
                "VALUES(?, 'construction', -100999, 7, ?, ?, ?, ?)",
                (eid, kind, f"Сущность {eid}", now, now))
        db.conn.commit()


def test_missing_creds(db_path: Path) -> None:
    print("=== 101: без кредов — словарь-ошибка, без исключения ===")
    set_env()
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is False and isinstance(res.get("error"), str) and bool(res["error"]),
          "101 без кредов: структурированная ошибка без исключения", str(res))
    check("EXPORT_101_URL" in res.get("error", "") and "EXPORT_101_TOKEN" in res.get("error", ""),
          "101 без URL: ошибка называет и URL, и токен", str(res))
    with DB(db_path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM integrations WHERE system='101'").fetchone()[0]
    check(n == 0, "101 без кредов: курсор integrations не тронут", str(n))

    set_env(EXPORT_101_URL="http://example.invalid")
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is False and "EXPORT_101_TOKEN" in res.get("error", "")
          and "EXPORT_101_URL" not in res.get("error", ""),
          "101: URL есть, авторизации нет — ошибка называет только токен", str(res))


def test_push(db_path: Path, base: str) -> None:
    print("=== 101: create/PATCH/комментарии, заголовки, курсор, идемпотентность ===")
    reset_capture()
    with DB(db_path) as db:
        due = db.conn.execute("SELECT due_ts FROM entities WHERE id=1").fetchone()["due_ts"]
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token")
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is True and res.get("exported") == 2
          and res.get("url") == base + "/api/tasks/v2/",
          "101: первый прогон — 2 сущности с событиями, без событий — не выгружена", str(res))
    check(len(CaptureHandler.requests) == 2
          and all(r["method"] == "POST" for r in CaptureHandler.requests),
          "101: ровно два POST на путь задач (по одному на сущность)",
          str([(r["method"], r["path"]) for r in CaptureHandler.requests]))
    check(all(r["path"] == "/api/tasks/v2/" for r in CaptureHandler.requests),
          "101: путь по умолчанию /api/tasks/v2/",
          str([r["path"] for r in CaptureHandler.requests]))
    check(all(r["auth"] == "Bearer jwt-test-token" for r in CaptureHandler.requests),
          "101: заголовок Authorization: Bearer <JWT>",
          str([r["auth"] for r in CaptureHandler.requests]))
    check(all(r["ctype"] == "application/json" for r in CaptureHandler.requests),
          "101: заголовок Content-Type: application/json",
          str([r["ctype"] for r in CaptureHandler.requests]))
    check(all(r["idem"] is None for r in CaptureHandler.requests),
          "101: заголовок Idempotency-Key не отправляется",
          str([r["idem"] for r in CaptureHandler.requests]))
    bodies = [r["body"] for r in CaptureHandler.requests]
    check(bodies == [{"title": "Залить фундамент", "description": "выкопать траншею",
                      "deadline": due},
                     {"title": "Заказать арматуру"}],
          "101: тело — карта полей (title/description/deadline), пустые поля опущены",
          str(bodies))
    check(all("entity" not in b and "events" not in b for b in bodies),
          "101: без обёртки entity/events", str(bodies))
    check(external_id_of(db_path, 1) == "g1" and external_id_of(db_path, 2) == "g2",
          "101: guid из ответа записан в integrations.external_id",
          f"{external_id_of(db_path, 1)} {external_id_of(db_path, 2)}")
    with DB(db_path) as db:
        ints = db.conn.execute(
            "SELECT entity_id, synced_ts FROM integrations WHERE system='101' "
            "ORDER BY entity_id").fetchall()
    check([r["entity_id"] for r in ints] == [1, 2] and all(r["synced_ts"] for r in ints),
          "101: курсор integrations только у выгруженных", str([dict(r) for r in ints]))

    res2 = export_mod.run(str(db_path), system="101")
    check(res2.get("ok") is True and res2.get("exported") == 0
          and len(CaptureHandler.requests) == 2,
          "101: повтор без новых событий — 0 запросов, курсор не двигается", str(res2))

    add_event(db_path, 1, int(time.time()) - 60, "новое событие", 98)
    res3 = export_mod.run(str(db_path), system="101")
    last = CaptureHandler.requests[-1]
    check(res3.get("ok") is True and res3.get("exported") == 1
          and len(CaptureHandler.requests) == 3
          and last["method"] == "PATCH" and last["path"] == "/api/tasks/v2/g1/"
          and last["auth"] == "Bearer jwt-test-token"
          and last["body"] == {"title": "Залить фундамент", "description": "выкопать траншею",
                               "deadline": due},
          "101: новое событие у выгруженной сущности — PATCH .../{guid}/ с той же картой полей",
          str(last))
    check(not any(r["path"].endswith("/comments/") for r in CaptureHandler.requests),
          "101: без EXPORT_101_COMMENTS комментарий не отправляется",
          str([r["path"] for r in CaptureHandler.requests]))

    add_event(db_path, 1, int(time.time()) - 50, "ещё событие", 99)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token",
            EXPORT_101_COMMENTS="1")
    res4 = export_mod.run(str(db_path), system="101")
    tail = CaptureHandler.requests[-2:]
    check(res4.get("ok") is True and res4.get("exported") == 1
          and len(CaptureHandler.requests) == 5
          and tail[0]["method"] == "PATCH" and tail[0]["path"] == "/api/tasks/v2/g1/"
          and tail[1]["method"] == "POST" and tail[1]["path"] == "/api/tasks/v2/g1/comments/"
          and tail[1]["body"] == {"text": "обновление: ещё событие"},
          "101: EXPORT_101_COMMENTS=1 — PATCH + POST .../{guid}/comments/ с текстом события",
          str(tail))


def test_kinds(db_path: Path, base: str) -> None:
    print("=== 101: EXPORT_101_KINDS — чужие kind не уходят в 101 ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    seed_entities(db_path, now, (2,), kind="question")
    add_event(db_path, 1, now - 100, "задача", 211)
    add_event(db_path, 2, now - 100, "вопрос", 212)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token")
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is True and len(CaptureHandler.requests) == 1
          and CaptureHandler.requests[0]["body"]["title"] == "Сущность 1",
          "101 kinds: выгружена только сущность из EXPORT_101_KINDS (task)",
          str([(r["method"], r["body"].get("title")) for r in CaptureHandler.requests]))
    check(cursor_of(db_path, 2) is not None,
          "101 kinds: пропущенный kind не застревает в очереди (курсор двинут)",
          str(cursor_of(db_path, 2)))
    res2 = export_mod.run(str(db_path), system="101")
    check(res2.get("ok") is True and len(CaptureHandler.requests) == 1,
          "101 kinds: повторный прогон ничего не доотправляет", str(res2))


def test_dry(db_path: Path, base: str) -> None:
    print("=== 101: EXPORT_101_DRY — без сети и без движения курсора ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1, 2))
    seed_entities(db_path, now, (3,), kind="question")
    seed_entities(db_path, now, (4,))
    for eid, mid in ((1, 221), (2, 222), (3, 223)):
        add_event(db_path, eid, now - 100, f"событие {eid}", mid)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token", EXPORT_101_DRY="1")
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is True and res.get("dry") is True and res.get("would_export") == 2
          and res.get("url") == base + "/api/tasks/v2/",
          "101 dry: would_export считает только нужные kind с событиями", str(res))
    check(len(CaptureHandler.requests) == 0,
          "101 dry: ни одного HTTP-запроса", str(CaptureHandler.requests))
    check(cursor_of(db_path, 1) is None and cursor_of(db_path, 2) is None,
          "101 dry: курсор не двинулся", f"{cursor_of(db_path, 1)} {cursor_of(db_path, 2)}")
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token")
    res2 = export_mod.run(str(db_path), system="101")
    check(res2.get("ok") is True and len(CaptureHandler.requests) == 2,
          "101 dry: после dry реальный прогон выгружает то же самое", str(res2))


def test_sign_in(db_path: Path, base: str) -> None:
    print("=== 101: LOGIN+PASSWORD — sign-in, затем Bearer ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    add_event(db_path, 1, now - 100, "по логину", 231)
    set_env(EXPORT_101_URL=base, EXPORT_101_LOGIN="u1", EXPORT_101_PASSWORD="p1")
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is True and res.get("exported") == 1
          and len(CaptureHandler.requests) == 2,
          "101 sign-in: прогон успешен, sign-in + один запрос задач", str(res))
    first, second = CaptureHandler.requests
    check(first["method"] == "POST" and first["path"] == "/api/auth/sign-in/"
          and first["body"] == {"login": "u1", "password": "p1"} and first["auth"] is None,
          "101 sign-in: POST /api/auth/sign-in/ с логином/паролем без Bearer", str(first))
    check(second["method"] == "POST" and second["path"] == "/api/tasks/v2/"
          and second["auth"] == "Bearer signin-token",
          "101 sign-in: токен из ответа используется как Bearer", str(second))
    check(external_id_of(db_path, 1) == "g1",
          "101 sign-in: guid записан", str(external_id_of(db_path, 1)))


def test_401_refresh(db_path: Path, base: str) -> None:
    print("=== 101: 401 → refresh-token → повтор с новым токеном ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    eid = add_event(db_path, 1, now - 100, "протухший токен", 241)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="stale-token",
            EXPORT_101_REFRESH="refresh-xyz")
    CaptureHandler.once_401 = True
    try:
        res = export_mod.run(str(db_path), system="101")
    finally:
        CaptureHandler.once_401 = False
    check(res.get("ok") is True and res.get("exported") == 1,
          "101 refresh: после обновления токена сущность выгружена", str(res))
    check([r["path"] for r in CaptureHandler.requests] ==
          ["/api/tasks/v2/", "/api/auth/refresh-token/", "/api/tasks/v2/"],
          "101 refresh: 401 → POST /api/auth/refresh-token/ → повтор запроса",
          str([r["path"] for r in CaptureHandler.requests]))
    check(CaptureHandler.requests[1]["body"] == {"refreshToken": "refresh-xyz"}
          and CaptureHandler.requests[1]["auth"] is None,
          "101 refresh: тело {refreshToken}, без Bearer", str(CaptureHandler.requests[1]))
    check(CaptureHandler.requests[0]["auth"] == "Bearer stale-token"
          and CaptureHandler.requests[2]["auth"] == "Bearer fresh-token",
          "101 refresh: повтор идёт с обновлённым токеном",
          str([r["auth"] for r in CaptureHandler.requests]))
    check(external_id_of(db_path, 1) == "g1" and cursor_of(db_path, 1) == eid,
          "101 refresh: guid и курсор записаны после успеха",
          f"{external_id_of(db_path, 1)} {cursor_of(db_path, 1)}")


def test_path_and_network(db_path: Path, base: str) -> None:
    print("=== 101: EXPORT_101_PATH и сетевой сбой ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    add_event(db_path, 1, now - 100, "для пути", 251)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token",
            EXPORT_101_PATH="/api/v2/things")
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is True and res.get("exported") == 1
          and CaptureHandler.requests[-1]["path"] == "/api/v2/things"
          and CaptureHandler.requests[-1]["method"] == "POST",
          "101: EXPORT_101_PATH переопределяет путь", str(res))

    dead = http.server.HTTPServer(("127.0.0.1", 0), CaptureHandler)
    dead_port = dead.server_address[1]
    dead.server_close()
    add_event(db_path, 1, now - 50, "для сети", 252)
    before = cursor_of(db_path, 1)
    set_env(EXPORT_101_URL=f"http://127.0.0.1:{dead_port}",
            EXPORT_101_TOKEN="jwt-test-token", EXPORT_101_TIMEOUT="2",
            EXPORT_101_RETRIES="2")
    res2 = export_mod.run(str(db_path), system="101")
    check(res2.get("ok") is False and res2.get("failed") == [1]
          and "попыток: 3" in str(res2.get("error", "")),
          "101: сетевой сбой — словарь-ошибка, ретраи исчерпаны", str(res2))
    check(cursor_of(db_path, 1) == before and external_id_of(db_path, 1) == "g1",
          "101: после сетевого сбоя курсор и external_id не тронуты",
          f"{cursor_of(db_path, 1)} {external_id_of(db_path, 1)}")


def test_same_second(db_path: Path, base: str) -> None:
    print("=== 101: двойное событие в одну секунду не теряется id-курсором ===")
    reset_capture()
    t = int(time.time()) - 3600
    seed_entities(db_path, t, (1,))
    id_a = add_event(db_path, 1, t, "первое", 201)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token")
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is True and res.get("exported") == 1
          and len(CaptureHandler.requests) == 1 and cursor_of(db_path, 1) == id_a,
          "101 id-курсор: первое событие выгружено, курсор = его id", str(res))
    id_b = add_event(db_path, 1, t, "второе в ту же секунду", 202)
    res2 = export_mod.run(str(db_path), system="101")
    last = CaptureHandler.requests[-1]
    check(res2.get("ok") is True and res2.get("exported") == 1
          and len(CaptureHandler.requests) == 2
          and last["method"] == "PATCH" and last["path"] == "/api/tasks/v2/g1/",
          "101 id-курсор: второе событие с тем же ts не потеряно (PATCH по guid)",
          str(res2))
    check(cursor_of(db_path, 1) == id_b,
          "101 id-курсор: synced_ts хранит max id события", str(cursor_of(db_path, 1)))
    res3 = export_mod.run(str(db_path), system="101")
    check(res3.get("ok") is True and res3.get("exported") == 0
          and len(CaptureHandler.requests) == 2,
          "101 id-курсор: повтор без новых событий пуст", str(res3))


def test_retry_success(db_path: Path, base: str) -> None:
    print("=== 101: 500 три раза, четвёртая попытка успешна ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    eid = add_event(db_path, 1, now - 100, "ретраевое", 301)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token",
            EXPORT_101_RETRIES="3")
    CaptureHandler.status = 500
    CaptureHandler.fail_first = 3
    try:
        res = export_mod.run(str(db_path), system="101")
    finally:
        CaptureHandler.status = 200
        CaptureHandler.fail_first = None
    check(res.get("ok") is True and res.get("exported") == 1
          and len(CaptureHandler.requests) == 4,
          "101 ретраи: 3 неудачи + успех на 4-й попытке, сущность выгружена", str(res))
    check(all(r["method"] == "POST" and r["path"] == "/api/tasks/v2/"
              and r["body"]["title"] == "Сущность 1"
              for r in CaptureHandler.requests),
          "101 ретраи: все попытки — один и тот же запрос", str(len(CaptureHandler.requests)))
    check(cursor_of(db_path, 1) == eid,
          "101 ретраи: курсор двинулся только после успеха", str(cursor_of(db_path, 1)))


def test_retry_exhausted(db_path: Path, base: str) -> None:
    print("=== 101: неисправимый 5xx — failed, остальные сущности выгружаются ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1, 2))
    add_event(db_path, 1, now - 100, "упадёт", 401)
    add_event(db_path, 2, now - 100, "проедет", 402)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token",
            EXPORT_101_RETRIES="3")
    CaptureHandler.title_status = {"Сущность 1": 500}
    try:
        res = export_mod.run(str(db_path), system="101")
    finally:
        CaptureHandler.title_status = {}
    titles = [r["body"]["title"] for r in CaptureHandler.requests]
    check(res.get("ok") is False and res.get("exported") == 1
          and res.get("failed") == [1] and "500" in str(res.get("error", "")),
          "101 5xx: ok=False, failed=[1], вторая сущность выгружена", str(res))
    check(titles == ["Сущность 1"] * 4 + ["Сущность 2"],
          "101 5xx: 4 попытки на первую сущность, вторая — сразу после", str(titles))
    check(cursor_of(db_path, 1) is None and cursor_of(db_path, 2) is not None,
          "101 5xx: курсор упавшей не двинулся, успешной — двинулся",
          f"{cursor_of(db_path, 1)} {cursor_of(db_path, 2)}")


def test_4xx_no_retry(db_path: Path, base: str) -> None:
    print("=== 101: 4xx не ретраится и не блокирует остальные сущности ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1, 2))
    add_event(db_path, 1, now - 100, "плохой запрос", 501)
    add_event(db_path, 2, now - 100, "хороший запрос", 502)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token",
            EXPORT_101_RETRIES="3")
    CaptureHandler.title_status = {"Сущность 1": 400}
    try:
        res = export_mod.run(str(db_path), system="101")
    finally:
        CaptureHandler.title_status = {}
    titles = [r["body"]["title"] for r in CaptureHandler.requests]
    check(res.get("ok") is False and res.get("exported") == 1
          and res.get("failed") == [1] and "400" in str(res.get("error", "")),
          "101 4xx: ok=False, failed=[1], ошибка называет код", str(res))
    check(titles == ["Сущность 1", "Сущность 2"],
          "101 4xx: ровно одна попытка на 4xx, вторая сущность не заблокирована",
          str(titles))
    add_event(db_path, 1, now - 50, "восстановление", 503)
    res2 = export_mod.run(str(db_path), system="101")
    last = CaptureHandler.requests[-1]
    check(res2.get("ok") is True and res2.get("exported") == 1
          and last["method"] == "POST" and last["path"] == "/api/tasks/v2/"
          and last["body"]["title"] == "Сущность 1",
          "101 4xx: упавшая сущность создаётся заново после починки (external_id не было)",
          str(res2))


def test_external_id(db_path: Path, base: str) -> None:
    print("=== 101: external_id из ответа (guid/id), PATCH по нему ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1, 2, 3))
    add_event(db_path, 1, now - 100, "с guid", 601)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token")
    CaptureHandler.response_body = b'{"guid": "ext-777"}'
    try:
        res = export_mod.run(str(db_path), system="101")
        check(res.get("ok") is True and res.get("exported") == 1
              and external_id_of(db_path, 1) == "ext-777",
              "101: guid из ответа записан в integrations.external_id",
              str(external_id_of(db_path, 1)))
        add_event(db_path, 1, now - 50, "обновление", 602)
        res2 = export_mod.run(str(db_path), system="101")
        check(res2.get("ok") is True and res2.get("exported") == 1
              and CaptureHandler.requests[-1]["method"] == "PATCH"
              and CaptureHandler.requests[-1]["path"] == "/api/tasks/v2/ext-777/",
              "101: обновление идёт PATCH .../{external_id}/", str(res2))
        CaptureHandler.response_body = b'{"id": 888}'
        add_event(db_path, 2, now - 40, "с id", 603)
        res3 = export_mod.run(str(db_path), system="101")
        check(res3.get("ok") is True and res3.get("exported") == 1
              and external_id_of(db_path, 2) == "888",
              "101: id из ответа тоже пишется в external_id",
              str(external_id_of(db_path, 2)))
        CaptureHandler.response_body = b"{}"
        add_event(db_path, 3, now - 30, "без id", 604)
        res4 = export_mod.run(str(db_path), system="101")
        check(res4.get("ok") is True and res4.get("exported") == 1
              and external_id_of(db_path, 3) is None,
              "101: ответ без guid/id не записывает external_id",
              str(external_id_of(db_path, 3)))
        CaptureHandler.response_body = b'{"guid": "g-late"}'
        add_event(db_path, 3, now - 20, "догон", 605)
        res5 = export_mod.run(str(db_path), system="101")
        check(res5.get("ok") is True and res5.get("exported") == 1
              and external_id_of(db_path, 3) == "g-late"
              and CaptureHandler.requests[-1]["method"] == "POST",
              "101: без external_id сущность создаётся заново и пишет guid",
              str(external_id_of(db_path, 3)))
    finally:
        CaptureHandler.response_body = b""


def test_file_id_cursor(db_path: Path, out_path: Path) -> None:
    print("=== file: курсор по id события, идемпотентность, остаток ===")
    t = int(time.time()) - 7200
    seed_entities(db_path, t, (1,))
    id_a = add_event(db_path, 1, t, "файл-1", 701)
    id_b = add_event(db_path, 1, t, "файл-2", 702)
    res = export_mod.run(str(db_path), system="file", out=str(out_path))
    lines = [json.loads(x) for x in out_path.read_text(encoding="utf-8").splitlines()]
    check(res.get("ok") is True and res.get("exported") == 1 and len(lines) == 1
          and [e["quote"] for e in lines[0]["events"]] == ["файл-1", "файл-2"],
          "file id-курсор: оба события одной секунды выгружены вместе", str(res))
    check(cursor_of(db_path, 1, "file") == max(id_a, id_b),
          "file id-курсор: synced_ts = max id события", str(cursor_of(db_path, 1, "file")))
    add_event(db_path, 1, t, "файл-3", 703)
    res2 = export_mod.run(str(db_path), system="file", out=str(out_path))
    lines = [json.loads(x) for x in out_path.read_text(encoding="utf-8").splitlines()]
    check(res2.get("ok") is True and res2.get("exported") == 1 and len(lines) == 2
          and [e["quote"] for e in lines[-1]["events"]] == ["файл-3"],
          "file id-курсор: повтор доносит только остаток", str(res2))
    size = out_path.stat().st_size
    res3 = export_mod.run(str(db_path), system="file", out=str(out_path))
    check(res3.get("ok") is True and res3.get("exported") == 0
          and out_path.stat().st_size == size,
          "file id-курсор: без новых событий файл не растёт", str(res3))


def test_extra_providers(tmp: Path) -> None:
    print("=== TG_EXTRA_PROVIDERS: сторонний реестр поверх встроенных ===")
    db = str(tmp / "extra.sqlite")
    marker = tmp / "fake.marker"
    mod_path = tmp / "extra_providers.py"
    mod_path.write_text(
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "def _fake(db, out):\n"
        "    MARKER.write_text('hit', encoding='utf-8')\n"
        "    return {'ok': True, 'exported': 0, 'provider': 'fake'}\n"
        "PROVIDERS = {'fake': _fake, 'file': _fake}\n",
        encoding="utf-8")
    os.environ["TG_EXTRA_PROVIDERS"] = str(mod_path)
    try:
        res = export_mod.run(db, system="fake")
        check(res.get("ok") is True and res.get("provider") == "fake"
              and marker.exists() and marker.read_text(encoding="utf-8") == "hit",
              "extras: run(system='fake') зовёт сторонний провайдер, маркер записан", str(res))
        marker.unlink()
        res_f = export_mod.run(db, system="file")
        check(res_f.get("provider") == "fake" and marker.exists(),
              "extras: сторонний 'file' перекрывает встроенный", str(res_f))
        res_z = export_mod.run(db, system="zzz")
        check(res_z.get("ok") is False and res_z.get("error") == "неизвестная система: zzz",
              "extras: неизвестная система — та же строка ошибки при активном реестре",
              str(res_z))
        res_n = export_mod.run(db, system="notion")
        check(res_n.get("ok") is False and "не реализован" in res_n.get("error", ""),
              "extras: встроенный notion не перекрыт и остаётся стабом", str(res_n))
    finally:
        os.environ.pop("TG_EXTRA_PROVIDERS", None)
    res_n2 = export_mod.run(db, system="notion")
    check(res_n2.get("ok") is False and "не реализован" in res_n2.get("error", ""),
          "без TG_EXTRA_PROVIDERS: реестр пуст, встроенные резолвятся", str(res_n2))
    os.environ["TG_EXTRA_PROVIDERS"] = str(tmp / "no_such_module.py")
    try:
        res_bad = export_mod.run(db, system="fake")
        check(False, "extras: битый путь — FileNotFoundError наружу", str(res_bad))
    except FileNotFoundError:
        check(True, "extras: битый путь — FileNotFoundError наружу")
    finally:
        os.environ.pop("TG_EXTRA_PROVIDERS", None)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_export101_"))
    db_path = tmp / "e101.sqlite"
    seed(db_path, int(time.time()))

    test_missing_creds(db_path)

    srv = http.server.HTTPServer(("127.0.0.1", 0), CaptureHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        test_push(db_path, base)
        test_kinds(tmp / "kinds.sqlite", base)
        test_dry(tmp / "dry.sqlite", base)
        test_sign_in(tmp / "signin.sqlite", base)
        test_401_refresh(tmp / "refresh.sqlite", base)
        test_path_and_network(tmp / "path_net.sqlite", base)
        test_same_second(tmp / "same_second.sqlite", base)
        test_retry_success(tmp / "retry.sqlite", base)
        test_retry_exhausted(tmp / "retry_dead.sqlite", base)
        test_4xx_no_retry(tmp / "4xx.sqlite", base)
        test_external_id(tmp / "extid.sqlite", base)
        test_file_id_cursor(tmp / "file_id.sqlite", tmp / "file_id.jsonl")
        test_extra_providers(tmp)
    finally:
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
