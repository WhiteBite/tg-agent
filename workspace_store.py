from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from db import DB, clean_text

KINDS = {"task", "promise", "question", "decision", "service", "trial", "promo"}
STATUSES = {"open", "in_progress", "blocked", "done"}
REVIEW_JOINS = (" FROM entities e LEFT JOIN (SELECT entity_id,MAX(id) AS last_event_id "
                "FROM entity_events GROUP BY entity_id) recent ON recent.entity_id=e.id "
                "LEFT JOIN meta rm ON rm.key='ws:review:'||e.id ")
REVIEW_STATE_SQL = ("CASE WHEN json_extract(rm.value,'$.revision')=COALESCE(recent.last_event_id,0) "
                    "THEN COALESCE(json_extract(rm.value,'$.state'),'pending') ELSE 'pending' END")


class WorkspaceError(ValueError):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def scrub(value):
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def metadata(db: DB, key: str, default=None):
    raw = db.get_meta(key)
    return json.loads(raw) if raw else default


def revision(db: DB, entity_id: int) -> int:
    return db.conn.execute("SELECT COALESCE(MAX(id),0) FROM entity_events WHERE entity_id=?",
                           (entity_id,)).fetchone()[0]


def review(db: DB, entity_id: int) -> str:
    data = metadata(db, f"ws:review:{entity_id}", {})
    return data.get("state", "pending") if data.get("revision") == revision(db, entity_id) else "pending"


