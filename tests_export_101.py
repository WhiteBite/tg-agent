#!/usr/bin/env python3
"""Проверки 101-провайдера export.py против локального http.server (127.0.0.1).

Реально исполняются: POST-механика (Bearer/Content-Type/тело записи), курсор
integrations, идемпотентность, остаток после курсора, поведение на HTTP 500
и сетевой сбой, отсутствие кредов, EXPORT_101_PATH. Живой 101 API не зовётся.
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

sys.path.insert(0, str(Path(__file__).parent))

from db import DB  # noqa: E402
import export as export_mod  # noqa: E402

FAILURES: list[str] = []

ENV_KEYS = ("EXPORT_101_URL", "EXPORT_101_TOKEN", "EXPORT_101_PATH", "EXPORT_101_TIMEOUT")


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def set_env(**kv: str) -> None:
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in kv.items():
        os.environ[k] = v


def add_event(db_path: Path, entity_id: int, ts: int, quote: str, msg_id: int) -> None:
    with DB(db_path) as db:
        db.conn.execute(
            "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
            "VALUES(?, 2, ?, 'update', -100999, ?, ?)", (entity_id, ts, msg_id, quote))
        db.conn.commit()


def cursor_of(db_path: Path, entity_id: int) -> int | None:
    with DB(db_path) as db:
        row = db.conn.execute(
            "SELECT synced_ts FROM integrations WHERE entity_id=? AND system='101'",
            (entity_id,)).fetchone()
        return row["synced_ts"] if row else None


class CaptureHandler(http.server.BaseHTTPRequestHandler):
    requests: list[dict] = []
    status: int = 200

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        CaptureHandler.requests.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "ctype": self.headers.get("Content-Type"),
            "body": json.loads(self.rfile.read(n).decode("utf-8")),
        })
        self.send_response(CaptureHandler.status)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def seed(db_path: Path, now: int) -> None:
    with DB(db_path) as db:
        for eid, title in ((1, "Залить фундамент"), (2, "Заказать арматуру"), (3, "Без событий")):
            db.conn.execute(
                "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, "
                "created_ts, updated_ts) "
                "VALUES(?, 'construction', -100999, 7, 'task', ?, ?, ?)",
                (eid, title, now, now))
        for eid, mid in ((1, 11), (2, 12)):
            db.conn.execute(
                "INSERT INTO entity_events(entity_id, run_id, ts, op, chat_id, msg_id, quote) "
                "VALUES(?, 1, ?, 'new', -100999, ?, 'фраза')", (eid, now - 3600, mid))
        db.conn.commit()


def test_missing_creds(db_path: Path) -> None:
    print("=== 101: без кредов — словарь-ошибка, без исключения ===")
    set_env()
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is False and isinstance(res.get("error"), str) and bool(res["error"]),
          "101 без кредов: структурированная ошибка без исключения", str(res))
    check("EXPORT_101_URL" in res.get("error", "") and "EXPORT_101_TOKEN" in res.get("error", ""),
          "101 без кредов: ошибка называет обе переменные", str(res))
    with DB(db_path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM integrations WHERE system='101'").fetchone()[0]
    check(n == 0, "101 без кредов: курсор integrations не тронут", str(n))

    set_env(EXPORT_101_URL="http://example.invalid")
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is False and "EXPORT_101_TOKEN" in res.get("error", "")
          and "EXPORT_101_URL" not in res.get("error", ""),
          "101: ошибка называет только реально отсутствующую переменную", str(res))


def test_push(db_path: Path, base: str) -> None:
    print("=== 101: пуш сущностей, заголовки, курсор, идемпотентность ===")
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token")
    res = export_mod.run(str(db_path), system="101")
    check(res.get("ok") is True and res.get("exported") == 2,
          "101: первый прогон — 2 сущности с событиями, без событий — не выгружена", str(res))
    check(len(CaptureHandler.requests) == 2,
          "101: ровно два POST-запроса (по одному на сущность)", str(len(CaptureHandler.requests)))
    check(all(r["path"] == "/entities" for r in CaptureHandler.requests),
          "101: путь по умолчанию /entities",
          str([r["path"] for r in CaptureHandler.requests]))
    check(all(r["auth"] == "Bearer jwt-test-token" for r in CaptureHandler.requests),
          "101: заголовок Authorization: Bearer <JWT>",
          str([r["auth"] for r in CaptureHandler.requests]))
    check(all(r["ctype"] == "application/json" for r in CaptureHandler.requests),
          "101: заголовок Content-Type: application/json",
          str([r["ctype"] for r in CaptureHandler.requests]))
    check([r["body"]["entity"]["id"] for r in CaptureHandler.requests] == [1, 2],
          "101: сущности по порядку id",
          str([r["body"]["entity"]["id"] for r in CaptureHandler.requests]))
    check(all(set(r["body"]) == {"entity", "events", "exported_ts"}
              for r in CaptureHandler.requests),
          "101: тело записи как у file-провайдера (entity/events/exported_ts)")
    check(all(len(r["body"]["events"]) == 1 and r["body"]["events"][0]["quote"] == "фраза"
              for r in CaptureHandler.requests),
          "101: события доехали с цитатами")
    check(all(isinstance(r["body"]["exported_ts"], int) for r in CaptureHandler.requests),
          "101: exported_ts — целое")

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
    last = CaptureHandler.requests[-1]["body"]
    check(res3.get("ok") is True and res3.get("exported") == 1
          and len(CaptureHandler.requests) == 3
          and last["entity"]["id"] == 1
          and [e["quote"] for e in last["events"]] == ["новое событие"],
          "101: остаток после курсора — одна сущность и только новое событие", str(res3))


def test_http_error(db_path: Path, base: str) -> None:
    print("=== 101: HTTP 500 — курсор на месте, сущность переотправляется ===")
    add_event(db_path, 2, int(time.time()) - 30, "сдвиг срока", 99)
    before = cursor_of(db_path, 2)
    CaptureHandler.status = 500
    try:
        res4 = export_mod.run(str(db_path), system="101")
    finally:
        CaptureHandler.status = 200
    check(res4.get("ok") is False and "500" in str(res4.get("error", "")),
          "101: HTTP 500 — словарь-ошибка без исключения", str(res4))
    check(cursor_of(db_path, 2) == before,
          "101: после HTTP 500 курсор не двигался", f"{before}")
    n_before = len(CaptureHandler.requests)
    res5 = export_mod.run(str(db_path), system="101")
    last = CaptureHandler.requests[-1]["body"]
    check(res5.get("ok") is True and res5.get("exported") == 1
          and len(CaptureHandler.requests) == n_before + 1
          and last["entity"]["id"] == 2
          and [e["quote"] for e in last["events"]] == ["сдвиг срока"],
          "101: после 500 сущность переотправлена целиком", str(res5))


def test_path_and_network(db_path: Path, base: str) -> None:
    print("=== 101: EXPORT_101_PATH и сетевой сбой ===")
    add_event(db_path, 1, int(time.time()) - 10, "для пути", 100)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token",
            EXPORT_101_PATH="/api/v2/things")
    res6 = export_mod.run(str(db_path), system="101")
    check(res6.get("ok") is True and res6.get("exported") == 1
          and CaptureHandler.requests[-1]["path"] == "/api/v2/things",
          "101: EXPORT_101_PATH переопределяет путь", str(res6))

    dead = http.server.HTTPServer(("127.0.0.1", 0), CaptureHandler)
    dead_port = dead.server_address[1]
    dead.server_close()
    add_event(db_path, 2, int(time.time()) - 5, "для сети", 101)
    before = cursor_of(db_path, 2)
    set_env(EXPORT_101_URL=f"http://127.0.0.1:{dead_port}",
            EXPORT_101_TOKEN="jwt-test-token", EXPORT_101_TIMEOUT="2")
    res7 = export_mod.run(str(db_path), system="101")
    check(res7.get("ok") is False and isinstance(res7.get("error"), str)
          and bool(res7["error"]),
          "101: сетевой сбой — словарь-ошибка без исключения", str(res7))
    check(cursor_of(db_path, 2) == before,
          "101: после сетевого сбоя курсор не двигался", f"{before}")


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
        test_http_error(db_path, base)
        test_path_and_network(db_path, base)
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
