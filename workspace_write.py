from __future__ import annotations

import json
import time

import engine
from db import DB, clean_text, sanitize_title
from workspace_store import KINDS, STATUSES, WorkspaceError, metadata, revision


def put_meta(db: DB, key: str, value) -> None:
    db.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))


def human_event(db: DB, entity_id: int, op: str, field: str, old, new, note: str = "",
                origin: str = "human") -> int:
    return db.conn.execute(
        "INSERT INTO entity_events(entity_id,ts,op,field,from_val,to_val,quote,origin) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (entity_id, int(time.time()), op, field, None if old is None else str(old),
         None if new is None else str(new), clean_text(note), origin)).lastrowid


def checked_entity(db: DB, body: dict) -> dict:
    try:
        entity_id, expected = int(body["id"]), int(body["expected_revision"])
    except (KeyError, ValueError, TypeError) as exc:
        raise WorkspaceError("Нужны id и expected_revision") from exc
    row = db.conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    if not row:
        raise WorkspaceError("Запись не найдена", 404)
    if revision(db, entity_id) != expected:
        raise WorkspaceError("Запись изменилась. Перезагрузите карточку перед сохранением.", 409)
    return dict(row)


def entity_review(db: DB, body: dict) -> dict:
    state = body.get("state")
    if state not in ("approved", "rejected"):
        raise WorkspaceError("Проверка: approved или rejected")
    note = body.get("note") or ""
    if not isinstance(note, str) or len(note) > 4000:
        raise WorkspaceError("Комментарий должен быть текстом до 4000 символов")
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        e = checked_entity(db, body)
        old = metadata(db, f"ws:review:{e['id']}", {})
        if old.get("state") == state and old.get("note") == note and old.get("revision") == revision(db, e["id"]):
            return {"ok": True, "id": e["id"], "revision": revision(db, e["id"]), "unchanged": True}
        rev = human_event(db, e["id"], "human_review", "review_state", old.get("state", "pending"), state, note)
        put_meta(db, f"ws:review:{e['id']}", {"state": state, "note": note, "revision": rev, "ts": int(time.time())})
    return {"ok": True, "id": e["id"], "revision": rev,
            "note": "Локальная проверка. Экспортёр пока не использует её как запрет отправки."}


def entity_edit(db: DB, body: dict) -> dict:
    fields = body.get("fields")
    allowed = {"title", "body", "owner", "due_ts", "status", "kind"}
    if not isinstance(fields, dict) or not fields or set(fields) - allowed:
        raise WorkspaceError("Неизвестные или пустые поля исправления")
    for key, value in fields.items():
        if key in ("title", "body", "owner") and value is not None and not isinstance(value, str):
            raise WorkspaceError("Название, описание и ответственный должны быть текстом")
        if key == "title" and (not value or not value.strip() or len(value) > 500):
            raise WorkspaceError("Нужно название до 500 символов")
        if key == "status" and value not in STATUSES:
            raise WorkspaceError("Неизвестный статус")
        if key == "kind" and value not in KINDS:
            raise WorkspaceError("Неизвестный тип записи")
        if key == "due_ts" and value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise WorkspaceError("Срок должен быть unix-ts или null")
    changes = []
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        e = checked_entity(db, body)
        for key, value in fields.items():
            if isinstance(value, str):
                value = clean_text(value.strip())
            if key == "title" and value is not None:
                value = sanitize_title(value, 500)
            if e[key] == value:
                continue
            changes.append((key, e[key], value))
            db.conn.execute(f"UPDATE entities SET {key}=? WHERE id=?", (value, e["id"]))
            human_event(db, e["id"], "human_edit", key, e[key], value)
        if changes:
            now = int(time.time())
            if "status" in fields:
                closed = e["closed_ts"] or now if fields["status"] == "done" else None
                db.conn.execute("UPDATE entities SET closed_ts=? WHERE id=?", (closed, e["id"]))
            db.conn.execute("UPDATE entities SET updated_ts=? WHERE id=?", (now, e["id"]))
            put_meta(db, f"ws:review:{e['id']}", {"state": "pending", "revision": revision(db, e["id"])})
    return {"ok": True, "id": e["id"], "revision": revision(db, e["id"]), "changed": [c[0] for c in changes]}


