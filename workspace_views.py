from __future__ import annotations

import json
import os
import time

from db import DB
from workspace_store import REVIEW_JOINS, REVIEW_STATE_SQL, WorkspaceError, metadata, page, review
from workspace_write import autoclose_stale_trials


def home(db: DB, params: dict) -> dict:
    autoclose_stale_trials(db)
    where = "WHERE e.project=?" if params.get("project") else ""
    args = [params["project"]] if params.get("project") else []
    all_rows = db.conn.execute("SELECT e.*," + REVIEW_STATE_SQL + " AS review_state" + REVIEW_JOINS + where, args)
    now = int(time.time())
    counts = dict.fromkeys(("overdue", "blocked", "open_questions", "promises_missing_due",
                           "promises_missing_owner", "review_pending"), 0)
    attention = []
    for row in all_rows:
        e = dict(row)
        state = e["review_state"]
        reasons = []
        if e["closed_ts"] is None:
            if e["due_ts"] and e["due_ts"] < now:
                counts["overdue"] += 1
                reasons.append("overdue")
            if e["status"] == "blocked":
                counts["blocked"] += 1
                reasons.append("blocked")
            if e["kind"] == "question":
                counts["open_questions"] += 1
                reasons.append("question")
                signals = db.conn.execute("SELECT DISTINCT op FROM entity_events WHERE entity_id=? "
                                          "AND op IN ('reply_received','reaction_received')", (e["id"],))
                reasons.extend(r[0] for r in signals)
            if e["kind"] == "promise" and not e["due_ts"]:
                counts["promises_missing_due"] += 1
                reasons.append("missing_due")
            if e["kind"] == "promise" and not e["owner"]:
                counts["promises_missing_owner"] += 1
                reasons.append("missing_owner")
        if state == "pending":
            counts["review_pending"] += 1
        if reasons and state != "rejected":
            attention.append({**e, "review_state": state, "reasons": reasons})
    attention.sort(key=lambda e: ("overdue" not in e["reasons"], "blocked" not in e["reasons"],
                                  e["due_ts"] or now + 864000, -e["updated_ts"]))
    fresh = {"messages_ts": db.conn.execute("SELECT MAX(ts) FROM messages WHERE deleted=0").fetchone()[0],
             "extraction_ts": db.conn.execute("SELECT MAX(ts) FROM extraction_runs").fetchone()[0],
             "pipeline_ok_ts": int(db.get_meta("pipeline:last_ok_ts") or 0) or None}
    for key in tuple(fresh):
        fresh[key.replace("_ts", "_age_sec")] = max(0, now - fresh[key]) if fresh[key] else None
    recent = [dict(r) for r in db.conn.execute(
        "SELECT ev.*,e.title,e.project,e.kind FROM entity_events ev JOIN entities e ON e.id=ev.entity_id "
        + ("WHERE e.project=? " if args else "") + "ORDER BY ev.id DESC LIMIT 10", args)]
    return {"counts": counts, "attention": attention[:30], "freshness": fresh, "recent_changes": recent}


