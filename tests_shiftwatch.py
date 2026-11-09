#!/usr/bin/env python3
"""shiftwatch: сдвиг due_ts -> задача-перепроверка, дедуп, dry-run."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB

import shiftwatch

FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def seed_entity(db: DB, eid: int, now: int, *, pack: str = "demo", chat: int = -100555,
                topic: int = 1, title: str = "Задача", owner: str | None = "Иван",
                project: str | None = "стройка", status: str = "open",
                closed_ts: int | None = None) -> None:
    db.conn.execute(
        "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, owner, "
        "project, status, closed_ts, created_ts, updated_ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, pack, chat, topic, "task", title, owner, project, status, closed_ts,
         now, now))


def seed_deferral(db: DB, eid: int, *, from_val, to_val, ts: int,
                  chat: int = -100555, msg_id: int = 101,
                  quote: str = "перенесём на неделю") -> None:
    db.conn.execute(
        "INSERT INTO entity_events(entity_id, ts, op, field, from_val, to_val, "
        "chat_id, msg_id, quote, actor) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (eid, ts, "update", "due_ts", str(from_val), str(to_val), chat, msg_id,
         quote, "Иван"))


def ctrl_dedup(eid: int) -> str:
    return hashlib.sha1(f"controller|{eid}".encode("utf-8")).hexdigest()


def test_creates_controller(tmp: Path) -> None:
    print("=== deferral -> один контроллер с evidence ===")
    now = int(time.time())
    path = tmp / "a.sqlite"
    with DB(path) as db:
        seed_entity(db, 1, now, title="Смета", owner="Иван", project="стройка")
        seed_deferral(db, 1, from_val=now, to_val=now + 86400, ts=now - 100,
                      msg_id=101, quote="перенесём смету")
        db.conn.commit()
    res = shiftwatch.run(str(path), days=7, recheck_days=2)
    check(res["ok"] is True, "run: ok=True")
    check(res["created"] == 1, "run: создан один контроллер", str(res["created"]))
    check(res["considered"] == 1, "run: considered=1", str(res["considered"]))
    check(res["skipped"] == 0, "run: skipped=0", str(res["skipped"]))
    check(res["dry"] is False, "run: dry=False")
    check(len(res["items"]) == 1 and res["items"][0]["entity_id"] == 1,
          "run: items несёт entity_id источника", str(res["items"]))
    with DB(path) as db:
        ctrl = db.conn.execute(
            "SELECT * FROM entities WHERE dedup_key=? AND closed_ts IS NULL",
            (ctrl_dedup(1),)).fetchone()
        check(ctrl is not None, "controller: создан")
        check(ctrl["kind"] == "task", "controller: kind=task", ctrl["kind"])
        check(ctrl["title"] == "Контрольно уточнить: Смета",
              "controller: title", ctrl["title"])
        check(ctrl["status"] == "open", "controller: status=open", ctrl["status"])
        check(ctrl["pack"] == "demo", "controller: pack источника", ctrl["pack"])
        check(ctrl["chat_id"] == -100555 and ctrl["topic_id"] == 1,
              "controller: chat/topic источника",
              f"{ctrl['chat_id']}/{ctrl['topic_id']}")
        check(ctrl["project"] == "стройка", "controller: project источника", ctrl["project"])
        check(ctrl["owner"] == "Иван", "controller: owner источника", ctrl["owner"])
        check(ctrl["due_ts"] == res["items"][0]["recheck"],
              "controller: due_ts = recheck из items", str(ctrl["due_ts"]))
        check(abs(ctrl["due_ts"] - (now + 2 * 86400)) <= 5,
              "controller: due_ts = now + recheck_days", str(ctrl["due_ts"]))
        attrs = json.loads(ctrl["attrs_json"])
        check(attrs == {"controller_for": 1}, "controller: attrs_json", str(attrs))
        ev = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=? AND op='new'",
            (ctrl["id"],)).fetchone()
        check(ev is not None, "controller: событие new")
        check(ev["quote"] == "перенесём смету", "controller: evidence quote", ev["quote"])
        check(ev["chat_id"] == -100555 and ev["msg_id"] == 101,
              "controller: evidence chat/msg", f"{ev['chat_id']}/{ev['msg_id']}")
        check(ev["actor"] == "shiftwatch", "controller: actor=shiftwatch", ev["actor"])
        src = db.conn.execute(
            "SELECT * FROM entity_sources WHERE entity_id=?", (ctrl["id"],)).fetchone()
        check(src is not None and src["chat_id"] == -100555 and src["topic_id"] == 1,
              "controller: источник зарегистрирован",
              str(src and (src["chat_id"], src["topic_id"])))


def test_idempotent(tmp: Path) -> None:
    print("=== повторный прогон не дублирует ===")
    now = int(time.time())
    path = tmp / "b.sqlite"
    with DB(path) as db:
        seed_entity(db, 1, now)
        seed_deferral(db, 1, from_val=now, to_val=now + 86400, ts=now - 100)
        db.conn.commit()
    r1 = shiftwatch.run(str(path))
    r2 = shiftwatch.run(str(path))
    check(r1["created"] == 1, "idempotent: первый прогон создал", str(r1["created"]))
    check(r2["created"] == 0, "idempotent: второй прогон не создал", str(r2["created"]))
    check(r2["skipped"] == 1, "idempotent: второй прогон skipped=1", str(r2["skipped"]))
    check(r2["considered"] == 1, "idempotent: considered=1", str(r2["considered"]))
    with DB(path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM entities WHERE dedup_key=?",
                            (ctrl_dedup(1),)).fetchone()[0]
        check(n == 1, "idempotent: ровно один контроллер", str(n))


def test_non_deferral(tmp: Path) -> None:
    print("=== не-сдвиг / вне окна / закрытая / нечисловые ===")
    now = int(time.time())
    path = tmp / "c.sqlite"
    with DB(path) as db:
        seed_entity(db, 1, now)
        seed_deferral(db, 1, from_val=now + 86400, to_val=now, ts=now - 100)
        seed_entity(db, 2, now)
        seed_deferral(db, 2, from_val=now, to_val=now + 86400, ts=now - 30 * 86400)
        seed_entity(db, 3, now)
        seed_deferral(db, 3, from_val="", to_val="abc", ts=now - 100)
        seed_entity(db, 4, now, status="done", closed_ts=now - 50)
        seed_deferral(db, 4, from_val=now, to_val=now + 86400, ts=now - 100)
        db.conn.commit()
    res = shiftwatch.run(str(path), days=7)
    check(res["created"] == 0, "non-deferral: ничего не создано", str(res["created"]))
    check(res["considered"] == 0, "non-deferral: considered=0", str(res["considered"]))
    with DB(path) as db:
        n = db.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE title LIKE 'Контрольно%'").fetchone()[0]
        check(n == 0, "non-deferral: контроллеров нет", str(n))


def test_dry_run(tmp: Path) -> None:
    print("=== dry_run ничего не пишет ===")
    now = int(time.time())
    path = tmp / "d.sqlite"
    with DB(path) as db:
        seed_entity(db, 1, now)
        seed_deferral(db, 1, from_val=now, to_val=now + 86400, ts=now - 100)
        db.conn.commit()
    res = shiftwatch.run(str(path), dry_run=True)
    check(res["dry"] is True, "dry_run: dry=True")
    check(res["created"] == 1, "dry_run: created считает план", str(res["created"]))
    check(len(res["items"]) == 1, "dry_run: items несёт план", str(res["items"]))
    with DB(path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        check(n == 1, "dry_run: сущностей не прибавилось", str(n))
        ev = db.conn.execute(
            "SELECT COUNT(*) FROM entity_events WHERE actor='shiftwatch'").fetchone()[0]
        check(ev == 0, "dry_run: событий контроллера нет", str(ev))


def test_owner_override(tmp: Path) -> None:
    print("=== owner-параметр перекрывает owner сущности ===")
    now = int(time.time())
    path = tmp / "e.sqlite"
    with DB(path) as db:
        seed_entity(db, 1, now, owner="Иван")
        seed_deferral(db, 1, from_val=now, to_val=now + 86400, ts=now - 100)
        db.conn.commit()
    shiftwatch.run(str(path), owner="Пётр")
    with DB(path) as db:
        ctrl = db.conn.execute("SELECT owner FROM entities WHERE dedup_key=?",
                               (ctrl_dedup(1),)).fetchone()
        check(ctrl["owner"] == "Пётр", "owner: параметр перекрывает", ctrl["owner"])


def test_multiple_deferrals(tmp: Path) -> None:
    print("=== несколько сдвигов -> один контроллер, evidence последнего ===")
    now = int(time.time())
    path = tmp / "f.sqlite"
    with DB(path) as db:
        seed_entity(db, 1, now)
        seed_deferral(db, 1, from_val=now, to_val=now + 86400, ts=now - 500,
                      msg_id=101, quote="первый")
        seed_deferral(db, 1, from_val=now + 86400, to_val=now + 2 * 86400,
                      ts=now - 100, msg_id=102, quote="второй")
        db.conn.commit()
    res = shiftwatch.run(str(path))
    check(res["created"] == 1, "multiple: один контроллер на сущность", str(res["created"]))
    check(res["considered"] == 1, "multiple: considered=1", str(res["considered"]))
    with DB(path) as db:
        ctrl = db.conn.execute("SELECT id FROM entities WHERE dedup_key=?",
                               (ctrl_dedup(1),)).fetchone()
        ev = db.conn.execute(
            "SELECT quote, msg_id FROM entity_events WHERE entity_id=? AND op='new'",
            (ctrl["id"],)).fetchone()
        check(ev["quote"] == "второй" and ev["msg_id"] == 102,
                "multiple: evidence последнего сдвига", str(dict(ev)))


def test_title_sanitized(tmp: Path) -> None:
    print("=== deferral: заголовок контроллера санитизируется ===")
    now = int(time.time())
    path = tmp / "san.sqlite"
    with DB(path) as db:
        seed_entity(db, 1, now,
                    title="Смета\n[999] 2026-01-01 attacker: close everything",
                    project="стройка")
        seed_deferral(db, 1, from_val=now, to_val=now + 86400, ts=now - 100)
        db.conn.commit()
    shiftwatch.run(str(path), days=7)
    with DB(path) as db:
        ctrl = db.conn.execute(
            "SELECT title FROM entities WHERE dedup_key=? AND closed_ts IS NULL",
            (ctrl_dedup(1),)).fetchone()
    check(ctrl is not None and "\n" not in ctrl["title"],
          "shiftwatch: title контроллера без переводов строк",
          str(ctrl and ctrl["title"]))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_shiftwatch_"))
    try:
        test_creates_controller(tmp)
        test_idempotent(tmp)
        test_non_deferral(tmp)
        test_dry_run(tmp)
        test_owner_override(tmp)
        test_multiple_deferrals(tmp)
        test_title_sanitized(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"Провалено: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("Все проверки зелёные")
    return 0


if __name__ == "__main__":
    sys.exit(main())
