"""Долгие задания с сохраняемым состоянием: таблицы jobs / job_items.

Задание — snapshot параметров и плановых пунктов на момент создания, поэтому
прерванный прогон продолжается с места остановки, а не начинается заново.
Состояния: planned -> running -> done | failed | cancelled; running -> paused
-> running. Исполнитель обязан между пунктами проверять состояние: paused
останавливает выборку новых пунктов, cancelled — терминальное.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from db import DB

ITEM_STATES = ("planned", "done", "skipped", "failed")


def create_job(db: DB, kind: str, params: dict, items: list[dict]) -> int:
    cur = db.conn.execute(
        "INSERT INTO jobs(kind, params_json, state, created_ts) "
        "VALUES(?,?,'planned',?)",
        (kind, json.dumps(params, ensure_ascii=False), int(time.time())))
    job_id = int(cur.lastrowid)
    db.conn.executemany(
        "INSERT OR IGNORE INTO job_items(job_id, chat_id, msg_id) VALUES(?,?,?)",
        [(job_id, int(i["chat_id"]), int(i["msg_id"])) for i in items])
    db.conn.commit()
    return job_id


def progress_counts(db: DB, job_id: int) -> dict:
    rows = db.conn.execute(
        "SELECT state, COUNT(*) AS n FROM job_items WHERE job_id=? GROUP BY state",
        (job_id,)).fetchall()
    counts = {r["state"]: r["n"] for r in rows}
    counts["total"] = sum(counts.values())
    return counts


def job_get(db: DB, job_id: int) -> Optional[dict]:
    row = db.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["params"] = json.loads(job.pop("params_json") or "{}")
    job["progress"] = json.loads(job.pop("progress_json") or "null")
    job["result"] = json.loads(job.pop("result_json") or "null")
    job["counts"] = progress_counts(db, job_id)
    return job


def job_list(db: DB, kind: Optional[str] = None, limit: int = 50) -> list[dict]:
    sql, args = "SELECT id FROM jobs", []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(int(limit), 500))
    return [job for r in db.conn.execute(sql, args) if (job := job_get(db, r["id"]))]


def claim(db: DB, job_id: int, force: bool = False) -> bool:
    """Забрать задание в работу. force — вернуть себе running/failed
    и повторно прогнать завершённое (пункты done/skipped не трогаются,
    retry_failed сбрасывает упавшие)."""
    states = "('planned','paused','running','failed','done')" if force \
        else "('planned','paused')"
    cur = db.conn.execute(
        f"UPDATE jobs SET state='running', started_ts=COALESCE(started_ts,?), "
        f"updated_ts=? WHERE id=? AND state IN {states}",
        (int(time.time()), int(time.time()), job_id))
    db.conn.commit()
    return cur.rowcount == 1


def pause(db: DB, job_id: int) -> bool:
    cur = db.conn.execute(
        "UPDATE jobs SET state='paused', updated_ts=? WHERE id=? AND state='running'",
        (int(time.time()), job_id))
    db.conn.commit()
    return cur.rowcount == 1


def resume(db: DB, job_id: int) -> bool:
    cur = db.conn.execute(
        "UPDATE jobs SET state='running', updated_ts=? WHERE id=? AND state='paused'",
        (int(time.time()), job_id))
    db.conn.commit()
    return cur.rowcount == 1


def cancel(db: DB, job_id: int) -> bool:
    cur = db.conn.execute(
        "UPDATE jobs SET state='cancelled', updated_ts=?, finished_ts=? "
        "WHERE id=? AND state IN ('planned','paused','running')",
        (int(time.time()), int(time.time()), job_id))
    db.conn.commit()
    return cur.rowcount == 1


def is_active(db: DB, job_id: int) -> bool:
    row = db.conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row is not None and row["state"] == "running"


def item_next(db: DB, job_id: int) -> Optional[dict]:
    row = db.conn.execute(
        "SELECT chat_id, msg_id FROM job_items WHERE job_id=? AND state='planned' "
        "ORDER BY chat_id, msg_id LIMIT 1", (job_id,)).fetchone()
    return dict(row) if row is not None else None


def mark_item(db: DB, job_id: int, chat_id: int, msg_id: int,
              state: str, detail: Optional[str] = None) -> None:
    if state not in ITEM_STATES:
        raise ValueError(f"неизвестное состояние пункта: {state!r}")
    db.conn.execute(
        "UPDATE job_items SET state=?, detail=?, ts=? "
        "WHERE job_id=? AND chat_id=? AND msg_id=?",
        (state, detail, int(time.time()), job_id, chat_id, msg_id))
    db.conn.execute("UPDATE jobs SET updated_ts=? WHERE id=?",
                    (int(time.time()), job_id))
    db.conn.commit()


def retry_failed(db: DB, job_id: int) -> int:
    cur = db.conn.execute(
        "UPDATE job_items SET state='planned', detail=NULL, ts=? "
        "WHERE job_id=? AND state='failed'", (int(time.time()), job_id))
    db.conn.commit()
    return cur.rowcount


def finish(db: DB, job_id: int, state: str, error: Optional[str] = None,
           result: Optional[dict] = None) -> None:
    db.conn.execute(
        "UPDATE jobs SET state=?, finished_ts=?, updated_ts=?, error=?, "
        "result_json=? WHERE id=?",
        (state, int(time.time()), int(time.time()), error,
         json.dumps(result, ensure_ascii=False) if result is not None else None,
         job_id))
    db.conn.commit()


def report(db: DB, job_id: int, failures_limit: int = 20) -> dict:
    counts = progress_counts(db, job_id)
    skipped: dict[str, int] = {}
    for r in db.conn.execute(
            "SELECT COALESCE(detail,'') AS d, COUNT(*) AS n FROM job_items "
            "WHERE job_id=? AND state='skipped' GROUP BY d ORDER BY n DESC",
            (job_id,)):
        skipped[r["d"] or "other"] = r["n"]
    failed = [dict(r) for r in db.conn.execute(
        "SELECT chat_id, msg_id, detail FROM job_items "
        "WHERE job_id=? AND state='failed' ORDER BY chat_id, msg_id LIMIT ?",
        (job_id, failures_limit))]
    failed_total = counts.get("failed", 0)
    return {"counts": counts, "skipped": skipped,
            "failed": failed, "failed_total": failed_total}