def chat_analysis(db: DB, params: dict) -> dict:
    try:
        chat_id = int(params.get("chat_id"))
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("Нужен числовой chat_id") from exc
    raw_topic = params.get("topic_id")
    try:
        topic_id = int(raw_topic) if raw_topic not in (None, "") else 1
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("topic_id должен быть числом") from exc
    topic_id = 1 if topic_id in (0, 1) else topic_id
    chat = db.conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if chat is None:
        raise WorkspaceError("Чат не найден в индексе", 404)
    topic = db.conn.execute("SELECT * FROM topics WHERE chat_id=? AND topic_id=?",
                            (chat_id, topic_id)).fetchone()
    watch = db.conn.execute("SELECT * FROM watch WHERE chat_id=? AND topic_id=?",
                            (chat_id, topic_id)).fetchone()
    scope = ("(EXISTS(SELECT 1 FROM entity_sources es WHERE es.entity_id=e.id "
             "AND es.chat_id=? AND es.topic_id=?) "
             "OR (e.chat_id=? AND e.topic_id=? AND NOT EXISTS("
             "SELECT 1 FROM entity_sources es2 WHERE es2.entity_id=e.id)))")
    scope_args = [chat_id, topic_id, chat_id, topic_id]
    eo, elimit = page(params, "entity_")
    rows = [dict(r) for r in db.conn.execute(
        "SELECT e.* FROM entities e WHERE " + scope +
        " ORDER BY e.updated_ts DESC, e.id DESC LIMIT ? OFFSET ?",
        [*scope_args, elimit, eo])]
    for e in rows:
        e["review_state"] = review(db, e["id"])
    gap_predicate = ("e.closed_ts IS NULL AND (e.kind='question' "
                     "OR (e.kind='promise' AND e.due_ts IS NULL) "
                     "OR (e.kind='promise' AND (e.owner IS NULL OR e.owner='')))")
    gaps_total = db.conn.execute(
        "SELECT COUNT(*) FROM entities e WHERE " + gap_predicate + " AND " + scope,
        scope_args).fetchone()[0]
    gaps = [dict(r) for r in db.conn.execute(
        "SELECT e.id, e.pack, e.kind, e.title, e.owner, e.due_ts, e.project, e.created_ts, "
        "CASE WHEN e.kind='question' THEN 'вопрос без ответа' "
        "WHEN e.due_ts IS NULL THEN 'обещание без срока' "
        "ELSE 'обещание без ответственного' END AS reason "
        "FROM entities e WHERE " + gap_predicate + " AND " + scope +
        " ORDER BY e.created_ts, e.id LIMIT ?",
        [*scope_args, elimit])]
    kind_rows = db.conn.execute(
        "SELECT e.kind, e.status, COUNT(*) AS n FROM entities e WHERE " + scope +
        " GROUP BY e.kind, e.status", scope_args).fetchall()
    total = sum(r["n"] for r in kind_rows)
    counts: dict[str, int] = {}
    for r in kind_rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + r["n"]
    open_n = sum(r["n"] for r in kind_rows if r["status"] != "done")
    mo, mlimit = page(params, "message_")
    msg_total = db.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE chat_id=? AND topic_id=? AND deleted=0",
        (chat_id, topic_id)).fetchone()[0]
    mrows = [dict(r) for r in db.conn.execute(
        "SELECT msg_id, topic_id, ts, date_iso, sender_name, is_service, text, media "
        "FROM messages WHERE chat_id=? AND topic_id=? AND deleted=0 "
        "ORDER BY msg_id DESC LIMIT ? OFFSET ?", (chat_id, topic_id, mlimit, mo))]
    mrows.reverse()
    watch_item = None
    if watch is not None:
        watch_item = dict(watch)
        watch_item["new_since_read"] = db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=? AND topic_id=? "
            "AND deleted=0 AND msg_id>?",
            (chat_id, topic_id, watch_item.get("last_read") or 0)).fetchone()[0]
    return {
        "chat": dict(chat),
        "topic": dict(topic) if topic else None,
        "watch": watch_item,
        "digests": db.digests_list(chat_id, topic_id, 3),
        "counts": {"total": total, "open": open_n, "by_kind": counts},
        "entities": {"items": rows, "total": total, "offset": eo, "limit": elimit},
        "gaps": {"items": gaps, "total": gaps_total},
        "messages": {"items": mrows, "total": msg_total, "offset": mo, "limit": mlimit},
    }


def projects(db: DB) -> dict:
    names = [r[0] for r in db.conn.execute(
        "SELECT project FROM watch WHERE project IS NOT NULL AND project!='' UNION "
        "SELECT project FROM entities WHERE project IS NOT NULL AND project!='' ORDER BY 1")]
    items = []
    for name in names:
        brief = db.project_brief(name)
        topics = [dict(r) for r in db.conn.execute(
            "SELECT w.chat_id,w.topic_id,w.alias,c.title AS chat_title,t.title AS topic_title "
            "FROM watch w LEFT JOIN chats c ON c.id=w.chat_id LEFT JOIN topics t "
            "ON t.chat_id=w.chat_id AND t.topic_id=w.topic_id WHERE w.project=?", (name,))]
        last = db.conn.execute("SELECT COALESCE(MAX(ev.id),0) FROM entity_events ev JOIN entities e "
                               "ON e.id=ev.entity_id WHERE e.project=?", (name,)).fetchone()[0]
        read = int(db.get_meta(f"ws:project_review:{name}") or 0)
        count = db.conn.execute("SELECT COUNT(*) FROM entity_events ev JOIN entities e "
                                "ON e.id=ev.entity_id WHERE e.project=? AND ev.id>?", (name, read)).fetchone()[0]
        items.append({**brief, "name": name, "last_event_id": last, "reviewed_event_id": read,
                      "new_events": count, "topics": topics})
    return {"items": items}


