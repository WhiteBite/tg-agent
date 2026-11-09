#!/usr/bin/env python3
"""memory: селектор открытых сущностей, similarity, linker, recall, rebuild/gc."""

from __future__ import annotations

import contextlib
import io
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
from packs import PACKS

import memory

FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


EMBED_ENV = ("MEMORY_EMBED_PROVIDER", "MEMORY_EMBED_KEY", "MEMORY_EMBED_URL",
             "MEMORY_EMBED_MODEL", "MEMORY_EMBED_TIMEOUT")


def clear_embed_env() -> None:
    for k in EMBED_ENV:
        os.environ.pop(k, None)


def set_embed_env(model: str = "test-embed") -> None:
    os.environ["MEMORY_EMBED_PROVIDER"] = "openai"
    os.environ["MEMORY_EMBED_KEY"] = "sk-test"
    os.environ["MEMORY_EMBED_URL"] = "https://example.invalid/v1"
    os.environ["MEMORY_EMBED_MODEL"] = model


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


def test_embed(tmp: Path) -> None:
    print("=== embed: opt-in, upsert, смена модели, no-op без провайдера ===")
    path = tmp / "embed.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="task", title="подключение радиаторов",
                    body="корпус Б", created_ts=now, updated_ts=now)
        db.conn.commit()

    clear_embed_env()
    res = memory.embed(str(path))
    check(res.get("skipped") is True, "embed: без провайдера skipped", str(res))
    with DB(path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM entity_vec").fetchone()[0]
        check(n == 0, "embed: без провайдера векторов нет", str(n))

    set_embed_env("test-embed")
    calls: list[list[str]] = []
    orig = memory._embed_texts

    def fake(texts):
        calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    memory._embed_texts = fake
    try:
        res = memory.embed(str(path))
        check(res["ok"] is True and res["embedded"] == 1, "embed: один вектор", str(res))
        check(res["model"] == "test-embed", "embed: модель в отчёте", str(res))
        check(calls and calls[0] == ["подключение радиаторов\nкорпус Б"],
              "embed: текст = title+body", str(calls))
        with DB(path) as db:
            row = db.conn.execute(
                "SELECT * FROM entity_vec WHERE entity_id=1").fetchone()
            check(row is not None, "embed: строка entity_vec создана")
            check(row["model"] == "test-embed", "embed: model записан", row["model"])
            check(row["dim"] == 3, "embed: dim=3", str(row["dim"]))
            check(memory._unpack_vec(row["vec"]) == [1.0, 0.0, 0.0],
                  "embed: vec round-trip")
        res2 = memory.embed(str(path))
        check(res2["embedded"] == 0, "embed: свежий не переэмбеддится", str(res2))
        set_embed_env("test-embed-2")
        res3 = memory.embed(str(path))
        check(res3["embedded"] == 1, "embed: смена модели переэмбеддит", str(res3))
        with DB(path) as db:
            row = db.conn.execute(
                "SELECT model FROM entity_vec WHERE entity_id=1").fetchone()
            check(row["model"] == "test-embed-2", "embed: модель обновлена", row["model"])
    finally:
        memory._embed_texts = orig
        clear_embed_env()


def test_recall_semantic(tmp: Path) -> None:
    print("=== recall: семантический кандидат вне лексических ===")
    path = tmp / "recall_sem.sqlite"
    now = int(time.time())
    with DB(path) as db:
        for i in range(1, 11):
            seed_entity(db, i, kind="task", title=f"радиатор {i}",
                        created_ts=now - 300 * 86400,
                        updated_ts=now - 300 * 86400)
        seed_entity(db, 50, kind="decision", title="монтаж отопительных приборов",
                    created_ts=now, updated_ts=now)
        db.conn.commit()

    clear_embed_env()
    with DB(path) as db:
        hits = memory.recall(db, "радиатор", limit=5)
    ids = {h["id"] for h in hits}
    check(50 not in ids, "recall: без векторов семантический не в топ-5", str(sorted(ids)))

    set_embed_env("test-embed")
    with DB(path) as db:
        for i in range(1, 11):
            db.conn.execute(
                "INSERT INTO entity_vec(entity_id, model, dim, vec, updated_ts) "
                "VALUES(?,?,?,?,?)",
                (i, "test-embed", 3, memory._pack_vec([0.0, 1.0, 0.0]), now))
        db.conn.execute(
            "INSERT INTO entity_vec(entity_id, model, dim, vec, updated_ts) "
            "VALUES(?,?,?,?,?)",
            (50, "test-embed", 3, memory._pack_vec([1.0, 0.0, 0.0]), now))
        db.conn.commit()

    orig = memory._embed_texts
    memory._embed_texts = lambda texts, timeout=None: [[1.0, 0.0, 0.0] for _ in texts]
    try:
        with DB(path) as db:
            hits = memory.recall(db, "радиатор", limit=5)
        ids = {h["id"] for h in hits}
        check(50 in ids, "recall: семантический кандидат в топ-5", str(sorted(ids)))
    finally:
        memory._embed_texts = orig
        clear_embed_env()


def test_link_cosine(tmp: Path) -> None:
    print("=== linker: косинус как вторичный сигнал ===")
    now = int(time.time())

    def build(path: Path, vec2, cue: bool) -> None:
        with DB(path) as db:
            seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                        created_ts=now - 100, updated_ts=now - 100)
            seed_entity(db, 2, kind="decision", title="монтаж отопительных приборов",
                        created_ts=now, updated_ts=now)
            seed_event(db, 2, op="new", ts=now, msg_id=10, quote="монтаж")
            seed_message(db, msg_id=10,
                         text=("передумали, монтаж отопительных приборов" if cue
                               else "монтаж отопительных приборов"), ts=now)
            if vec2 is not None:
                db.conn.execute(
                    "INSERT INTO entity_vec(entity_id, model, dim, vec, updated_ts) "
                    "VALUES(?,?,?,?,?)",
                    (1, "test-embed", 3, memory._pack_vec([1.0, 0.0, 0.0]), now))
                db.conn.execute(
                    "INSERT INTO entity_vec(entity_id, model, dim, vec, updated_ts) "
                    "VALUES(?,?,?,?,?)",
                    (2, "test-embed", 3, memory._pack_vec(vec2), now))
            db.conn.commit()

    p0 = tmp / "linkcos0.sqlite"
    build(p0, None, cue=False)
    clear_embed_env()
    r0 = memory.link(str(p0), dry_run=False)
    check(r0["new"] == 0, "link cos: без векторов кандидата нет", str(r0["new"]))

    p1 = tmp / "linkcos1.sqlite"
    build(p1, [0.8, 0.6, 0.0], cue=False)
    set_embed_env("test-embed")
    r1 = memory.link(str(p1), dry_run=False)
    check(r1["new"] == 1, "link cos: кандидат по косинусу", str(r1["new"]))
    check(r1["auto"] == 0, "link cos: auto нет без cue", str(r1["auto"]))
    with DB(p1) as db:
        cand = db.conn.execute(
            "SELECT status FROM memory_candidates WHERE new_entity_id=2 "
            "AND candidate_entity_id=1").fetchone()
        check(cand is not None and cand["status"] == "pending",
              "link cos: статус pending", str(cand and cand["status"]))

    p2 = tmp / "linkcos2.sqlite"
    build(p2, [0.9, 0.4, 0.0], cue=True)
    r2 = memory.link(str(p2), dry_run=False, auto=True)
    check(r2["auto"] == 1, "link cos: авто-supersede по косинусу", str(r2["auto"]))
    with DB(p2) as db:
        p = db.conn.execute(
            "SELECT status, closed_ts FROM entities WHERE id=1").fetchone()
        check(p["closed_ts"] is not None, "link cos: P закрыт")
        check(p["status"] == "done", "link cos: P status=done", p["status"])
    clear_embed_env()


