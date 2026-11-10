"""Выбор источников для заданий: watch-лист, папка, подборка, весь индекс.

Подборка (collection) — сохранённая группа каналов для повторяемых заданий.
Она не меняет watch-лист и не открывает агенту доступ к чатам: это снимок
для планирования, а не разрешение.

Спека источников — строка через запятую:
    watch | all | folder:<id> | collection:<имя> | chat:<id | @username>
Повторы схлопываются, итог сортирован по chat_id.

`folder:<id>` резолвится в разрезе аккаунта (`resolve(..., account=...)`):
id папки account-локальный, поэтому один номер в разных аккаунтах — разные папки.
"""

from __future__ import annotations

import time

from db import DB


def collection_list(db: DB) -> list[dict]:
    rows = db.conn.execute(
        "SELECT c.id, c.name, c.created_ts, fc.chat_id, ch.title "
        "FROM collections c "
        "LEFT JOIN collection_chats fc ON fc.collection_id=c.id "
        "LEFT JOIN chats ch ON ch.id=fc.chat_id "
        "ORDER BY c.name, fc.chat_id").fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        c = out.setdefault(r["id"], {"id": r["id"], "name": r["name"],
                                     "created_ts": r["created_ts"], "chats": []})
        if r["chat_id"] is not None:
            c["chats"].append({"chat_id": r["chat_id"], "title": r["title"]})
    return list(out.values())


def collection_save(db: DB, name: str, chat_ids: list[int]) -> dict:
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise ValueError("имя подборки: 1–64 символа")
    ids = sorted({int(c) for c in chat_ids})
    db.conn.execute("DELETE FROM collections WHERE name=?", (name,))
    cur = db.conn.execute(
        "INSERT INTO collections(name, created_ts) VALUES(?,?)",
        (name, int(time.time())))
    cid = int(cur.lastrowid)
    db.conn.executemany(
        "INSERT INTO collection_chats(collection_id, chat_id) VALUES(?,?)",
        [(cid, c) for c in ids])
    db.conn.commit()
    return {"id": cid, "name": name, "chats": ids}


def collection_delete(db: DB, name: str) -> bool:
    row = db.conn.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
    if row is None:
        return False
    db.conn.execute("DELETE FROM collection_chats WHERE collection_id=?", (row["id"],))
    db.conn.execute("DELETE FROM collections WHERE id=?", (row["id"],))
    db.conn.commit()
    return True


def _chat_ref(ref: str) -> int | None:
    try:
        return int(ref)
    except ValueError:
        return None


def resolve(db: DB, spec: str, account: str = "default") -> list[dict]:
    """Спека источников -> [{chat_id, title}]. Неизвестный токен — ValueError."""
    out: dict[int, dict] = {}
    for tok in (spec or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok == "watch":
            rows = db.conn.execute(
                "SELECT DISTINCT w.chat_id, c.title FROM watch w "
                "LEFT JOIN chats c ON c.id=w.chat_id").fetchall()
        elif tok == "all":
            rows = db.conn.execute("SELECT id AS chat_id, title FROM chats").fetchall()
        elif tok.startswith("folder:"):
            raw = tok[len("folder:"):].strip()
            try:
                fid = int(raw)
            except ValueError:
                raise ValueError(f"folder принимает числовой id: {tok!r}") from None
            rows = db.conn.execute(
                "SELECT fc.chat_id, c.title FROM folder_chats fc "
                "LEFT JOIN chats c ON c.id=fc.chat_id "
                "WHERE fc.folder_id=? AND fc.account_id=?",
                (fid, account)).fetchall()
        elif tok.startswith("collection:"):
            name = tok[len("collection:"):].strip()
            rows = db.conn.execute(
                "SELECT fc.chat_id, c.title FROM collection_chats fc "
                "JOIN collections co ON co.id=fc.collection_id "
                "LEFT JOIN chats c ON c.id=fc.chat_id WHERE co.name=?", (name,)).fetchall()
            if not rows:
                raise ValueError(f"подборка не найдена: {name!r}")
        elif tok.startswith("chat:"):
            ref = tok[len("chat:"):].strip().lstrip("@")
            rows = db.conn.execute(
                "SELECT id AS chat_id, title FROM chats WHERE id=? OR username=?",
                (_chat_ref(ref), ref)).fetchall()
            if not rows:
                raise ValueError(f"чат не найден в индексе: {ref!r}")
        else:
            raise ValueError(
                f"неизвестный источник: {tok!r} (watch | all | folder:<id> | "
                "collection:<имя> | chat:<id|@username>)")
        for r in rows:
            out.setdefault(r["chat_id"], {"chat_id": r["chat_id"], "title": r["title"]})
    return sorted(out.values(), key=lambda r: r["chat_id"])