def bulk_close(db: DB, body: dict) -> dict:
    try:
        days = int(body.get("older_than_days", 30))
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("older_than_days должен быть числом") from exc
    if not 1 <= days <= 3650:
        raise WorkspaceError("older_than_days от 1 до 3650")
    kinds = body.get("kinds") or ["trial"]
    if not isinstance(kinds, list) or not kinds or any(k not in KINDS for k in kinds):
        raise WorkspaceError("Неизвестные типы записей")
    cutoff = int(time.time()) - days * 86400
    closed = []
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" for _ in kinds)
        rows = db.conn.execute(
            "SELECT id,status FROM entities WHERE closed_ts IS NULL AND due_ts IS NOT NULL "
            f"AND due_ts < ? AND kind IN ({marks}) ORDER BY updated_ts LIMIT 500",
            [cutoff, *kinds]).fetchall()
        now = int(time.time())
        for r in rows:
            db.conn.execute(
                "UPDATE entities SET status='done', closed_ts=?, updated_ts=? WHERE id=?",
                (now, now, r["id"]))
            human_event(db, r["id"], "human_edit", "status", r["status"], "done",
                        "массовое закрытие протухших")
            put_meta(db, f"ws:review:{r['id']}",
                     {"state": "pending", "revision": revision(db, r["id"])})
            closed.append(r["id"])
    return {"ok": True, "closed": len(closed), "older_than_days": days}


def autoclose_stale_trials(db: DB, grace_days: int = 7, batch: int = 500) -> int:
    cutoff = int(time.time()) - grace_days * 86400
    closed = []
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        rows = db.conn.execute(
            "SELECT id,status,due_ts FROM entities WHERE closed_ts IS NULL AND kind='trial' "
            "AND due_ts IS NOT NULL AND due_ts < ? ORDER BY due_ts LIMIT ?",
            (cutoff, batch)).fetchall()
        now = int(time.time())
        for r in rows:
            db.conn.execute("UPDATE entities SET status='done', closed_ts=?, updated_ts=? WHERE id=?",
                            (r["due_ts"], now, r["id"]))
            human_event(db, r["id"], "auto_close", "status", r["status"], "done",
                        "триал давно истёк", origin="system")
            put_meta(db, f"ws:review:{r['id']}",
                     {"state": "pending", "revision": revision(db, r["id"])})
            closed.append(r["id"])
    return len(closed)


def visit(db: DB, body: dict) -> dict:
    try:
        chat_id = int(body["chat_id"])
        topic_id = int(body.get("topic_id", 1)) or 1
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceError("Нужны числовые chat_id и topic_id") from exc
    if not db.conn.execute("SELECT 1 FROM topics WHERE chat_id=? AND topic_id=?",
                           (chat_id, topic_id)).fetchone():
        raise WorkspaceError("Тема не найдена в индексе", 404)
    with db.conn:
        put_meta(db, f"ws:visit:{chat_id}:{topic_id}", int(time.time()))
    return {"ok": True, "chat_id": chat_id, "topic_id": topic_id}