def test_recall_reachability(tmp: Path) -> None:
    print("=== recall: старое решение вне топ-1000 достижимо через FTS ===")
    path = tmp / "recall_reach.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    created_ts=now - 400 * 86400, updated_ts=now - 400 * 86400)
        for i in range(2, 1102):
            seed_entity(db, i, kind="task", title=f"задача номер {i}",
                        created_ts=now - 86400, updated_ts=now - 86400)
        db.conn.commit()
    with DB(path) as db:
        hits = memory.recall(db, "радиатор", limit=20)
    ids = {h["id"] for h in hits}
    check(1 in ids, "recall H1: старое решение вне топ-1000 найдено", str(sorted(ids)[:5]))


def test_recall_filter_rank(tmp: Path) -> None:
    print("=== recall: фильтр не пропускает чужой ранг в лексику ===")
    now = int(time.time())
    p1 = tmp / "recall_f1.sqlite"
    with DB(p1) as db:
        seed_entity(db, 1, pack="construction", kind="task",
                    title="подключение радиаторов", created_ts=now, updated_ts=now)
        db.conn.commit()
    p2 = tmp / "recall_f2.sqlite"
    with DB(p2) as db:
        seed_entity(db, 1, pack="construction", kind="task",
                    title="подключение радиаторов", created_ts=now, updated_ts=now)
        seed_entity(db, 2, pack="services", kind="task",
                    title="радиаторы радиаторы радиаторы радиаторы",
                    created_ts=now, updated_ts=now)
        db.conn.commit()
    with DB(p1) as db:
        h1 = memory.recall(db, "радиаторы", pack="construction", limit=5)
    with DB(p2) as db:
        h2 = memory.recall(db, "радиаторы", pack="construction", limit=5)
    s1 = next(h["score"] for h in h1 if h["id"] == 1)
    s2 = next(h["score"] for h in h2 if h["id"] == 1)
    check(abs(s1 - s2) < 1e-9, "recall M6: чужой пак не влияет на лексику", f"{s1} vs {s2}")
    check(all(h["pack"] == "construction" for h in h2), "recall M6: только свой пак")


