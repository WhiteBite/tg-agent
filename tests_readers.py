#!/usr/bin/env python3
"""Readers retrieval-поверхности db.py: entities_list(owner), entity_changes,
project_brief, project_gaps — посев сущностей/событий прямым SQL."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB  # noqa: E402

FAILURES: list[str] = []

ENT_COLS = {"id", "pack", "chat_id", "topic_id", "kind", "title", "body", "status",
            "owner", "due_ts", "project", "confidence", "attrs_json", "dedup_key",
            "created_ts", "updated_ts", "closed_ts"}

EVENT_COLS = {"ts", "op", "field", "from_val", "to_val", "chat_id", "msg_id", "quote"}


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def seed(db: DB, now: int) -> None:
    rows = [
        (1, "demo", "task", "Задача 1", "Иван", now - 3600, "open", None, "стройка", now - 900),
        (2, "demo", "task", "Задача 2", "Мария", now + 86400, "open", None, "стройка", now - 800),
        (3, "demo", "question", "Вопрос 1", None, None, "open", None, "стройка", now - 300),
        (4, "demo", "promise", "Обещание 1", "Иван", None, "open", None, "стройка", now - 200),
        (5, "demo", "promise", "Обещание 2", None, now + 100, "open", None, "стройка", now - 100),
        (6, "demo", "promise", "Обещание 3", "Мария", now + 200, "open", None, "стройка", now - 150),
        (7, "demo", "question", "Вопрос закрыт", None, None, "done", now - 10, "стройка", now - 700),
        (8, "demo", "promise", "Обещание закрыто", "Иван", None, "done", now - 20, "стройка", now - 600),
        (9, "demo", "task", "Задача сайт", "Олег", None, "open", None, "сайт", now - 500),
        (10, "other", "task", "Задача other", "Иван", None, "open", None, "стройка", now - 400),
        (11, "demo", "task", "Задача inprog", "Олег", now + 500, "in_progress", None, "стройка", now - 90),
    ]
    db.conn.executemany(
        "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, owner, due_ts, "
        "status, closed_ts, project, created_ts, updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i, pk, -100555, 1, kd, tl, ow, du, st, cl, pr, cr, cr)
         for i, pk, kd, tl, ow, du, st, cl, pr, cr in rows],
    )
    events = [
        (1, now - 100, "new", None, None, None, -100555, 101, "создано"),
        (1, now - 50, "update", "owner", None, "Иван", -100555, 102, "Иван берёт"),
        (2, now - 200, "new", None, None, None, -100555, 103, "создано"),
        (3, now - 30, "new", None, None, None, -100555, 104, "вопрос задан"),
        (3, now - 20, "update", "title", None, "Вопрос 1", -100555, 105, "уточнение"),
        (4, now - 10, "new", None, None, None, -100555, 106, "обещание дано"),
        (5, now - 1000, "new", None, None, None, -100555, 107, "старое событие"),
        (7, now - 900, "new", None, None, None, -100555, 108, "закрытый вопрос"),
        (8, now - 800, "new", None, None, None, -100555, 109, "закрытое обещание"),
        (9, now - 40, "new", None, None, None, -100555, 110, "задача сайта"),
        (10, now - 5, "new", None, None, None, -100555, 111, "другой пакет"),
        (11, now - 70, "new", None, None, None, -100555, 112, "в работе"),
    ]
    db.conn.executemany(
        "INSERT INTO entity_events(entity_id, ts, op, field, from_val, to_val, "
        "chat_id, msg_id, quote) VALUES(?,?,?,?,?,?,?,?,?)",
        events,
    )
    db.conn.commit()


def test_entities_owner_filter(tmp: Path) -> None:
    print("=== entities_list: фильтр owner ===")
    now = int(time.time())
    with DB(tmp / "owner.sqlite") as db:
        seed(db, now)
        ids = sorted(e["id"] for e in db.entities_list(owner="Иван"))
        check(ids == [1, 4, 8, 10], "entities_list(owner): только сущности Ивана", str(ids))
        ids_m = sorted(e["id"] for e in db.entities_list(owner="Мария"))
        check(ids_m == [2, 6], "entities_list(owner=Мария): только Мария", str(ids_m))
        check(db.entities_list(owner="Нет") == [], "entities_list(owner): чужой owner пуст")
        ids_p = sorted(e["id"] for e in db.entities_list(owner="Иван", pack="demo"))
        check(ids_p == [1, 4, 8], "entities_list(owner+pack): комбинируется с pack", str(ids_p))
        gt = db.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE owner = ?", ("Иван",)).fetchone()[0]
        check(len(db.entities_list(owner="Иван")) == gt,
              "entities_list(owner): сходится с прямым SQL", f"{gt}")


def test_entity_changes(tmp: Path) -> None:
    print("=== entity_changes: сущности с событиями новее since ===")
    now = int(time.time())
    with DB(tmp / "changes.sqlite") as db:
        seed(db, now)
        since = now - 60
        gt = db.conn.execute(
            "SELECT COUNT(DISTINCT entity_id) FROM entity_events WHERE ts > ?",
            (since,)).fetchone()[0]
        res = db.entity_changes(since)
        check(res["count"] == gt == 5,
              "entity_changes: count = независимый COUNT(DISTINCT entity_id)",
              f"{res['count']} vs {gt}")
        check(res["since_ts"] == since, "entity_changes: since_ts эхом", str(res["since_ts"]))
        order = [it["id"] for it in res["items"]]
        check(order == [10, 4, 3, 9, 1],
              "entity_changes: порядок по max ts события DESC", str(order))
        check(len(res["items"]) == res["count"], "entity_changes: без limit всё на месте")
        it1 = next(it for it in res["items"] if it["id"] == 1)
        check(set(it1) == ENT_COLS | {"events"},
              "entity_changes: item = строка entities + events",
              str(sorted(set(it1) - ENT_COLS - {"events"})))
        evs = it1["events"]
        check(len(evs) == 1 and evs[0]["op"] == "update" and evs[0]["field"] == "owner"
              and evs[0]["to_val"] == "Иван",
              "entity_changes: только события новее since", str([e.get("op") for e in evs]))
        check(set(evs[0]) == EVENT_COLS,
              "entity_changes: событие несёт контрактные поля", str(sorted(evs[0])))
        it3 = next(it for it in res["items"] if it["id"] == 3)
        check([e["ts"] for e in it3["events"]] == [now - 30, now - 20],
              "entity_changes: события упорядочены по ts",
              str([e["ts"] for e in it3["events"]]))
        it10 = next(it for it in res["items"] if it["id"] == 10)
        check(it10["pack"] == "other", "entity_changes: поля сущности на месте", it10["pack"])

        fut = db.entity_changes(now + 10000)
        check(fut["count"] == 0 and fut["items"] == [],
              "entity_changes: будущий since -> пусто", str(fut["count"]))

        gt_pack = db.conn.execute(
            "SELECT COUNT(DISTINCT e.id) FROM entities e JOIN entity_events ev "
            "ON ev.entity_id = e.id WHERE ev.ts > ? AND e.pack = ?",
            (since, "demo")).fetchone()[0]
        res_pack = db.entity_changes(since, pack="demo")
        check(res_pack["count"] == gt_pack == 4
              and sorted(it["id"] for it in res_pack["items"]) == [1, 3, 4, 9],
              "entity_changes: фильтр pack уважается", str(res_pack["count"]))
        gt_proj = db.conn.execute(
            "SELECT COUNT(DISTINCT e.id) FROM entities e JOIN entity_events ev "
            "ON ev.entity_id = e.id WHERE ev.ts > ? AND e.project = ?",
            (since, "стройка")).fetchone()[0]
        res_proj = db.entity_changes(since, project="стройка")
        check(res_proj["count"] == gt_proj == 4
              and sorted(it["id"] for it in res_proj["items"]) == [1, 3, 4, 10],
              "entity_changes: фильтр project уважается", str(res_proj["count"]))
        both = db.entity_changes(since, pack="demo", project="стройка")
        check(both["count"] == 3 and sorted(it["id"] for it in both["items"]) == [1, 3, 4],
              "entity_changes: pack+project вместе", str(both["count"]))

        lim = db.entity_changes(since, limit=2)
        check(lim["count"] == 5 and len(lim["items"]) == 2
              and [it["id"] for it in lim["items"]] == [10, 4],
              "entity_changes: limit режет items, но не count",
              f"count={lim['count']} items={[it['id'] for it in lim['items']]}")


def test_project_brief(tmp: Path) -> None:
    print("=== project_brief: агрегат по проекту ===")
    now = int(time.time())
    with DB(tmp / "brief.sqlite") as db:
        seed(db, now)
        b = db.project_brief("стройка")
        check(b["project"] == "стройка" and b["pack"] is None,
              "project_brief: project/pack эхом", f"{b.get('project')}/{b.get('pack')}")
        gt_counts = {r["status"]: r["n"] for r in db.conn.execute(
            "SELECT status, COUNT(*) n FROM entities WHERE project=? GROUP BY status",
            ("стройка",))}
        check(b["counts"] == gt_counts == {"open": 7, "done": 2, "in_progress": 1},
              "project_brief: counts по статусам = независимый SQL", str(b["counts"]))
        gt_open = db.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE project=? AND closed_ts IS NULL",
            ("стройка",)).fetchone()[0]
        check(b["open"] == gt_open == 8, "project_brief: open = closed_ts IS NULL",
              str(b["open"]))
        gt_overdue = db.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE project=? AND closed_ts IS NULL "
            "AND due_ts <= ?", ("стройка", now)).fetchone()[0]
        check(b["overdue"] == gt_overdue == 1, "project_brief: overdue", str(b["overdue"]))
        gt_next = db.conn.execute(
            "SELECT MIN(due_ts) FROM entities WHERE project=? AND closed_ts IS NULL "
            "AND due_ts IS NOT NULL", ("стройка",)).fetchone()[0]
        check(b["next_due"] == gt_next == now - 3600,
              "project_brief: next_due = MIN(due_ts) открытых", str(b["next_due"]))
        gt_q = db.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE project=? AND kind='question' "
            "AND closed_ts IS NULL", ("стройка",)).fetchone()[0]
        check(b["open_questions"] == gt_q == 1, "project_brief: open_questions",
              str(b["open_questions"]))
        gt_pd = db.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE project=? AND kind='promise' "
            "AND closed_ts IS NULL AND due_ts IS NULL", ("стройка",)).fetchone()[0]
        check(b["promises_no_due"] == gt_pd == 1, "project_brief: promises_no_due",
              str(b["promises_no_due"]))
        gt_po = db.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE project=? AND kind='promise' "
            "AND closed_ts IS NULL AND (owner IS NULL OR owner='')",
            ("стройка",)).fetchone()[0]
        check(b["promises_no_owner"] == gt_po == 1, "project_brief: promises_no_owner",
              str(b["promises_no_owner"]))
        gt_owners = [[r["owner"], r["n"]] for r in db.conn.execute(
            "SELECT owner, COUNT(*) n FROM entities WHERE project=? AND closed_ts IS NULL "
            "AND owner IS NOT NULL AND owner != '' GROUP BY owner "
            "ORDER BY n DESC, owner LIMIT 5", ("стройка",))]
        check(b["top_owners"] == gt_owners == [["Иван", 3], ["Мария", 2], ["Олег", 1]],
              "project_brief: top_owners = независимый SQL", str(b["top_owners"]))
        gt_last = db.conn.execute(
            "SELECT MAX(ev.ts) FROM entity_events ev JOIN entities e "
            "ON e.id = ev.entity_id WHERE e.project = ?", ("стройка",)).fetchone()[0]
        check(b["last_event_ts"] == gt_last == now - 5, "project_brief: last_event_ts",
              str(b["last_event_ts"]))

        bp = db.project_brief("стройка", pack="demo")
        check(bp["open"] == 7 and bp["counts"]["open"] == 6
              and bp["last_event_ts"] == now - 10,
              "project_brief: pack срезает чужой пакет",
              f"open={bp['open']} last={bp['last_event_ts']}")

        b0 = db.project_brief("нет такого")
        check(b0["counts"] == {} and b0["open"] == 0 and b0["overdue"] == 0
              and b0["top_owners"] == [] and b0["last_event_ts"] is None
              and b0["next_due"] is None,
              "project_brief: пустой проект — нули без исключений", str(b0))


def test_project_gaps(tmp: Path) -> None:
    print("=== project_gaps: открытые вопросы и обещания без срока/ответственного ===")
    now = int(time.time())
    with DB(tmp / "gaps.sqlite") as db:
        seed(db, now)
        g = db.project_gaps()
        check(g["count"] == 3, "project_gaps: count = 3 дыры", str(g["count"]))
        ids = [it["id"] for it in g["items"]]
        check(ids == [3, 4, 5], "project_gaps: порядок по created_ts", str(ids))
        by_id = {it["id"]: it for it in g["items"]}
        check(by_id[3]["reason"] == "вопрос без ответа",
              "project_gaps: вопрос без ответа", by_id[3]["reason"])
        check(by_id[4]["reason"] == "обещание без срока",
              "project_gaps: обещание без срока", by_id[4]["reason"])
        check(by_id[5]["reason"] == "обещание без ответственного",
              "project_gaps: обещание без ответственного", by_id[5]["reason"])
        check(set(by_id[3]) == {"id", "pack", "kind", "title", "owner", "due_ts",
                                "project", "created_ts", "reason"},
              "project_gaps: item несёт контрактные поля", str(sorted(by_id[3])))
        check(7 not in by_id and 8 not in by_id,
              "project_gaps: закрытые сущности исключены", str(sorted(by_id)))
        gp = db.project_gaps(project="стройка")
        check(gp["count"] == 3 and [it["id"] for it in gp["items"]] == [3, 4, 5],
              "project_gaps: фильтр project", str(gp["count"]))
        check(db.project_gaps(project="сайт")["count"] == 0,
              "project_gaps: чужой project пуст")
        check(db.project_gaps(pack="demo")["count"] == 3, "project_gaps: фильтр pack")
        check(db.project_gaps(pack="other")["count"] == 0,
              "project_gaps: чужой pack пуст")
        lim = db.project_gaps(limit=2)
        check(lim["count"] == 3 and len(lim["items"]) == 2
              and [it["id"] for it in lim["items"]] == [3, 4],
              "project_gaps: limit режет items, но не count",
              f"count={lim['count']} items={[it['id'] for it in lim['items']]}")


def test_gaps_edge(tmp: Path) -> None:
    print("=== project_gaps/project_brief: обещание без срока и без ответственного ===")
    now = int(time.time())
    with DB(tmp / "gaps_edge.sqlite") as db:
        db.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, owner, due_ts, "
            "status, project, created_ts, updated_ts) "
            "VALUES('demo', -100556, 1, 'promise', 'Совсем без всего', '', NULL, 'open', "
            "'стройка', ?, ?)", (now - 50, now - 50))
        db.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, owner, due_ts, "
            "status, closed_ts, project, created_ts, updated_ts) "
            "VALUES('demo', -100556, 1, 'promise', 'Закрытое без всего', '', NULL, 'done', "
            "?, 'стройка', ?, ?)", (now - 5, now - 60, now - 5))
        db.conn.commit()
        g = db.project_gaps()
        check(g["count"] == 1 and g["items"][0]["reason"] == "обещание без срока",
              "project_gaps: без срока и без владельца — приоритет «без срока»",
              str(g["items"][0]["reason"] if g["items"] else None))
        b = db.project_brief("стройка")
        check(b["promises_no_owner"] == 1 and b["promises_no_due"] == 1,
              "project_brief: owner='' считается пустым владельцем",
              f"{b['promises_no_owner']}/{b['promises_no_due']}")
        check(len(db.entities_list(owner="")) == 2,
              "entities_list(owner=''): точное совпадение с пустой строкой",
              str(len(db.entities_list(owner=""))))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_readers_"))
    try:
        test_entities_owner_filter(tmp)
        test_entity_changes(tmp)
        test_project_brief(tmp)
        test_project_gaps(tmp)
        test_gaps_edge(tmp)
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