def project_review(db: DB, body: dict) -> dict:
    project = body.get("project")
    try:
        through = int(body["through_event_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise WorkspaceError("Нужен курсор through_event_id") from exc
    if not isinstance(project, str) or not project.strip() or through < 0:
        raise WorkspaceError("Нужны проект и неотрицательный курсор")
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        maximum = db.conn.execute("SELECT COALESCE(MAX(ev.id),0) FROM entity_events ev JOIN entities e "
                                  "ON e.id=ev.entity_id WHERE e.project=?", (project,)).fetchone()[0]
        if through > maximum:
            raise WorkspaceError("Курсор выше последнего события проекта")
        old = int(db.get_meta(f"ws:project_review:{project}") or 0)
        put_meta(db, f"ws:project_review:{project}", max(old, through))
    return {"ok": True, "project": project, "reviewed_event_id": max(old, through)}


def person(db: DB, body: dict) -> dict:
    key = body.get("key")
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise WorkspaceError("Нужен ключ человека")
    fields = {k: body.get(k, "") for k in ("display_name", "role", "crm_member_guid")}
    if any(not isinstance(v, str) or len(v) > 500 for v in fields.values()):
        raise WorkspaceError("Поля человека должны быть текстом до 500 символов")
    ids = body.get("telegram_sender_ids", [])
    if not isinstance(ids, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
        raise WorkspaceError("Telegram ID должны быть списком целых чисел")
    with db.conn:
        put_meta(db, f"ws:person:{key}", {**fields, "telegram_sender_ids": ids})
    return {"ok": True, "key": key}


def set_alerts(db: DB, body: dict) -> dict:
    if not any(k in body for k in ("words", "mentions")):
        raise WorkspaceError("Нужны words или mentions")
    stored = metadata(db, "ws:alerts", {})
    if not isinstance(stored, dict):
        stored = {}
    for key in ("words", "mentions"):
        if key not in body:
            continue
        raw = body[key]
        if (not isinstance(raw, list) or len(raw) > 100
                or any(not isinstance(v, str) or not v.strip() or len(v) > 60 for v in raw)):
            raise WorkspaceError(f"{key}: список строк до 100 штук, каждая до 60 символов")
        stored[key] = [clean_text(v.strip()) for v in raw]
    with db.conn:
        put_meta(db, "ws:alerts", stored)
    return {"ok": True, "words": stored.get("words", []), "mentions": stored.get("mentions", [])}


def engine_start(db: DB, body: dict) -> dict:
    try:
        return engine.start(db.path, str(body.get("engine") or "pipeline"),
                            int(body.get("minutes") or 30))
    except PermissionError as exc:
        raise WorkspaceError(str(exc), 403) from exc
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(str(exc)) from exc


def engine_stop(db: DB, body: dict) -> dict:
    try:
        return engine.stop(db.path, str(body.get("engine") or "pipeline"))
    except PermissionError as exc:
        raise WorkspaceError(str(exc), 403) from exc
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(str(exc)) from exc


def catalog_star(db: DB, body: dict) -> dict:
    key = body.get("key")
    starred = body.get("starred")
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise WorkspaceError("Нужен ключ сервиса до 200 символов")
    if not isinstance(starred, bool):
        raise WorkspaceError("starred: true или false")
    stored = metadata(db, "ws:catalog_stars", [])
    stars = [str(s) for s in stored] if isinstance(stored, list) else []
    key = key.strip()
    if starred and key not in stars:
        stars.append(key)
    elif not starred and key in stars:
        stars.remove(key)
    with db.conn:
        put_meta(db, "ws:catalog_stars", stars)
    return {"ok": True, "key": key, "starred": starred}


def project_topics(db: DB, body: dict) -> dict:
    project, topics = body.get("project"), body.get("topics")
    if not isinstance(project, str) or not project.strip() or len(project) > 120:
        raise WorkspaceError("Нужно название проекта до 120 символов")
    if not isinstance(topics, list) or not topics or len(topics) > 500:
        raise WorkspaceError("Выберите от 1 до 500 тем")
    pairs = []
    for topic in topics:
        try:
            cid, tid = int(topic["chat_id"]), int(topic["topic_id"]) or 1
        except (KeyError, ValueError, TypeError) as exc:
            raise WorkspaceError("Нужны chat_id и topic_id") from exc
        if not db.conn.execute("SELECT 1 FROM topics WHERE chat_id=? AND topic_id=?", (cid, tid)).fetchone():
            raise WorkspaceError("Одна из тем отсутствует в индексе")
        pairs.append((cid, tid))
    project = clean_text(project.strip())
    with db.conn:
        for cid, tid in set(pairs):
            db.conn.execute("INSERT INTO watch(chat_id,topic_id,project,added_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(chat_id,topic_id) DO UPDATE SET project=excluded.project",
                            (cid, tid, project, int(time.time())))
            db.conn.execute("UPDATE entities SET project=? WHERE chat_id=? AND topic_id=? AND closed_ts IS NULL",
                            (project, cid, tid))
    return {"ok": True, "project": project, "topics": [{"chat_id": c, "topic_id": t} for c, t in pairs]}
