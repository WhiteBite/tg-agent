from __future__ import annotations

import sqlite3

from db import DB
from workspace_store import WorkspaceError, entities, timestamp, page


def search(db: DB, params: dict) -> dict:
    scope = params.get("scope") or "all"
    if scope not in ("all", "entities", "messages"):
        raise WorkspaceError("Поиск: all, entities или messages")
    q = params.get("q", "").strip()
    eo, limit = page(params, "entity_")
    mo, _ = page(params, "message_")
    result = {"query": q, "entities": {"items": [], "total": 0, "offset": eo, "limit": limit},
              "messages": {"items": [], "total": 0, "offset": mo, "limit": limit}}
    if not q:
        return result
    if scope != "messages":
        result["entities"] = entities(db, {**params, "offset": eo})
    if scope == "entities":
        return result
    db.conn.create_function("ws_fold", 1, lambda text: (text or "").casefold(), deterministic=True)
    parts, args = ["m.deleted=0"], []
    if params.get("project"):
        parts.append("EXISTS(SELECT 1 FROM watch w WHERE w.chat_id=m.chat_id AND w.topic_id=m.topic_id AND w.project=?)")
        args.append(params["project"])
    if params.get("owner"):
        parts.append("m.sender_name=?")
        args.append(params["owner"])
    for key, operator in (("created_from", ">="), ("created_to", "<=")):
        if params.get(key):
            parts.append(f"m.ts {operator} ?")
            args.append(timestamp(params[key], key == "created_to"))
    def fetch(source, search_clause, search_arg):
        where = " AND ".join([*parts, search_clause])
        values = [*args, search_arg]
        total = db.conn.execute("SELECT COUNT(*) FROM " + source + " WHERE " + where, values).fetchone()[0]
        rows = [dict(r) for r in db.conn.execute(
            "SELECT m.chat_id,m.msg_id,m.topic_id,m.text,m.sender_name,m.sender_id,m.ts FROM " + source
            + " WHERE " + where + " ORDER BY m.ts DESC,m.msg_id DESC LIMIT ? OFFSET ?", [*values, limit, mo])]
        return total, rows
    if db.fts:
        try:
            total, rows = fetch("messages m JOIN messages_fts ON messages_fts.rowid=m.rowid", "messages_fts MATCH ?", q)
        except sqlite3.OperationalError:
            total, rows = fetch("messages m", "ws_fold(m.text) LIKE ?", "%" + q.casefold() + "%")
    else:
        total, rows = fetch("messages m", "ws_fold(m.text) LIKE ?", "%" + q.casefold() + "%")
    result["messages"] = {"items": rows, "total": total, "offset": mo, "limit": limit}
    return result