def timestamp(value, end: bool = False) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            dt = datetime.strptime(str(value), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise WorkspaceError("Дата должна быть YYYY-MM-DD или unix-ts") from exc
        return int(dt.timestamp()) + (86399 if end else 0)


def page(params: dict, prefix: str = "") -> tuple[int, int]:
    try:
        offset = int(params.get(prefix + "offset", 0))
        limit = int(params.get("limit", 30))
    except (ValueError, TypeError) as exc:
        raise WorkspaceError("offset и limit должны быть числами") from exc
    if offset < 0 or not 1 <= limit <= 200:
        raise WorkspaceError("offset >= 0; limit от 1 до 200")
    return offset, limit


def entity_where(params: dict) -> tuple[str, list]:
    parts, args = ["1=1"], []
    for name in ("project", "owner", "status", "kind", "pack"):
        if params.get(name):
            if name == "status" and params[name] not in STATUSES:
                raise WorkspaceError("Неизвестный статус")
            if name == "kind" and params[name] not in KINDS:
                raise WorkspaceError("Неизвестный тип записи")
            parts.append(f"e.{name}=?")
            args.append(params[name])
    if params.get("q"):
        parts.append("(ws_fold(e.title) LIKE ? OR ws_fold(e.body) LIKE ?)")
        args.extend(["%" + params["q"].casefold() + "%"] * 2)
    if params.get("kinds"):
        selected_kinds = params["kinds"].split(",")
        if any(k not in KINDS for k in selected_kinds):
            raise WorkspaceError("Неизвестные типы записей")
        parts.append("e.kind IN (" + ",".join("?" for _ in selected_kinds) + ")")
        args.extend(selected_kinds)
    for key, operator in (("created_from", ">="), ("created_to", "<="), ("changed_after", ">")):
        if params.get(key):
            parts.append(f"e.{'updated_ts' if key=='changed_after' else 'created_ts'} {operator} ?")
            args.append(timestamp(params[key], key == "created_to"))
    due = params.get("due")
    if due:
        if due not in ("overdue", "missing", "upcoming"):
            raise WorkspaceError("Неизвестный фильтр срока")
        parts.append("e.closed_ts IS NULL")
        if due == "missing":
            parts.append("e.due_ts IS NULL")
        else:
            now = int(time.time())
            parts.append("e.due_ts <= ?" if due == "overdue" else "e.due_ts BETWEEN ? AND ?")
            args.extend([now] if due == "overdue" else [now, now + 7 * 86400])
    return " AND ".join(parts), args


def entities(db: DB, params: dict) -> dict:
    db.conn.create_function("ws_fold", 1, lambda text: (text or "").casefold(), deterministic=True)
    offset, limit = page(params)
    where, args = entity_where(params)
    sort = params.get("sort") or "updated_ts"
    if sort not in ("due_ts", "updated_ts", "created_ts", "title"):
        raise WorkspaceError("Неизвестная сортировка")
    order = params.get("order") or "desc"
    if order not in ("asc", "desc"):
        raise WorkspaceError("Порядок сортировки: asc или desc")
    state = params.get("review_state")
    if state and state not in ("pending", "approved", "rejected"):
        raise WorkspaceError("Неизвестное состояние проверки")
    if state:
        where += f" AND ({REVIEW_STATE_SQL})=?"
        args.append(state)
    total = db.conn.execute("SELECT COUNT(*)" + REVIEW_JOINS + "WHERE " + where, args).fetchone()[0]
    selected = [dict(r) for r in db.conn.execute(
        "SELECT e.*,COALESCE(recent.last_event_id,0) AS last_event_id," + REVIEW_STATE_SQL
        + " AS review_state" + REVIEW_JOINS + f"WHERE {where} ORDER BY e.{sort} {order},e.id LIMIT ? OFFSET ?",
        [*args, limit, offset])]
    for row in selected:
        row["source_count"] = db.conn.execute(
            "SELECT COUNT(*) FROM entity_sources WHERE entity_id=?", (row["id"],)).fetchone()[0]
        ext = db.conn.execute(
            "SELECT external_id FROM integrations WHERE entity_id=? AND system='101'",
            (row["id"],)).fetchone()
        row["external_id"] = ext[0] if ext else None
    return {"items": selected, "total": total, "offset": offset, "limit": limit}


def entity(db: DB, entity_id: int) -> dict:
    alias = db.conn.execute("SELECT entity_id FROM entity_aliases WHERE old_entity_id=?",
                            (entity_id,)).fetchone()
    entity_id = alias[0] if alias else entity_id
    row = db.conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        raise WorkspaceError("Запись не найдена", 404)
    item = dict(row)
    item["review_state"] = review(db, entity_id)
    events = [dict(r) for r in db.conn.execute(
        "SELECT * FROM entity_events WHERE entity_id=? ORDER BY id DESC", (entity_id,))]
    evidence, seen = [], set()
    for ev in events:
        if ev["msg_id"] is None or ev["chat_id"] is None or not ev["quote"]:
            continue
        key = (ev["chat_id"], ev["msg_id"], ev["quote"])
        if key in seen:
            continue
        seen.add(key)
        msg = db.conn.execute("SELECT * FROM messages WHERE chat_id=? AND msg_id=?", key[:2]).fetchone()
        evidence.append({"chat_id": key[0], "msg_id": key[1], "quote": key[2],
                         "topic_id": msg["topic_id"] if msg else item["topic_id"],
                         "text": msg["text"] if msg else None, "missing": msg is None,
                         "deleted": bool(msg["deleted"]) if msg else False,
                         "sender_name": msg["sender_name"] if msg else None,
                         "ts": msg["ts"] if msg else ev["ts"]})
    ext = db.conn.execute("SELECT external_id,synced_ts FROM integrations WHERE entity_id=? AND system='101'",
                          (entity_id,)).fetchone()
    return {"entity": item, "revision": revision(db, entity_id), "events": events,
            "evidence": evidence, "integration": {"external_id": ext[0] if ext else None,
            "synced_cursor": ext[1] if ext else 0, "contract_verified": False,
            "status": "recorded_locally" if ext else "never_exported"}}


def changes(db: DB, params: dict) -> dict:
    offset, limit = page(params)
    project = params.get("project")
    reviewed = int(db.get_meta(f"ws:project_review:{project}") or 0)
    try:
        after = int(params.get("after_event_id") or reviewed)
    except (ValueError, TypeError) as exc:
        raise WorkspaceError("Некорректный курсор изменений") from exc
    if after < 0:
        raise WorkspaceError("Курсор должен быть >= 0")
    sql = ("FROM entity_events ev JOIN entities e ON e.id=ev.entity_id WHERE ev.id>?"
           + (" AND e.project=?" if project else ""))
    args = [after, project] if project else [after]
    total = db.conn.execute("SELECT COUNT(*) " + sql, args).fetchone()[0]
    items = [dict(r) for r in db.conn.execute(
        "SELECT ev.*,e.title,e.project,e.kind " + sql + " ORDER BY ev.id LIMIT ? OFFSET ?",
        [*args, limit, offset])]
    snapshot = db.conn.execute("SELECT COALESCE(MAX(ev.id),0) " + sql, args).fetchone()[0]
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "snapshot_event_id": snapshot, "reviewed_event_id": reviewed}