def test_counts_chunk(tmp: Path) -> None:
    print("=== _counts: чанки по SQL_CHUNK ===")
    path = tmp / "counts.sqlite"
    now = int(time.time())
    n = 33000
    with DB(path) as db:
        db.conn.executemany(
            "INSERT INTO entity_events(entity_id, ts, op, quote) VALUES(?,?,?,?)",
            [(i, now, "new", "q") for i in range(1, n + 1)])
        db.conn.commit()
        ev, _deg = memory._counts(db, list(range(1, n + 1)))
    check(len(ev) == n, "counts M5: все события посчитаны", str(len(ev)))
    check(all(v == 1 for v in ev.values()), "counts M5: по одному событию")


def test_link_last_event(tmp: Path) -> None:
    print("=== linker: cue в позднем update -> auto ===")
    path = tmp / "link_last.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="подключение радиаторов",
                    created_ts=now, updated_ts=now)
        seed_event(db, 2, op="new", ts=now - 10, msg_id=10, quote="подключение радиаторов")
        seed_event(db, 2, op="update", ts=now, msg_id=11, quote="отменяем")
        seed_message(db, msg_id=10, text="подключение радиаторов", ts=now - 10)
        seed_message(db, msg_id=11, text="передумали, отменяем подключение радиаторов", ts=now)
        db.conn.commit()
    res = memory.link(str(path), dry_run=False, auto=True)
    check(res["auto"] == 1, "link M7: cue в позднем update сработал", str(res["auto"]))
    with DB(path) as db:
        cand = db.conn.execute(
            "SELECT status, msg_id FROM memory_candidates WHERE new_entity_id=2 "
            "AND candidate_entity_id=1").fetchone()
        check(cand is not None and cand["status"] == "auto", "link M7: статус auto",
              str(cand and cand["status"]))
        check(cand["msg_id"] == 11, "link M7: evidence из последнего события",
              str(cand and cand["msg_id"]))


