#!/usr/bin/env python3
"""Проверки 101-провайдера export.py против локального http.server (127.0.0.1).

Реально исполняются: POST-механика (Bearer/Content-Type/тело записи), курсор
integrations по id события (двойное событие в одну секунду не теряется),
идемпотентность, остаток после курсора, ретраи 5xx (три неудачи потом успех,
исчерпание попыток), 4xx без ретрая и без блокировки остальных сущностей,
Idempotency-Key, external_id из ответа, file-провайдер на id-курсоре,
поведение на HTTP 500 и сетевой сбой, отсутствие кредов, EXPORT_101_PATH.
Живой 101 API не зовётся.
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

ENV_KEYS = ("EXPORT_101_URL", "EXPORT_101_TOKEN", "EXPORT_101_PATH",
            "EXPORT_101_TIMEOUT", "EXPORT_101_RETRIES", "EXPORT_101_BACKOFF")


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
    requests: list[dict] = []
    status: int = 200
    fail_first: int | None = None
    entity_status: dict[int, int] = {}
    response_body: bytes = b""

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        CaptureHandler.requests.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "ctype": self.headers.get("Content-Type"),
            "idem": self.headers.get("Idempotency-Key"),
            "body": body,
        })
        code = CaptureHandler.entity_status.get(body["entity"]["id"])
        if code is None:
            after_fail = (CaptureHandler.fail_first is not None
                          and len(CaptureHandler.requests) > CaptureHandler.fail_first)
            code = 200 if after_fail else CaptureHandler.status
        payload = CaptureHandler.response_body if code == 200 else b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def reset_capture() -> None:
    CaptureHandler.requests = []
    CaptureHandler.status = 200
    CaptureHandler.fail_first = None
    CaptureHandler.entity_status = {}
    CaptureHandler.response_body = b""


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


def seed_entities(db_path: Path, now: int, ids: tuple[int, ...]) -> None:
    with DB(db_path) as db:
        for eid in ids:
            db.conn.execute(
                "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, "
                "created_ts, updated_ts) "
                "VALUES(?, 'construction', -100999, 7, 'task', ?, ?, ?)",
                (eid, f"Сущность {eid}", now, now))
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
    n0 = len(CaptureHandler.requests)
    CaptureHandler.status = 500
    try:
        res4 = export_mod.run(str(db_path), system="101")
    finally:
        CaptureHandler.status = 200
    keys500 = [r["idem"] for r in CaptureHandler.requests[n0:]]
    check(res4.get("ok") is False and "500" in str(res4.get("error", "")),
          "101: HTTP 500 — словарь-ошибка без исключения", str(res4))
    check(cursor_of(db_path, 2) == before,
          "101: после HTTP 500 курсор не двигался", f"{before}")
    check(len(keys500) >= 1 and len(set(keys500)) == 1,
          "101: Idempotency-Key одинаков на всех ретраях", str(keys500))
    n_before = len(CaptureHandler.requests)
    res5 = export_mod.run(str(db_path), system="101")
    last = CaptureHandler.requests[-1]["body"]
    check(res5.get("ok") is True and res5.get("exported") == 1
          and len(CaptureHandler.requests) == n_before + 1
          and last["entity"]["id"] == 2
          and [e["quote"] for e in last["events"]] == ["сдвиг срока"],
          "101: после 500 сущность переотправлена целиком", str(res5))
    check(CaptureHandler.requests[-1]["idem"] == keys500[0],
          "101: ключ переотправки совпадает с ключом неудавшихся попыток",
          str(CaptureHandler.requests[-1]["idem"]))


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
          and [e["quote"] for e in last["body"]["events"]] == ["второе в ту же секунду"],
          "101 id-курсор: второе событие с тем же ts не потеряно, первое не повторено",
          str(res2))
    check(last["idem"] == f"1-{id_b}",
          "101: Idempotency-Key = <entity_id>-<max_event_id>", str(last["idem"]))
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
    keys = [r["idem"] for r in CaptureHandler.requests]
    check(res.get("ok") is True and res.get("exported") == 1
          and len(CaptureHandler.requests) == 4,
          "101 ретраи: 3 неудачи + успех на 4-й попытке, сущность выгружена", str(res))
    check(len(set(keys)) == 1 and keys[0] == f"1-{eid}",
          "101 ретраи: Idempotency-Key одинаков на всех попытках", str(keys))
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
    CaptureHandler.entity_status = {1: 500}
    try:
        res = export_mod.run(str(db_path), system="101")
    finally:
        CaptureHandler.entity_status = {}
    per_entity = [r["body"]["entity"]["id"] for r in CaptureHandler.requests]
    check(res.get("ok") is False and res.get("exported") == 1
          and res.get("failed") == [1] and "500" in str(res.get("error", "")),
          "101 5xx: ok=False, failed=[1], вторая сущность выгружена", str(res))
    check(per_entity == [1, 1, 1, 1, 2],
          "101 5xx: 4 попытки на первую сущность, вторая — сразу после", str(per_entity))
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
    CaptureHandler.entity_status = {1: 400}
    try:
        res = export_mod.run(str(db_path), system="101")
    finally:
        CaptureHandler.entity_status = {}
    per_entity = [r["body"]["entity"]["id"] for r in CaptureHandler.requests]
    check(res.get("ok") is False and res.get("exported") == 1
          and res.get("failed") == [1] and "400" in str(res.get("error", "")),
          "101 4xx: ok=False, failed=[1], ошибка называет код", str(res))
    check(per_entity == [1, 2],
          "101 4xx: ровно одна попытка на 4xx, вторая сущность не заблокирована",
          str(per_entity))
    add_event(db_path, 1, now - 50, "восстановление", 503)
    res2 = export_mod.run(str(db_path), system="101")
    last = CaptureHandler.requests[-1]["body"]
    check(res2.get("ok") is True and res2.get("exported") == 1
          and last["entity"]["id"] == 1
          and [e["quote"] for e in last["events"]] == ["плохой запрос", "восстановление"],
          "101 4xx: упавшая сущность выгружается целиком после починки", str(res2))


def test_idempotency_external_id(db_path: Path, base: str) -> None:
    print("=== 101: Idempotency-Key и external_id из ответа ===")
    reset_capture()
    now = int(time.time())
    seed_entities(db_path, now, (1,))
    eid = add_event(db_path, 1, now - 100, "с external_id", 601)
    set_env(EXPORT_101_URL=base, EXPORT_101_TOKEN="jwt-test-token")
    CaptureHandler.response_body = b'{"external_id": "ext-777"}'
    try:
        res = export_mod.run(str(db_path), system="101")
        check(res.get("ok") is True and res.get("exported") == 1
              and CaptureHandler.requests[0]["idem"] == f"1-{eid}",
              "101: Idempotency-Key <entity_id>-<max_event_id> в заголовке",
              str(CaptureHandler.requests[0]["idem"]))
        check(external_id_of(db_path, 1) == "ext-777",
              "101: external_id из ответа записан в integrations",
              str(external_id_of(db_path, 1)))
        eid2 = add_event(db_path, 1, now - 50, "с id", 602)
        CaptureHandler.response_body = b'{"id": 888}'
        res2 = export_mod.run(str(db_path), system="101")
        check(res2.get("ok") is True
              and CaptureHandler.requests[-1]["idem"] == f"1-{eid2}",
              "101: ключ меняется с новым max_event_id",
              str(CaptureHandler.requests[-1]["idem"]))
        check(external_id_of(db_path, 1) == "888",
              "101: id из ответа тоже пишется в external_id",
              str(external_id_of(db_path, 1)))
        add_event(db_path, 1, now - 40, "без id", 603)
        CaptureHandler.response_body = b"{}"
        res3 = export_mod.run(str(db_path), system="101")
        check(res3.get("ok") is True and external_id_of(db_path, 1) == "888",
              "101: ответ без id не затирает external_id",
              str(external_id_of(db_path, 1)))
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
        test_same_second(tmp / "same_second.sqlite", base)
        test_retry_success(tmp / "retry.sqlite", base)
        test_retry_exhausted(tmp / "retry_dead.sqlite", base)
        test_4xx_no_retry(tmp / "4xx.sqlite", base)
        test_idempotency_external_id(tmp / "idem.sqlite", base)
        test_file_id_cursor(tmp / "file_id.sqlite", tmp / "file_id.jsonl")
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
