#!/usr/bin/env python3
"""Извлечение сущностей из сообщений по декларативным пакетам (packs/).

    EXTRACT_PROVIDER=stub python3 extract.py            # механический извлекатель (без LLM)
    EXTRACT_PROVIDER=stub python3 extract.py --dry-run  # показать операции, ничего не писать
    python3 extract.py --pack construction --db demo.sqlite

По каждой паре (пакет, watch-тема) берёт сообщения после прошлого прогона
(курсор extract:{chat}:{topic}:{pack} в meta; первый прогон — окно backfill_days),
зовёт провайдера и применяет его операции new одной транзакцией: entities,
entity_events, extraction_runs, курсор. Сбой провайдера курсор не двигает.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402
from packs import PACKS  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

MAX_OPS_PER_BATCH = 50
OPEN_ENTITIES_CAP = 50

_LINE_RE = re.compile(r"^\[(\d+)\] (\S+) (.*?): (.*)$")
_CHAT_RE = re.compile(r"чат (-?\d+)")
_OWNER_RE = re.compile(r"у ([A-ZА-Я][\w]+)")
_DUE_RE = re.compile(r"до (\d{2}\.\d{2}\.\d{4})")
_STUB_TRIGGERS = (
    ("decision", "решено"), ("decision", "договорились"),
    ("task", "задача"), ("task", "надо "),
    ("promise", "обещаю"), ("promise", "сделаю"),
    ("question", "вопрос"), ("question", "когда "),
)


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def scope_matches(scope_filter: dict, w: dict) -> bool:
    sf = scope_filter or {}
    for field, key in (("topic_title", "topic_title_regex"),
                       ("alias", "alias_regex"), ("project", "project_regex")):
        rx = sf.get(key)
        if rx is not None and not re.search(rx, w.get(field) or ""):
            return False
    chat_ids = sf.get("chat_ids")
    if chat_ids is not None and w["chat_id"] not in chat_ids:
        return False
    return True


def build_prompt(db: DB, pack: dict, w: dict, rows: list[dict]) -> str:
    alias = w.get("alias") or w.get("topic_title") or ""
    scope_header = f"Тема «{alias}», чат {w['chat_id']}, новых сообщений: {len(rows)}"
    lines = [f"[{r['msg_id']}] {r['date_iso'] or '-'} {r['sender_name'] or '?'}: {r['text'] or ''}"
             for r in rows]
    open_rows = db.conn.execute(
        "SELECT id, kind, title, owner, due_ts FROM entities "
        "WHERE pack=? AND chat_id=? AND topic_id=? AND closed_ts IS NULL "
        "ORDER BY id LIMIT ?",
        (pack["id"], w["chat_id"], w["topic_id"], OPEN_ENTITIES_CAP)).fetchall()
    open_lines = [f"{r['id']} | {r['kind']} | {r['title']} | {r['owner'] or ''} | {r['due_ts'] or ''}"
                  for r in open_rows]
    return pack["prompt_template"].format(
        scope_header=scope_header,
        messages="\n".join(lines),
        open_entities="\n".join(open_lines) or "—",
    )


def provider_stub(prompt: str, pack: dict) -> dict:
    chat_m = _CHAT_RE.search(prompt)
    chat_id = int(chat_m.group(1)) if chat_m else 0
    ops: list[dict] = []
    covered = 0
    for line in prompt.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        msg_id, text = int(m.group(1)), m.group(4)
        covered = max(covered, msg_id)
        for kind, trigger in _STUB_TRIGGERS:
            if kind not in pack["kinds"] or trigger not in text:
                continue
            owner_m = _OWNER_RE.search(text)
            due_m = _DUE_RE.search(text)
            due_ts = (int(datetime.strptime(due_m.group(1), "%d.%m.%Y")
                          .replace(tzinfo=timezone.utc).timestamp()) if due_m else None)
            ops.append({
                "op": "new",
                "kind": kind,
                "title": text[text.index(trigger) + len(trigger):].strip()[:120],
                "entity_id": None,
                "body": None,
                "owner": owner_m.group(1) if owner_m else None,
                "due_text": due_m.group(1) if due_m else None,
                "due_ts": due_ts,
                "confidence": 0.6,
                "evidence": [{"chat_id": chat_id, "msg_id": msg_id, "quote": text[:200]}],
            })
            break
    return {"ops": ops, "covered_upto_msg_id": covered}


def provider_openai(prompt: str, pack: dict) -> dict:
    raise NotImplementedError("openai provider: T3b")


def get_provider(provider=None):
    if provider is not None:
        return provider
    name = os.environ.get("EXTRACT_PROVIDER", "stub")
    if name == "stub":
        return provider_stub
    if name == "openai":
        return provider_openai
    raise RuntimeError(f"неизвестный EXTRACT_PROVIDER: {name}")


def _validate_ops(db: DB, pack: dict, ops: list[dict],
                  lower: int, upto_msg: int) -> tuple[list[dict], int]:
    valid: list[dict] = []
    rejected = 0
    for op in ops:
        ok = (op.get("op") == "new" and op.get("kind") in pack["kinds"]
              and bool(op.get("evidence")))
        for ev in op.get("evidence") or []:
            msg_id = ev.get("msg_id")
            if not isinstance(msg_id, int) or not lower < msg_id <= upto_msg:
                ok = False
                break
            row = db.conn.execute(
                "SELECT text FROM messages WHERE chat_id=? AND msg_id=?",
                (ev.get("chat_id"), msg_id)).fetchone()
            if row is None or _norm(ev.get("quote")) not in _norm(row["text"]):
                ok = False
                break
        if ok and len(valid) < MAX_OPS_PER_BATCH:
            valid.append(op)
        else:
            rejected += 1
    return valid, rejected


def _apply_ops(db: DB, pack: dict, w: dict, valid: list[dict], run_id: int) -> None:
    now = int(time.time())
    for op in valid:
        kind = op["kind"]
        title = op.get("title") or ""
        dedup_key = hashlib.sha1(
            f"{kind}|{_norm(title).lower()}".encode("utf-8")).hexdigest()
        attrs = {"due_text": op["due_text"]} if op.get("due_text") else None
        try:
            cur = db.conn.execute(
                "INSERT INTO entities(pack, chat_id, topic_id, kind, title, body, owner, "
                "due_ts, project, confidence, attrs_json, dedup_key, created_ts, updated_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pack["id"], w["chat_id"], w["topic_id"], kind, title, op.get("body"),
                 op.get("owner"), op.get("due_ts"), w.get("project"), op.get("confidence"),
                 json.dumps(attrs, ensure_ascii=False) if attrs is not None else None,
                 dedup_key, now, now))
            entity_id, ev_op, field = cur.lastrowid, "new", None
        except sqlite3.IntegrityError:
            row = db.conn.execute(
                "SELECT id FROM entities WHERE pack=? AND chat_id=? AND topic_id=? "
                "AND dedup_key=? AND closed_ts IS NULL",
                (pack["id"], w["chat_id"], w["topic_id"], dedup_key)).fetchone()
            if row is None:
                raise
            entity_id, ev_op, field = row["id"], "update", "dedup"
            db.conn.execute("UPDATE entities SET updated_ts=? WHERE id=?", (now, entity_id))
        for ev in op.get("evidence") or []:
            db.conn.execute(
                "INSERT INTO entity_events(entity_id, run_id, ts, op, field, "
                "chat_id, msg_id, quote) VALUES(?,?,?,?,?,?,?,?)",
                (entity_id, run_id, now, ev_op, field,
                 ev.get("chat_id"), ev.get("msg_id"), ev.get("quote")))


def _run_scope(db: DB, pack: dict, w: dict, prov, dry_run: bool) -> dict | None:
    chat_id, topic_id = w["chat_id"], w["topic_id"]
    key = f"extract:{chat_id}:{topic_id}:{pack['id']}"
    row = db.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    cursor = int(row["value"]) if row else None
    sql = ("SELECT * FROM messages WHERE chat_id=? AND COALESCE(topic_id,1)=? "
           "AND msg_id>? AND is_service=0")
    args: list = [chat_id, topic_id, cursor if cursor is not None else 0]
    if cursor is None:
        sql += " AND ts>=?"
        args.append(int(time.time()) - pack["backfill_days"] * 86400)
    rows = [dict(r) for r in db.conn.execute(sql + " ORDER BY msg_id", args)]
    if not rows:
        return None
    from_msg = cursor + 1 if cursor is not None else rows[0]["msg_id"]
    upto_msg = rows[-1]["msg_id"]
    result = prov(build_prompt(db, pack, w, rows), pack)
    valid, rejected = _validate_ops(
        db, pack, result.get("ops") or [], cursor if cursor is not None else 0, upto_msg)
    if dry_run:
        print(f"[dry-run] {pack['id']} {chat_id}/{topic_id}: "
              f"ops={len(valid)} rejected={rejected}")
        for op in valid:
            print(f"  {op.get('op')} {op.get('kind')}: {op.get('title')}")
        return {"pack": pack["id"], "chat_id": chat_id, "topic_id": topic_id,
                "messages": len(rows), "ops_applied": 0, "ops_rejected": rejected,
                "upto_msg": upto_msg, "dry_run": True}
    now = int(time.time())
    status = "ok" if rejected == 0 else "partial"
    cur = db.conn.execute(
        "INSERT INTO extraction_runs(pack, chat_id, topic_id, from_msg, upto_msg, ts, "
        "ops_applied, ops_rejected, status) VALUES(?,?,?,?,?,?,?,?,?)",
        (pack["id"], chat_id, topic_id, from_msg, upto_msg, now, len(valid), rejected, status))
    run_id = cur.lastrowid
    _apply_ops(db, pack, w, valid, run_id)
    covered = result.get("covered_upto_msg_id")
    db.conn.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(min(covered or upto_msg, upto_msg))))
    db.conn.commit()
    return {"pack": pack["id"], "chat_id": chat_id, "topic_id": topic_id,
            "messages": len(rows), "ops_applied": len(valid), "ops_rejected": rejected,
            "upto_msg": upto_msg}


def run(db_path: str = DB_PATH, pack_id: str | None = None,
        dry_run: bool = False, provider=None) -> list[dict]:
    prov = get_provider(provider)
    packs = [PACKS[pack_id]] if pack_id is not None else list(PACKS.values())
    made: list[dict] = []
    with DB(db_path) as db:
        for pack in packs:
            for w in db.watch_list():
                if not scope_matches(pack["scope_filter"], w):
                    continue
                try:
                    res = _run_scope(db, pack, w, prov, dry_run)
                except Exception as exc:
                    db.conn.rollback()
                    print(f"  ! {pack['id']} {w['chat_id']}/{w['topic_id']}: {exc}",
                          file=sys.stderr)
                    continue
                if res is not None:
                    made.append(res)
    return made


def _arg_value(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    pack_id = _arg_value(args, "--pack")
    db_path = _arg_value(args, "--db") or DB_PATH
    made = run(db_path, pack_id=pack_id, dry_run=dry_run)
    total = sum(m["ops_applied"] for m in made)
    print(f"областей обработано: {len(made)}, операций применено: {total}")
    for m in made:
        print(f"  {m['pack']} {m['chat_id']}/{m['topic_id']} ({m['messages']} msgs): "
              f"ops={m['ops_applied']} rejected={m['ops_rejected']}")


if __name__ == "__main__":
    main()