def test_link_rollback(tmp: Path) -> None:
    print("=== linker: исключение -> rollback, курсор не двинулся ===")
    path = tmp / "link_rb.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="подключение радиаторов",
                    created_ts=now, updated_ts=now)
        seed_entity(db, 3, kind="decision", title="подключение радиаторов",
                    created_ts=now, updated_ts=now)
        seed_event(db, 2, op="new", ts=now - 5, msg_id=10, quote="цитата")
        seed_event(db, 3, op="new", ts=now, msg_id=11, quote="цитата")
        seed_message(db, msg_id=10, text="подключение радиаторов", ts=now - 5)
        seed_message(db, msg_id=11, text="подключение радиаторов", ts=now)
        db.conn.commit()
    orig = memory._find_candidates
    calls = {"n": 0}

    def flaky(db, e, vecs=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("boom")
        return orig(db, e, vecs)

    memory._find_candidates = flaky
    try:
        res = memory.link(str(path), dry_run=False)
    finally:
        memory._find_candidates = orig
    check(res["ok"] is False, "link M4: ok=False при исключении", str(res))
    with DB(path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        check(n == 0, "link M4: частичные кандидаты откатаны", str(n))
        cur = db.get_meta(memory.CURSOR_KEY)
        check(cur is None, "link M4: курсор не двинулся", str(cur))


def test_accept_honesty(tmp: Path) -> None:
    print("=== review: accept при неудачном supersede не врёт ===")
    path = tmp / "accept.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="старое",
                    status="done", closed_ts=now - 50,
                    created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="новое",
                    created_ts=now, updated_ts=now)
        db.conn.execute(
            "INSERT INTO memory_candidates(new_entity_id, candidate_entity_id, "
            "status, created_ts) VALUES(2, 1, 'pending', 0)")
        db.conn.commit()
    res = memory._review(str(path), ["--accept", "1"])
    check(res["ok"] is True, "accept M3: ok=True")
    check(res["applied"] is False, "accept M3: applied=False", str(res))
    check(res["status"] == "pending", "accept M3: статус остался pending", str(res))
    with DB(path) as db:
        st = db.conn.execute(
            "SELECT status FROM memory_candidates WHERE id=1").fetchone()["status"]
        check(st == "pending", "accept M3: в БД pending", st)


def test_link_auto_gate(tmp: Path) -> None:
    print("=== linker: auto не ставится при неудачном supersede ===")
    path = tmp / "link_gate.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="подключение радиаторов",
                    created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="подключение радиаторов",
                    created_ts=now, updated_ts=now)
        seed_event(db, 2, op="new", ts=now, msg_id=10, quote="цитата")
        seed_message(db, msg_id=10, text="передумали, отменяем подключение радиаторов", ts=now)
        db.conn.commit()
    orig = memory._apply_supersede
    memory._apply_supersede = lambda *a, **k: False
    try:
        res = memory.link(str(path), dry_run=False, auto=True)
    finally:
        memory._apply_supersede = orig
    check(res["auto"] == 0, "link L4: auto не сработал", str(res["auto"]))
    with DB(path) as db:
        cand = db.conn.execute(
            "SELECT status FROM memory_candidates WHERE new_entity_id=2 "
            "AND candidate_entity_id=1").fetchone()
        check(cand is not None and cand["status"] == "pending",
              "link L4: кандидат pending", str(cand and cand["status"]))