def people(db: DB) -> dict:
    names = {r[0] for r in db.conn.execute("SELECT DISTINCT owner FROM entities WHERE owner IS NOT NULL AND owner!=''")}
    names.update(r[0][len("ws:person:"):] for r in db.conn.execute("SELECT key FROM meta WHERE key LIKE 'ws:person:%'"))
    items = []
    for name in sorted(names):
        mapping = metadata(db, f"ws:person:{name}", {})
        items.append({"key": name, "owner": name, "display_name": name, "role": "",
                      "telegram_sender_ids": [], "crm_member_guid": "", "mapped": bool(mapping), **mapping})
    return {"items": items}


def field_map() -> dict:
    raw = os.environ.get("EXPORT_101_MAP")
    try:
        mapping = json.loads(raw) if raw else {"title": "title", "body": "description", "due_ts": "deadline"}
    except json.JSONDecodeError as exc:
        raise WorkspaceError("Некорректный JSON маппинга 101") from exc
    allowed = {"title", "body", "due_ts", "owner", "project", "status", "kind", "id"}
    if not isinstance(mapping, dict) or any(k not in allowed or not isinstance(v, str) or not v for k, v in mapping.items()):
        raise WorkspaceError("Маппинг 101 должен связывать поля сущности с именами полей API")
    return mapping


def integration_status(db: DB) -> dict:
    credentials = bool(os.environ.get("EXPORT_101_URL") and (os.environ.get("EXPORT_101_TOKEN")
                       or os.environ.get("EXPORT_101_LOGIN") and os.environ.get("EXPORT_101_PASSWORD")))
    mapped_people = any(p.get("crm_member_guid") for p in people(db)["items"])
    return {"state": "unverified" if credentials else "unconfigured", "credentials_configured": credentials,
            "contract_verified": False, "network_checked": False, "sending_enabled": False,
            "project_mapping_configured": False, "owner_mapping_configured": mapped_people,
            "kinds": export_kinds(), "field_map": field_map(),
            "blockers": ["Тело создания задачи не описано в предоставленном сваггере",
                         "Нужны GUID проектов и проверенное соответствие исполнителей",
                         "Живая проверка API не выполнялась; отправка из UI недоступна"]}


def export_kinds() -> list[str]:
    return [k.strip() for k in (os.environ.get("EXPORT_101_KINDS") or "task,promise").split(",") if k.strip()]


def integration_preview(db: DB, params: dict) -> dict:
    offset, limit = page(params)
    mapping, kinds = field_map(), export_kinds()
    where = "WHERE project=?" if params.get("project") else ""
    args = [params["project"]] if params.get("project") else []
    candidates = []
    for row in db.conn.execute("SELECT * FROM entities " + where + " ORDER BY id", args):
        e = dict(row)
        if e["kind"] not in kinds:
            continue
        ext = db.conn.execute("SELECT external_id,synced_ts FROM integrations WHERE entity_id=? AND system='101'",
                              (e["id"],)).fetchone()
        cursor = ext[1] if ext else 0
        count = db.conn.execute("SELECT COUNT(*) FROM entity_events WHERE entity_id=? AND id>? AND ts<?",
                                (e["id"], cursor, int(time.time()))).fetchone()[0]
        if not count:
            continue
        external = ext[0] if ext else None
        path = os.environ.get("EXPORT_101_PATH") or "/api/tasks/v2/"
        if not path.startswith("/") or "://" in path or "?" in path:
            raise WorkspaceError("Путь 101 должен быть относительным маршрутом без query")
        payload = {dst: e[src] for src, dst in mapping.items() if e.get(src) not in (None, "")}
        candidates.append({"entity_id": e["id"], "title": e["title"], "project": e["project"],
                           "method": "PATCH" if external else "POST", "external_id": external,
                           "path": path.rstrip("/") + "/" + str(external) + "/" if external else path,
                           "payload": payload, "pending_event_count": count, "eligible": False,
                           "blockers": ["Контракт API и привязка к объекту ещё не подтверждены"]})
    return {"preview_only": True, "contract_verified": False, "total": len(candidates),
            "offset": offset, "limit": limit, "items": candidates[offset:offset + limit]}


def notification_preview(db: DB, params: dict) -> dict:
    import monitor
    summary = monitor.collect(db.path, project=params.get("project"))
    return {"preview_only": True, "project": params.get("project"),
            "lines": monitor.format_report(summary), "destination_configured": bool(os.environ.get("TG_NOTIFY_ROUTES"))}
