#!/usr/bin/env python3
"""memory: селектор открытых сущностей, similarity, linker, recall, rebuild/gc."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB
from packs import PACKS

import memory

FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def seed_entity(db: DB, eid: int, *, pack: str = "construction", chat: int = -100,
                topic: int = 5, kind: str = "task", title: str = "Задача",
                body: str | None = None, status: str = "open",
                owner: str | None = None, due_ts: int | None = None,
                project: str | None = "Объекд", created_ts: int | None = None,
                updated_ts: int | None = None, closed_ts: int | None = None) -> None:
    db.conn.execute(
        "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, body, status, "
        "owner, due_ts, project, created_ts, updated_ts, closed_ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, pack, chat, topic, kind, title, body, status, owner, due_ts, project,
         created_ts, updated_ts, closed_ts))
    db.conn.execute(
        "INSERT OR IGNORE INTO entity_sources(entity_id, chat_id, topic_id, "
        "first_seen_ts, last_seen_ts) VALUES(?,?,?,?,?)",
        (eid, chat, topic, created_ts, updated_ts))


def seed_event(db: DB, eid: int, *, op: str = "new", ts: int, chat: int = -100,
               msg_id: int = 10, quote: str = "цитата", field: str | None = None,
               to_val: str | None = None) -> None:
    db.conn.execute(
        "INSERT INTO entity_events(entity_id, ts, op, field, to_val, chat_id, msg_id, "
        "quote, actor) VALUES(?,?,?,?,?,?,?,?,?)",
        (eid, ts, op, field, to_val, chat, msg_id, quote, "Иван"))


def seed_message(db: DB, *, chat: int = -100, msg_id: int = 10, text: str,
                 ts: int) -> None:
    db.conn.execute(
        "INSERT INTO messages(chat_id, msg_id, topic_id, ts, text) VALUES(?,?,?,?,?)",
        (chat, msg_id, 5, ts, text))


def test_similarity() -> None:
    print("=== similarity: char-4-gram Jaccard на RU-морфологии ===")
    sim = memory._similarity("подключим радиаторы", "подключение радиаторов")
    check(sim >= 0.3, "similarity: RU-инфлексия >= 0.3", str(round(sim, 3)))
    check(memory._similarity("подключим радиаторы", "покрасим забор") < 0.3,
          "similarity: несвязанное < 0.3")
    check(memory._similarity("", "что-то") == 0.0, "similarity: пустое = 0")
    check(memory._norm("Ёлка,  Дом!") == "ёлка дом", "norm: NFKC+casefold+чистка",
          memory._norm("Ёлка,  Дом!"))
    check(memory._tokens("надо подключить радиатор") == ["подключить", "радиатор"],
          "tokens: стоп-слова и короткие отброшены",
          str(memory._tokens("надо подключить радиатор")))


def test_selector(tmp: Path) -> None:
    print("=== selector: >50 сущностей, старое решение лексически, кросс-топик, core ===")
    path = tmp / "sel.sqlite"
    now = 1_700_000_000
    with DB(path) as db:
        for i in range(1, 71):
            seed_entity(db, i, kind="task", title=f"задача номер {i}",
                        created_ts=now - 86400, updated_ts=now - 86400)
        seed_entity(db, 100, kind="decision", title="подключение радиаторов",
                    created_ts=now - 200 * 86400, updated_ts=now - 200 * 86400)
        seed_entity(db, 101, topic=9, kind="task",
                    title="подключение радиаторов в корпусе Б",
                    created_ts=now - 86400, updated_ts=now - 86400)
        seed_entity(db, 102, kind="task", title="заблокированная задача",
                    status="blocked", created_ts=now - 86400, updated_ts=now - 86400)
        seed_entity(db, 103, kind="task", title="просроченная задача",
                    due_ts=now - 86400, created_ts=now - 86400, updated_ts=now - 86400)
        seed_entity(db, 104, kind="decision", title="свежее решение",
                    created_ts=now, updated_ts=now)
        db.conn.commit()
    pack = PACKS["construction"]
    w = {"chat_id": -100, "topic_id": 5, "project": "Объекд",
         "alias": "Стройка", "topic_title": "Стройка"}
    rows = [{"text": "надо подключить радиатор в новом корпусе"}]
    with DB(path) as db:
        lines = memory.select_open_entities(db, pack, w, rows, now=now)
        lines2 = memory.select_open_entities(db, pack, w, rows, now=now)
    ids = {int(ln.split(" | ")[0]) for ln in lines}
    check(len(lines) <= 60, "selector: не больше K=60", str(len(lines)))
    check(100 in ids, "selector: старое решение выбрано лексически")
    check(101 in ids, "selector: кросс-топик same-project в кандидатах")
    check(102 in ids, "selector: core blocked")
    check(103 in ids, "selector: core overdue")
    check(104 in ids, "selector: core newest decision")
    check(lines == lines2, "selector: детерминирован при фиксированном now")
    check(all(" | " in ln for ln in lines), "selector: формат строки сохранён")


def test_link_pending(tmp: Path) -> None:
    print("=== linker: pending-кандидат, auto выключен по умолчанию ===")
    path = tmp / "link1.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="подключим радиаторы",
                    created_ts=now, updated_ts=now)
        seed_event(db, 2, op="new", ts=now, msg_id=10, quote="подключим радиаторы")
        seed_message(db, msg_id=10, text="подключим радиаторы", ts=now)
        db.conn.commit()
    res = memory.link(str(path), dry_run=False)
    check(res["ok"] is True, "link: ok=True")
    check(res["new"] == 1, "link: один новый кандидат", str(res["new"]))
    check(res["auto"] == 0, "link: auto выключен по умолчанию", str(res["auto"]))
    with DB(path) as db:
        cand = db.conn.execute(
            "SELECT * FROM memory_candidates WHERE new_entity_id=2 "
            "AND candidate_entity_id=1").fetchone()
        check(cand is not None, "link: кандидат создан")
        check(cand["status"] == "pending", "link: статус pending", cand["status"])
        check(cand["signal"] == "similarity", "link: signal=similarity", cand["signal"])
        check(cand["chat_id"] == -100 and cand["msg_id"] == 10,
              "link: evidence из события E", f"{cand['chat_id']}/{cand['msg_id']}")
        p = db.conn.execute("SELECT closed_ts FROM entities WHERE id=1").fetchone()
        check(p["closed_ts"] is None, "link: P не закрыт при auto off")


def test_link_auto(tmp: Path) -> None:
    print("=== linker: auto=True + cue в полном тексте -> supersede ===")
    path = tmp / "link2.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="подключение радиаторов",
                    created_ts=now, updated_ts=now)
        seed_event(db, 2, op="new", ts=now, msg_id=10, quote="короткая цитата")
        seed_message(db, msg_id=10,
                     text="передумали, подключение радиаторов отменяется", ts=now)
        db.conn.commit()
    res = memory.link(str(path), dry_run=False, auto=True)
    check(res["auto"] == 1, "link auto: один авто-supersede", str(res["auto"]))
    with DB(path) as db:
        p = db.conn.execute(
            "SELECT status, closed_ts FROM entities WHERE id=1").fetchone()
        check(p["closed_ts"] is not None, "link auto: P закрыт")
        check(p["status"] == "done", "link auto: P status=done", p["status"])
        ev = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=1 AND op='supersede'").fetchone()
        check(ev is not None, "link auto: событие supersede записано")
        check(ev["field"] == "superseded_by" and ev["to_val"] == "2",
              "link auto: field/to_val", f"{ev['field']}={ev['to_val']}")
        cand = db.conn.execute(
            "SELECT status FROM memory_candidates WHERE new_entity_id=2 "
            "AND candidate_entity_id=1").fetchone()
        check(cand["status"] == "auto", "link auto: кандидат status=auto", cand["status"])


def test_link_rejected(tmp: Path) -> None:
    print("=== linker: rejected не переэскалируется ===")
    path = tmp / "link3.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="подключение радиаторов",
                    created_ts=now, updated_ts=now)
        seed_event(db, 2, op="new", ts=now, msg_id=10, quote="цитата")
        seed_message(db, msg_id=10, text="передумали, отменяем", ts=now)
        db.conn.commit()
    memory.link(str(path), dry_run=False, auto=False)
    with DB(path) as db:
        db.conn.execute("UPDATE memory_candidates SET status='rejected'")
        db.conn.commit()
        db.set_meta(memory.CURSOR_KEY, "0")
    res = memory.link(str(path), dry_run=False, auto=True)
    check(res["auto"] == 0, "rejected: авто не сработал", str(res["auto"]))
    with DB(path) as db:
        cand = db.conn.execute(
            "SELECT status FROM memory_candidates WHERE new_entity_id=2 "
            "AND candidate_entity_id=1").fetchone()
        check(cand["status"] == "rejected", "rejected: статус не изменился", cand["status"])
        p = db.conn.execute("SELECT closed_ts FROM entities WHERE id=1").fetchone()
        check(p["closed_ts"] is None, "rejected: P не закрыт")


def test_link_idempotent(tmp: Path) -> None:
    print("=== linker: идемпотентность ===")
    path = tmp / "link4.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="подключим радиаторы",
                    created_ts=now, updated_ts=now)
        seed_event(db, 2, op="new", ts=now, msg_id=10, quote="подключим радиаторы")
        seed_message(db, msg_id=10, text="подключим радиаторы", ts=now)
        db.conn.commit()
    r1 = memory.link(str(path), dry_run=False)
    r2 = memory.link(str(path), dry_run=False)
    check(r1["new"] == 1, "idempotent: первый прогон создал", str(r1["new"]))
    check(r2["new"] == 0, "idempotent: второй прогон не создал", str(r2["new"]))
    with DB(path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        check(n == 1, "idempotent: ровно один кандидат", str(n))


def test_link_dry_run(tmp: Path) -> None:
    print("=== linker: dry_run ничего не пишет ===")
    path = tmp / "link5.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="подключим радиаторы",
                    created_ts=now, updated_ts=now)
        seed_event(db, 2, op="new", ts=now, msg_id=10, quote="подключим радиаторы")
        seed_message(db, msg_id=10, text="подключим радиаторы", ts=now)
        db.conn.commit()
    res = memory.link(str(path), dry_run=True)
    check(res["dry"] is True, "dry_run: dry=True")
    check(res["new"] == 1, "dry_run: план посчитан", str(res["new"]))
    with DB(path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        check(n == 0, "dry_run: кандидатов не создано", str(n))
        cur = db.get_meta(memory.CURSOR_KEY)
        check(cur is None, "dry_run: курсор не двинулся", str(cur))


def test_recall(tmp: Path) -> None:
    print("=== recall: successor-цепочка и changed_since ===")
    path = tmp / "recall.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    status="done", closed_ts=now, created_ts=now - 100,
                    updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="подключение радиаторов v2",
                    created_ts=now, updated_ts=now)
        seed_event(db, 1, op="supersede", ts=now, field="superseded_by", to_val="2",
                   msg_id=10, quote="заменяем")
        db.conn.commit()
    with DB(path) as db:
        hits = memory.recall(db, "радиаторы")
        hit1 = next((h for h in hits if h["id"] == 1), None)
        check(hit1 is not None, "recall: сущность 1 найдена")
        check(hit1["supersede"]["successor"] == 2, "recall: successor=2",
              str(hit1["supersede"]))
        check(hit1["last_event"] is not None, "recall: last_event есть")
        check(hit1["changed_since"] is False, "recall: changed_since без since_ts")
        hits2 = memory.recall(db, "радиаторы", since_ts=now - 100)
        hit1b = next((h for h in hits2 if h["id"] == 1), None)
        check(hit1b["changed_since"] is True, "recall: changed_since True при новом событии")
        hits3 = memory.recall(db, "радиаторы", since_ts=now + 100)
        hit1c = next((h for h in hits3 if h["id"] == 1), None)
        check(hit1c["changed_since"] is False, "recall: changed_since False при старом since")


def test_rebuild_gc(tmp: Path) -> None:
    print("=== rebuild/gc: идемпотентность и чистка сирот ===")
    path = tmp / "rg.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="task", title="задача один",
                    created_ts=now, updated_ts=now)
        seed_entity(db, 2, kind="task", title="задача два",
                    created_ts=now, updated_ts=now)
        db.conn.commit()
    r1 = memory.rebuild(str(path))
    check(r1["ok"] is True and r1["indexed"] == 2, "rebuild: indexed=2", str(r1))
    r2 = memory.rebuild(str(path))
    check(r2["indexed"] == 2, "rebuild: идемпотентен", str(r2["indexed"]))
    with DB(path) as db:
        db.conn.execute(
            "INSERT INTO memory_candidates(new_entity_id, candidate_entity_id, "
            "status, created_ts) VALUES(1, 999, 'pending', 0)")
        db.conn.execute(
            "INSERT INTO memory_candidates(new_entity_id, candidate_entity_id, "
            "status, created_ts) VALUES(1, 2, 'pending', 0)")
        db.conn.execute(
            "INSERT INTO entity_vec(entity_id, model, dim, vec, updated_ts) "
            "VALUES(999, 'm', 1, x'00', 0)")
        db.conn.commit()
    g = memory.gc(str(path))
    check(g["candidates"] == 1, "gc: удалён один осиротевший кандидат", str(g["candidates"]))
    check(g["vectors"] == 1, "gc: удалён один осиротевший вектор", str(g["vectors"]))
    with DB(path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        check(n == 1, "gc: валидный кандидат остался", str(n))
        v = db.conn.execute("SELECT COUNT(*) FROM entity_vec").fetchone()[0]
        check(v == 0, "gc: вектор удалён", str(v))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_memory_"))
    try:
        test_similarity()
        test_selector(tmp)
        test_link_pending(tmp)
        test_link_auto(tmp)
        test_link_rejected(tmp)
        test_link_idempotent(tmp)
        test_link_dry_run(tmp)
        test_recall(tmp)
        test_rebuild_gc(tmp)
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