def test_gc_stale(tmp: Path) -> None:
    print("=== gc: закрытая сторона -> кандидат stale ===")
    path = tmp / "gc_stale.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="decision", title="старое",
                    status="done", closed_ts=now, created_ts=now - 100, updated_ts=now - 100)
        seed_entity(db, 2, kind="decision", title="новое", created_ts=now, updated_ts=now)
        db.conn.execute(
            "INSERT INTO memory_candidates(new_entity_id, candidate_entity_id, "
            "status, created_ts) VALUES(2, 1, 'pending', 0)")
        db.conn.commit()
    g = memory.gc(str(path))
    check(g["stale"] == 1, "gc M9: один stale", str(g))
    with DB(path) as db:
        st = db.conn.execute("SELECT status FROM memory_candidates").fetchone()["status"]
        check(st == "stale", "gc M9: статус stale", st)


def test_rebuild_recreate(tmp: Path) -> None:
    print("=== rebuild: пересоздаёт удалённый entities_fts ===")
    path = tmp / "rebuild_re.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="task", title="задача один", created_ts=now, updated_ts=now)
        seed_entity(db, 2, kind="task", title="задача два", created_ts=now, updated_ts=now)
        db.conn.commit()
        db.conn.execute("DROP TABLE entities_fts")
        db.conn.commit()
    r = memory.rebuild(str(path))
    check(r["ok"] is True and r["indexed"] == 2, "rebuild LOW-b: indexed=2", str(r))
    with DB(path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
        check(n == 2, "rebuild LOW-b: таблица восстановлена", str(n))
        hit = db.conn.execute(
            "SELECT entity_id FROM entities_fts WHERE entities_fts MATCH ?",
            ("задача*",)).fetchall()
        check(len(hit) == 2, "rebuild LOW-b: поиск работает", str(len(hit)))


def test_resolve_alias_chain(tmp: Path) -> None:
    print("=== _resolve_alias: цепочка A->B->C ===")
    path = tmp / "alias.sqlite"
    with DB(path) as db:
        db.conn.execute("INSERT INTO entity_aliases(old_entity_id, entity_id) VALUES(1, 2)")
        db.conn.execute("INSERT INTO entity_aliases(old_entity_id, entity_id) VALUES(2, 3)")
        db.conn.commit()
        check(memory._resolve_alias(db, 1) == 3, "alias LOW-c: 1->3",
              str(memory._resolve_alias(db, 1)))
        check(memory._resolve_alias(db, 3) == 3, "alias LOW-c: 3->3")


def test_reject_missing(tmp: Path) -> None:
    print("=== review: reject несуществующего id -> ошибка ===")
    path = tmp / "reject.sqlite"
    with DB(path) as db:
        db.conn.commit()
    res = memory._review(str(path), ["--reject", "999"])
    check(res["ok"] is False, "reject LOW-d: ok=False", str(res))
    check(res.get("error") == "candidate not found", "reject LOW-d: ошибка", str(res))


def test_cli_safe_int(tmp: Path) -> None:
    print("=== CLI: нечисловой --accept/--limit -> структурная ошибка ===")
    path = tmp / "cli.sqlite"
    with DB(path) as db:
        db.conn.commit()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memory.main(["review", "--accept", "abc", "--db", str(path)])
    check(rc == 1, "CLI LOW-a: review --accept abc rc=1", str(rc))
    out = json.loads(buf.getvalue())
    check(out.get("ok") is False, "CLI LOW-a: review структурная ошибка", str(out))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memory.main(["embed", "--limit", "abc", "--db", str(path)])
    check(rc == 1, "CLI LOW-a: embed --limit abc rc=1", str(rc))
    out = json.loads(buf.getvalue())
    check(out.get("ok") is False, "CLI LOW-a: embed структурная ошибка", str(out))


def test_unpack_vec_guard(tmp: Path) -> None:
    print("=== _unpack_vec: не кратно 4 -> [] ===")
    check(memory._unpack_vec(b"\x00\x01\x02") == [], "unpack L5: 3 байта -> []")
    check(memory._unpack_vec(b"\x00\x01\x02\x03\x04") == [], "unpack L5: 5 байт -> []")
    check(memory._unpack_vec(b"") == [], "unpack L5: пусто -> []")
    v = memory._unpack_vec(memory._pack_vec([1.5, -2.0]))
    check(v == [1.5, -2.0], "unpack L5: кратно 4 round-trip", str(v))


def test_cue_transcript(tmp: Path) -> None:
    print("=== _cue_hit: cue в транскрипте медиа ===")
    path = tmp / "cue_tr.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_message(db, msg_id=10, text="обычный текст без маркера", ts=now)
        db.conn.execute(
            "INSERT INTO media_assets(chat_id, msg_id, kind, status, text, ts) "
            "VALUES(?,?,?,?,?,?)", (-100, 10, "voice", "done",
                                    "передумали, отменяем решение", now))
        db.conn.commit()
        check(memory._cue_hit(db, {"chat_id": -100, "msg_id": 10}) is True,
              "cue L6: маркер из транскрипта")
        check(memory._cue_hit(db, {"chat_id": -100, "msg_id": 999}) is False,
              "cue L6: нет сообщения -> False")


def test_embed_mismatch(tmp: Path) -> None:
    print("=== embed: короткий ответ не ok:true ===")
    path = tmp / "embed_mm.sqlite"
    now = int(time.time())
    with DB(path) as db:
        seed_entity(db, 1, kind="task", title="задача", created_ts=now, updated_ts=now)
        db.conn.commit()
    set_embed_env("test-embed")
    orig = memory._embed_texts
    memory._embed_texts = lambda texts, timeout=None: []
    try:
        res = memory.embed(str(path))
        check(res["ok"] is False, "embed L7: короткий ответ -> ok=False", str(res))
        with DB(path) as db:
            n = db.conn.execute("SELECT COUNT(*) FROM entity_vec").fetchone()[0]
            check(n == 0, "embed L7: ничего не записано", str(n))
    finally:
        memory._embed_texts = orig
        clear_embed_env()


def test_title_render(tmp: Path) -> None:
    print("=== selector: title санитизируется в строке промпта ===")
    path = tmp / "title.sqlite"
    now = 1_700_000_000
    with DB(path) as db:
        seed_entity(db, 1, kind="task", title="строка\n" + "x" * 500,
                    created_ts=now, updated_ts=now)
        db.conn.commit()
    pack = PACKS["construction"]
    w = {"chat_id": -100, "topic_id": 5, "project": "Объекд",
         "alias": "Стройка", "topic_title": "Стройка"}
    with DB(path) as db:
        lines = memory.select_open_entities(db, pack, w, [], now=now)
    line = next((ln for ln in lines if ln.startswith("1 | ")), None)
    check(line is not None, "render H2: строка сущности есть")
    title_part = line.split(" | ")[2]
    check("\n" not in title_part and "\r" not in title_part and "\t" not in title_part,
          "render H2: переводы строк убраны", repr(title_part[:20]))
    check(len(title_part) <= 120, "render H2: title обрезан до 120", str(len(title_part)))


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
        test_embed(tmp)
        test_recall_semantic(tmp)
        test_link_cosine(tmp)
        test_recall_reachability(tmp)
        test_recall_filter_rank(tmp)
        test_counts_chunk(tmp)
        test_link_last_event(tmp)
        test_link_rollback(tmp)
        test_accept_honesty(tmp)
        test_link_auto_gate(tmp)
        test_gc_stale(tmp)
        test_rebuild_recreate(tmp)
        test_resolve_alias_chain(tmp)
        test_reject_missing(tmp)
        test_cli_safe_int(tmp)
        test_unpack_vec_guard(tmp)
        test_cue_transcript(tmp)
        test_embed_mismatch(tmp)
        test_title_render(tmp)
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
