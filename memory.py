#!/usr/bin/env python3
"""Долговременная память: отбор открытых сущностей в промпт, детерминированная
связка кандидатов на замену, гибридный recall, обслуживание индекса.

    python3 memory.py link [--apply]     # связка кандидатов (по умолчанию dry-run)
    python3 memory.py rebuild            # перестроить entities_fts
    python3 memory.py gc                 # убрать осиротевшие кандидаты/векторы
    python3 memory.py embed [--limit N]  # посчитать эмбеддинги (opt-in, MEMORY_EMBED_*)
    python3 memory.py review [--status pending] [--accept ID] [--reject ID]

Только stdlib и модули репозитория; эмбеддинги — opt-in через MEMORY_EMBED_PROVIDER.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env

load_env()

from db import DB, ENTITIES_FTS_SCHEMA

import llm_http
import people

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

CURSOR_KEY = "memory:link:cursor"
SCAN_LIMIT = 500
SELECT_K = 60
LEX_CAP = 40
CORE_CAP = 12
CAND_CAP = 5
SIM_CAND = 0.3
SIM_AUTO = 0.5
COS_CAND = 0.7
COS_AUTO = 0.85
SEM_TOPN = 50
EMBED_DEFAULT_MODEL = "text-embedding-3-small"
EMBED_DEFAULT_URL = "https://api.openai.com/v1"
RECALL_EMBED_TIMEOUT = 5.0
EMBED_BATCH = 100
SQL_CHUNK = 900
TITLE_CAP = 120

_CUE_RE = re.compile(
    r"передумал|по-другому|по другому|вместо|отмен|меняем|измен|пересмотр|"
    r"не так|иначе|замен|переигр|снимаем|другой вариант|пересоглас",
    re.IGNORECASE)

_KIND_W = {"decision": 1.0, "approval": 0.9, "task": 0.6, "promise": 0.5,
           "question": 0.4, "service": 0.5, "trial": 0.4, "promo": 0.3}

_STOPWORDS = frozenset({
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех",
    "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть",
    "после", "над", "больше", "тот", "через", "эти", "нас", "про", "всего",
    "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем",
    "хорошо", "свою", "этой", "перед", "иногда", "лучше", "чуть", "том",
    "нельзя", "такой", "им", "более", "всегда", "конечно", "всю", "между",
    "the", "and", "for", "are", "was", "were", "been", "this", "that", "with",
    "from", "not", "but", "then", "they", "she", "does", "did", "has", "have",
    "had", "will", "would", "can", "could", "should", "may", "might", "must",
    "about", "into", "over", "after", "before", "than", "too", "very", "just",
    "also", "only", "more", "most", "some", "any", "all", "yes",
})


def _norm(text: str | None) -> str:
    t = unicodedata.normalize("NFKC", text or "").casefold()
    chars = [ch if (ch.isalnum() or ch.isspace()) else " " for ch in t]
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def _tokens(text: str | None) -> list[str]:
    return [t for t in _norm(text).split() if len(t) >= 3 and t not in _STOPWORDS]


def _grams(s: str) -> set[str]:
    if len(s) < 4:
        return {s} if s else set()
    return {s[i:i + 4] for i in range(len(s) - 3)}


def _similarity(a: str | None, b: str | None) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    ga, gb = _grams(na), _grams(nb)
    if not ga or not gb:
        return 0.0
    union = len(ga | gb)
    return len(ga & gb) / union if union else 0.0


def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_vec(blob) -> list[float]:
    if not blob:
        return []
    b = bytes(blob)
    if len(b) % 4 != 0:
        return []
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


def _as_floats(v) -> list[float]:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return _unpack_vec(bytes(v))
    return [float(x) for x in v]


def _cosine(a, b) -> float:
    va, vb = _as_floats(a), _as_floats(b)
    if not va or not vb or len(va) != len(vb):
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _load_vecs(db, model: str) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    try:
        rows = db.conn.execute(
            "SELECT entity_id, vec FROM entity_vec WHERE model=?", (model,))
    except sqlite3.OperationalError:
        return out
    for r in rows:
        vec = _unpack_vec(r["vec"])
        if vec:
            out[r["entity_id"]] = vec
    return out


_VEC_CACHE: dict[tuple[str, str], tuple[int, dict[int, list[float]]]] = {}


def _load_vecs_cached(db, model: str) -> dict[int, list[float]]:
    try:
        n = db.conn.execute("SELECT COUNT(*) FROM entity_vec").fetchone()[0]
    except sqlite3.OperationalError:
        return {}
    key = (str(db.path), model)
    cached = _VEC_CACHE.get(key)
    if cached is not None and cached[0] == n:
        return cached[1]
    vecs = _load_vecs(db, model)
    _VEC_CACHE[key] = (n, vecs)
    return vecs


def _chunks(seq, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _fetch_by_ids(db, where: str, args: list, ids) -> list[dict]:
    out: list[dict] = []
    for chunk in _chunks(list(ids), SQL_CHUNK):
        marks = ",".join("?" * len(chunk))
        out.extend(dict(r) for r in db.conn.execute(
            f"SELECT * FROM entities WHERE {where} AND id IN ({marks})",
            list(args) + list(chunk)))
    return out


def _recall_filter(pack, project, kind, prefix: str = "") -> tuple[str, list]:
    where = "1=1"
    args: list = []
    if pack is not None:
        where += f" AND {prefix}pack=?"
        args.append(pack)
    if project is not None:
        where += f" AND {prefix}project=?"
        args.append(project)
    if kind is not None:
        where += f" AND {prefix}kind=?"
        args.append(kind)
    return where, args


def _title_for_prompt(title) -> str:
    t = re.sub(r"[\r\n\t]+", " ", title or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:TITLE_CAP]


def _importance(events: int, degree: int, kind: str) -> float:
    kw = _KIND_W.get(kind, 0.4)
    return min(1.0, 0.4 * math.log1p(events) / math.log1p(20)
               + 0.3 * min(degree, 5) / 5 + 0.3 * kw)


def _recency(updated_ts, created_ts, now: int) -> float:
    ts = updated_ts or created_ts or now
    age_days = max(0.0, (now - ts) / 86400)
    return 0.5 ** (age_days / 30)


def _embed_config() -> dict | None:
    provider = os.environ.get("MEMORY_EMBED_PROVIDER", "").strip()
    key_raw = os.environ.get("MEMORY_EMBED_KEY", "").strip()
    if not provider or not key_raw:
        return None
    url = os.environ.get("MEMORY_EMBED_URL", "").strip() or EMBED_DEFAULT_URL
    model = os.environ.get("MEMORY_EMBED_MODEL", "").strip() or EMBED_DEFAULT_MODEL
    try:
        timeout = float(os.environ.get("MEMORY_EMBED_TIMEOUT", "") or 30)
    except ValueError:
        timeout = 30.0
    return {"provider": provider, "url": url.rstrip("/"), "model": model,
            "keys": llm_http.key_list(key_raw), "timeout": timeout}


def _embed_texts(texts: list[str], timeout: float | None = None) -> list[list[float]]:
    cfg = _embed_config()
    if cfg is None:
        raise RuntimeError("embeddings provider not configured")
    payload = {"model": cfg["model"], "input": list(texts)}
    t = cfg["timeout"] if timeout is None else min(cfg["timeout"], timeout)
    data = llm_http.post_json_keys(
        f"{cfg['url']}/embeddings", cfg["keys"], payload, t)
    items = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
    return [[float(x) for x in it["embedding"]] for it in items]


def _batch_terms(rows: list[dict]) -> list[str]:
    freq: dict[str, int] = {}
    for r in rows:
        for t in _tokens(r.get("text") or ""):
            freq[t] = freq.get(t, 0) + 1
    top = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    return [t for t, _ in top]


def _counts(db, ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    ev_count: dict[int, int] = {}
    degree: dict[int, int] = {}
    if not ids:
        return ev_count, degree
    for chunk in _chunks(list(ids), SQL_CHUNK):
        marks = ",".join("?" * len(chunk))
        for r in db.conn.execute(
                f"SELECT entity_id, COUNT(*) n FROM entity_events "
                f"WHERE entity_id IN ({marks}) GROUP BY entity_id", chunk):
            ev_count[r["entity_id"]] = r["n"]
        for r in db.conn.execute(
                f"SELECT entity_id, COUNT(*) n FROM ("
                f"SELECT from_entity_id AS entity_id FROM entity_links WHERE link_kind='blocks' "
                f"UNION ALL SELECT to_entity_id FROM entity_links WHERE link_kind='blocks') "
                f"WHERE entity_id IN ({marks}) GROUP BY entity_id", chunk):
            degree[r["entity_id"]] = r["n"]
    return ev_count, degree


def select_open_entities(db, pack, w, rows, now=None) -> list[str]:
    now = int(time.time()) if now is None else int(now)
    pack_id = pack["id"]
    chat_id, topic_id = w["chat_id"], w["topic_id"]
    project = w.get("project") or ""

    cand: dict[int, dict] = {}

    def add(row, scope):
        eid = row["id"]
        cur = cand.get(eid)
        if cur is None:
            cand[eid] = {"row": row, "scope": scope, "lex": 0.0, "core": False}
        elif scope > cur["scope"]:
            cur["scope"] = scope

    s1 = [dict(r) for r in db.conn.execute(
        "SELECT e.* FROM entities e JOIN entity_sources s ON s.entity_id = e.id "
        "WHERE e.pack=? AND s.chat_id=? AND s.topic_id=? AND e.closed_ts IS NULL",
        (pack_id, chat_id, topic_id))]
    s1_ids = {r["id"] for r in s1}
    for r in s1:
        add(r, 1.0)

    if project:
        for r in db.conn.execute(
                "SELECT * FROM entities WHERE pack=? AND project=? AND closed_ts IS NULL",
                (pack_id, project)):
            add(dict(r), 0.6)

    terms = _batch_terms(rows)
    lex_ids: list[int] = []
    if terms and db.fts_entities:
        match = " OR ".join(t + "*" for t in terms)
        sql = ("SELECT e.*, bm25(entities_fts) AS rank FROM entities_fts "
               "JOIN entities e ON e.id = entities_fts.entity_id "
               "WHERE entities_fts MATCH ? AND e.pack=? AND e.closed_ts IS NULL")
        args: list = [match, pack_id]
        if project:
            sql += " AND e.project=?"
            args.append(project)
        sql += " ORDER BY rank LIMIT ?"
        args.append(LEX_CAP)
        try:
            for r in db.conn.execute(sql, args):
                d = dict(r)
                lex_ids.append(d["id"])
                add(d, 0.0)
        except sqlite3.OperationalError:
            lex_ids = []
    n_lex = len(lex_ids)
    for pos, eid in enumerate(lex_ids):
        if eid in cand:
            cand[eid]["lex"] = 1.0 - pos / max(1, n_lex)

    core_order: list[int] = []

    def core_add(rows_):
        for r in rows_:
            eid = r["id"]
            if eid not in cand:
                add(r, 1.0 if eid in s1_ids else 0.0)
            if eid not in core_order:
                core_order.append(eid)

    core_add([dict(r) for r in db.conn.execute(
        "SELECT e.* FROM entities e JOIN entity_sources s ON s.entity_id = e.id "
        "WHERE e.pack=? AND s.chat_id=? AND s.topic_id=? AND e.closed_ts IS NULL "
        "AND e.status='blocked' "
        "ORDER BY COALESCE(e.updated_ts,e.created_ts) DESC LIMIT 4",
        (pack_id, chat_id, topic_id))])
    core_add([dict(r) for r in db.conn.execute(
        "SELECT e.* FROM entities e JOIN entity_sources s ON s.entity_id = e.id "
        "WHERE e.pack=? AND s.chat_id=? AND s.topic_id=? AND e.closed_ts IS NULL "
        "AND e.due_ts IS NOT NULL AND e.due_ts <= ? ORDER BY e.due_ts LIMIT 4",
        (pack_id, chat_id, topic_id, now))])
    core_add([dict(r) for r in db.conn.execute(
        "SELECT e.* FROM entities e JOIN entity_sources s ON s.entity_id = e.id "
        "WHERE e.pack=? AND s.chat_id=? AND s.topic_id=? AND e.closed_ts IS NULL "
        "AND e.kind IN ('decision','approval') "
        "ORDER BY COALESCE(e.updated_ts,e.created_ts) DESC LIMIT 4",
        (pack_id, chat_id, topic_id))])
    linked = [dict(r) for r in db.conn.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM entity_links l WHERE l.link_kind='blocks' "
        "AND (l.from_entity_id=e.id OR l.to_entity_id=e.id)) AS degree "
        "FROM entities e JOIN entity_sources s ON s.entity_id = e.id "
        "WHERE e.pack=? AND s.chat_id=? AND s.topic_id=? AND e.closed_ts IS NULL "
        "ORDER BY degree DESC, COALESCE(e.updated_ts,e.created_ts) DESC LIMIT 3",
        (pack_id, chat_id, topic_id))]
    core_add([r for r in linked if r.get("degree", 0) > 0])
    core_ids = set(core_order[:CORE_CAP])
    for eid in core_ids:
        cand[eid]["core"] = True

    ev_count, degree = _counts(db, list(cand))
    scored = []
    for eid, info in cand.items():
        row = info["row"]
        imp = _importance(ev_count.get(eid, 0), degree.get(eid, 0), row["kind"])
        rec = _recency(row["updated_ts"], row["created_ts"], now)
        score = (3.0 * info["lex"] + 2.0 * info["scope"] + 1.0 * rec
                 + 1.5 * imp + 4.0 * (1.0 if info["core"] else 0.0))
        scored.append((info["core"], score,
                       row["updated_ts"] or row["created_ts"] or 0, eid, row))
    scored.sort(key=lambda x: (not x[0], -x[1], -x[2], x[3]))
    return [f"{r['id']} | {r['kind']} | {_title_for_prompt(r['title'])} | {r['owner'] or ''} | {r['due_ts'] or ''}"
            for _, _, _, _, r in scored[:SELECT_K]]


def _actor_for(db, chat_id, msg_id):
    if chat_id is None or msg_id is None:
        return None
    row = db.conn.execute(
        "SELECT sender_name FROM messages WHERE chat_id=? AND msg_id=?",
        (chat_id, msg_id)).fetchone()
    if row is None or not row["sender_name"]:
        return None
    person = people.resolve_owner(db, row["sender_name"])
    return person["display_name"] if person else row["sender_name"]


def _apply_supersede(db, new_id: int, old_id: int, now: int, ev: dict) -> bool:
    cur = db.conn.execute(
        "UPDATE entities SET closed_ts=?, status='done', updated_ts=? "
        "WHERE id=? AND closed_ts IS NULL", (now, now, old_id))
    if cur.rowcount == 0:
        return False
    db.conn.execute(
        "INSERT INTO entity_events(entity_id, run_id, ts, op, field, to_val, "
        "chat_id, msg_id, quote, actor) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (old_id, None, now, "supersede", "superseded_by", str(new_id),
         ev.get("chat_id"), ev.get("msg_id"), ev.get("quote"),
         _actor_for(db, ev.get("chat_id"), ev.get("msg_id"))))
    return True


def _find_candidates(db, e, vecs=None) -> list[tuple[dict, float, float]]:
    rows = db.conn.execute(
        "SELECT * FROM entities WHERE pack=? AND kind=? AND closed_ts IS NULL "
        "AND id < ? AND COALESCE(project,'') = COALESCE(?,'')",
        (e["pack"], e["kind"], e["id"], e["project"])).fetchall()
    et = f"{e['title'] or ''} {e['body'] or ''}"
    ev = (vecs or {}).get(e["id"])
    scored = []
    for p in rows:
        pt = f"{p['title'] or ''} {p['body'] or ''}"
        sim = _similarity(et, pt)
        cos = 0.0
        if ev is not None:
            pv = vecs.get(p["id"])
            if pv is not None:
                cos = _cosine(ev, pv)
        if sim >= SIM_CAND or cos >= COS_CAND:
            scored.append((dict(p), sim, cos))
    scored.sort(key=lambda x: (-max(x[1], x[2]), x[0]["id"]))
    return scored[:CAND_CAP]


def _cue_hit(db, ev) -> bool:
    if ev.get("chat_id") is None or ev.get("msg_id") is None:
        return False
    row = db.conn.execute(
        "SELECT text FROM messages WHERE chat_id=? AND msg_id=?",
        (ev["chat_id"], ev["msg_id"])).fetchone()
    if row is not None and row["text"] and _CUE_RE.search(row["text"]):
        return True
    trow = db.conn.execute(
        "SELECT text FROM media_assets WHERE chat_id=? AND msg_id=?",
        (ev["chat_id"], ev["msg_id"])).fetchone()
    return trow is not None and bool(trow["text"]) and _CUE_RE.search(trow["text"]) is not None


def link(db_path=DB_PATH, days: int = 7, dry_run: bool = False, auto=None) -> dict:
    now = int(time.time())
    if auto is None:
        auto = os.environ.get("MEMORY_AUTO_SUPERSEDE", "").strip().lower() in (
            "1", "true", "yes", "on")
    result = {"ok": True, "new": 0, "candidates": 0, "auto": 0, "dry": bool(dry_run)}
    with DB(db_path) as db:
        try:
            cfg = _embed_config()
            vecs = _load_vecs_cached(db, cfg["model"]) if cfg is not None else {}
            cursor = int(db.get_meta(CURSOR_KEY) or 0)
            sql = ("SELECT ev.id AS event_id, ev.entity_id, ev.op, ev.chat_id, "
                   "ev.msg_id, ev.quote, ev.ts FROM entity_events ev "
                   "WHERE ev.id > ? AND ev.op IN ('new','update')")
            args: list = [cursor]
            if cursor == 0:
                sql += " AND ev.ts >= ?"
                args.append(now - days * 86400)
            sql += " ORDER BY ev.id LIMIT ?"
            args.append(SCAN_LIMIT)
            events = [dict(r) for r in db.conn.execute(sql, args)]
            max_id = cursor
            last_ev: dict[int, dict] = {}
            for ev in events:
                max_id = max(max_id, ev["event_id"])
                last_ev[ev["entity_id"]] = ev
            for eid, ev in last_ev.items():
                e = db.conn.execute(
                    "SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
                if e is None or e["closed_ts"] is not None:
                    continue
                for p, sim, cos in _find_candidates(db, e, vecs):
                    result["candidates"] += 1
                    score = max(sim, cos)
                    existing = db.conn.execute(
                        "SELECT id, status FROM memory_candidates "
                        "WHERE new_entity_id=? AND candidate_entity_id=?",
                        (eid, p["id"])).fetchone()
                    if existing is not None and existing["status"] in (
                            "rejected", "accepted", "auto"):
                        continue
                    do_auto = (bool(auto) and (sim >= SIM_AUTO or cos >= COS_AUTO)
                               and _cue_hit(db, ev))
                    if existing is None:
                        if not dry_run:
                            db.conn.execute(
                                "INSERT OR IGNORE INTO memory_candidates("
                                "new_entity_id, candidate_entity_id, signal, score, "
                                "quote, chat_id, msg_id, status, created_ts) "
                                "VALUES(?,?,?,?,?,?,?,?,?)",
                                (eid, p["id"], "similarity", score, ev["quote"],
                                 ev["chat_id"], ev["msg_id"], "pending", now))
                        result["new"] += 1
                        if do_auto:
                            applied = False
                            if not dry_run:
                                applied = _apply_supersede(db, eid, p["id"], now, ev)
                                if applied:
                                    db.conn.execute(
                                        "UPDATE memory_candidates SET status='auto' "
                                        "WHERE new_entity_id=? AND candidate_entity_id=?",
                                        (eid, p["id"]))
                            if applied or dry_run:
                                result["auto"] += 1
                    else:
                        if do_auto:
                            applied = False
                            if not dry_run:
                                applied = _apply_supersede(db, eid, p["id"], now, ev)
                                if applied:
                                    db.conn.execute(
                                        "UPDATE memory_candidates SET status='auto', score=? "
                                        "WHERE id=?", (score, existing["id"]))
                            if applied or dry_run:
                                result["auto"] += 1
                        elif not dry_run:
                            db.conn.execute(
                                "UPDATE memory_candidates SET score=? WHERE id=?",
                                (score, existing["id"]))
            if not dry_run:
                db.conn.commit()
                if max_id > cursor:
                    db.set_meta(CURSOR_KEY, str(max_id))
        except Exception as exc:
            db.conn.rollback()
            result["ok"] = False
            result["error"] = str(exc)
    return result


def _resolve_alias(db, eid: int) -> int:
    seen: set[int] = set()
    cur = eid
    while cur not in seen:
        seen.add(cur)
        row = db.conn.execute(
            "SELECT entity_id FROM entity_aliases WHERE old_entity_id=?", (cur,)).fetchone()
        if row is None:
            return cur
        cur = row["entity_id"]
    return cur


def _supersede_chain(db, eid: int) -> dict:
    succ = db.conn.execute(
        "SELECT to_val FROM entity_events WHERE entity_id=? AND op='supersede' "
        "AND field='superseded_by' ORDER BY ts DESC, id DESC LIMIT 1",
        (eid,)).fetchone()
    successor = None
    if succ is not None and succ["to_val"]:
        try:
            successor = _resolve_alias(db, int(succ["to_val"]))
        except (TypeError, ValueError):
            successor = None
    pred = db.conn.execute(
        "SELECT entity_id FROM entity_events WHERE op='supersede' "
        "AND field='superseded_by' AND to_val=? ORDER BY ts DESC, id DESC LIMIT 1",
        (str(eid),)).fetchone()
    predecessor = _resolve_alias(db, pred["entity_id"]) if pred is not None else None
    return {"successor": successor, "predecessor": predecessor}


def _last_event(db, eid: int):
    row = db.conn.execute(
        "SELECT * FROM entity_events WHERE entity_id=? ORDER BY ts DESC, id DESC LIMIT 1",
        (eid,)).fetchone()
    return dict(row) if row is not None else None


def _changed_since(db, eid: int, since_ts) -> bool:
    if since_ts is None:
        return False
    return db.conn.execute(
        "SELECT 1 FROM entity_events WHERE entity_id=? AND ts > ? LIMIT 1",
        (eid, since_ts)).fetchone() is not None


def recall(db, query: str, project=None, kind=None, pack=None,
           since_ts=None, limit: int = 20) -> list[dict]:
    now = int(time.time())
    where, args = _recall_filter(pack, project, kind)
    where_e, args_e = _recall_filter(pack, project, kind, "e.")
    rows = [dict(r) for r in db.conn.execute(
        f"SELECT * FROM entities WHERE {where} "
        f"ORDER BY COALESCE(updated_ts, created_ts) DESC LIMIT 1000", args)]
    have = {r["id"] for r in rows}

    fts_lex: dict[int, float] = {}
    if db.fts_entities:
        terms = _tokens(query)
        if terms:
            match = " OR ".join(t + "*" for t in terms)
            try:
                frows = list(db.conn.execute(
                    "SELECT e.id AS entity_id, bm25(entities_fts) AS rank FROM entities_fts "
                    "JOIN entities e ON e.id = entities_fts.entity_id "
                    f"WHERE entities_fts MATCH ? AND {where_e} ORDER BY rank LIMIT 200",
                    [match] + args_e))
                n = len(frows)
                for i, r in enumerate(frows):
                    fts_lex[r["entity_id"]] = 1.0 - i / max(1, n)
            except sqlite3.OperationalError:
                fts_lex = {}
    for r in _fetch_by_ids(db, where, args, [eid for eid in fts_lex if eid not in have]):
        rows.append(r)
        have.add(r["id"])

    sem: dict[int, float] = {}
    cfg = _embed_config()
    if cfg is not None:
        vecs = _load_vecs_cached(db, cfg["model"])
        if vecs:
            try:
                qv = _embed_texts([query], timeout=RECALL_EMBED_TIMEOUT)[0]
            except Exception:
                qv = None
            if qv:
                corpus = {r["id"] for r in db.conn.execute(
                    f"SELECT id FROM entities WHERE {where}", args)}
                sims = {eid: _cosine(qv, v) for eid, v in vecs.items() if eid in corpus}
                top = sorted(sims.items(), key=lambda kv: (-kv[1], kv[0]))[:SEM_TOPN]
                sem = {eid: s for eid, s in top if s > 0.0}
                extra = [eid for eid, _ in top if eid not in have]
                for r in _fetch_by_ids(db, where, args, extra):
                    rows.append(r)
                    have.add(r["id"])

    ev_count, degree = _counts(db, [r["id"] for r in rows])
    scored = []
    for r in rows:
        eid = r["id"]
        text = f"{r['title'] or ''} {r['body'] or ''}"
        lex = fts_lex.get(eid, _similarity(query, text))
        rec = _recency(r["updated_ts"], r["created_ts"], now)
        imp = _importance(ev_count.get(eid, 0), degree.get(eid, 0), r["kind"])
        scored.append((3.0 * lex + 2.0 * sem.get(eid, 0.0) + 1.0 * rec + 1.5 * imp, r))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    hits = []
    for score, r in scored[:limit]:
        eid = r["id"]
        hits.append({
            "id": eid, "pack": r["pack"], "kind": r["kind"], "title": r["title"],
            "body": r["body"], "status": r["status"], "owner": r["owner"],
            "due_ts": r["due_ts"], "project": r["project"], "closed_ts": r["closed_ts"],
            "score": score,
            "supersede": _supersede_chain(db, eid),
            "last_event": _last_event(db, eid),
            "changed_since": _changed_since(db, eid, since_ts),
        })
    return hits


def embed(db_path=DB_PATH, limit: int = 500) -> dict:
    cfg = _embed_config()
    if cfg is None:
        return {"ok": True, "skipped": True, "reason": "embeddings not configured"}
    model = cfg["model"]
    with DB(db_path) as db:
        try:
            rows = [dict(r) for r in db.conn.execute(
                "SELECT e.id, e.title, e.body, "
                "COALESCE(e.updated_ts, e.created_ts, 0) AS uts, "
                "v.updated_ts AS vts, v.model AS vmodel "
                "FROM entities e LEFT JOIN entity_vec v ON v.entity_id = e.id "
                "WHERE v.entity_id IS NULL "
                "OR v.model IS NULL OR v.model != ? "
                "OR v.updated_ts < COALESCE(e.updated_ts, e.created_ts, 0) "
                "ORDER BY e.id LIMIT ?", (model, limit))]
            if not rows:
                return {"ok": True, "embedded": 0, "model": model}
            now = int(time.time())
            n = 0
            for i in range(0, len(rows), EMBED_BATCH):
                batch = rows[i:i + EMBED_BATCH]
                texts = [f"{r['title'] or ''}\n{r['body'] or ''}" for r in batch]
                vecs = _embed_texts(texts)
                if not isinstance(vecs, list) or len(vecs) != len(batch):
                    got = len(vecs) if isinstance(vecs, list) else None
                    return {"ok": False, "error": "embedding response size mismatch",
                            "expected": len(batch), "got": got}
                for r, vec in zip(batch, vecs):
                    if not vec:
                        continue
                    db.conn.execute(
                        "INSERT OR REPLACE INTO entity_vec("
                        "entity_id, model, dim, vec, updated_ts) VALUES(?,?,?,?,?)",
                        (r["id"], model, len(vec), _pack_vec(vec), now))
                    n += 1
            db.conn.commit()
            return {"ok": True, "embedded": n, "model": model}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def rebuild(db_path=DB_PATH) -> dict:
    with DB(db_path) as db:
        try:
            db.conn.execute("DELETE FROM entities_fts")
        except sqlite3.OperationalError:
            db.conn.executescript(ENTITIES_FTS_SCHEMA)
        db.conn.execute(
            "INSERT INTO entities_fts(rowid, entity_id, title, body) "
            "SELECT id, id, title, COALESCE(body, '') FROM entities")
        n = db.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        db.conn.commit()
        return {"ok": True, "indexed": n}


def gc(db_path=DB_PATH) -> dict:
    with DB(db_path) as db:
        cand = db.conn.execute(
            "DELETE FROM memory_candidates WHERE "
            "new_entity_id NOT IN (SELECT id FROM entities) OR "
            "candidate_entity_id NOT IN (SELECT id FROM entities)").rowcount
        stale = db.conn.execute(
            "UPDATE memory_candidates SET status='stale' WHERE status='pending' AND ("
            "new_entity_id NOT IN (SELECT id FROM entities) OR "
            "candidate_entity_id NOT IN (SELECT id FROM entities) OR "
            "new_entity_id IN (SELECT id FROM entities WHERE closed_ts IS NOT NULL) OR "
            "candidate_entity_id IN (SELECT id FROM entities WHERE closed_ts IS NOT NULL))"
        ).rowcount
        try:
            vec = db.conn.execute(
                "DELETE FROM entity_vec WHERE entity_id NOT IN (SELECT id FROM entities)"
            ).rowcount
        except sqlite3.OperationalError:
            vec = 0
        db.conn.commit()
        return {"ok": True, "candidates": cand, "vectors": vec, "stale": stale}


def _arg_value(args: list[str], name: str):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _accept(db, cand_id: int, now: int) -> dict:
    row = db.conn.execute(
        "SELECT * FROM memory_candidates WHERE id=?", (cand_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "candidate not found"}
    ev = {"chat_id": row["chat_id"], "msg_id": row["msg_id"], "quote": row["quote"]}
    applied = _apply_supersede(db, row["new_entity_id"], row["candidate_entity_id"], now, ev)
    if not applied:
        return {"ok": True, "id": cand_id, "status": "pending", "applied": False}
    db.conn.execute(
        "UPDATE memory_candidates SET status='accepted', decided_ts=?, decided_by=? "
        "WHERE id=?", (now, "review", cand_id))
    db.conn.commit()
    return {"ok": True, "id": cand_id, "status": "accepted", "applied": True}


def _review(db_path, rest: list[str]) -> dict:
    status = _arg_value(rest, "--status") or "pending"
    accept = _arg_value(rest, "--accept")
    reject = _arg_value(rest, "--reject")
    now = int(time.time())
    with DB(db_path) as db:
        if accept is not None:
            try:
                cand_id = int(accept)
            except ValueError:
                return {"ok": False, "error": f"invalid --accept: {accept}"}
            return _accept(db, cand_id, now)
        if reject is not None:
            try:
                cand_id = int(reject)
            except ValueError:
                return {"ok": False, "error": f"invalid --reject: {reject}"}
            cur = db.conn.execute(
                "UPDATE memory_candidates SET status='rejected', decided_ts=?, "
                "decided_by=? WHERE id=?", (now, "review", cand_id))
            db.conn.commit()
            if cur.rowcount == 0:
                return {"ok": False, "error": "candidate not found"}
            return {"ok": True, "id": cand_id, "status": "rejected"}
        rows = [dict(r) for r in db.conn.execute(
            "SELECT * FROM memory_candidates WHERE status=? ORDER BY score DESC, id",
            (status,))]
        return {"ok": True, "status": status, "count": len(rows), "items": rows}


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    db_path = DB_PATH
    if "--db" in args:
        i = args.index("--db")
        if i + 1 < len(args):
            db_path = args[i + 1]
        del args[i:i + 2]
    cmd = args[0] if args else "link"
    rest = args[1:]
    if cmd == "link":
        res = link(db_path, dry_run="--apply" not in rest)
    elif cmd == "rebuild":
        res = rebuild(db_path)
    elif cmd == "gc":
        res = gc(db_path)
    elif cmd == "embed":
        lim = _arg_value(rest, "--limit")
        if lim is None:
            res = embed(db_path, limit=500)
        else:
            try:
                limit = int(lim)
            except ValueError:
                res = {"ok": False, "error": f"invalid --limit: {lim}"}
            else:
                res = embed(db_path, limit=limit)
    elif cmd == "review":
        res = _review(db_path, rest)
    else:
        res = {"ok": False, "error": f"unknown command: {cmd}"}
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
