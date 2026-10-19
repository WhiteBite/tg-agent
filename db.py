"""Локальный индекс Telegram-групп для офлайн-анализа.

Одна SQLite-база, три таблицы: chats / topics / messages.
Её пишут `sync.py` (Telethon -> SQLite), а читают `server.py` (MCP) и `sql.py`.

Всё read-only: здесь нет ни одной функции, которая что-то отправляет в Telegram.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from sanitize import sanitize_name, sanitize_user_content
from redact import redact


def clean_text(text: Optional[str], max_chars: int = 4096) -> Optional[str]:
    """redact (secrets) + sanitize (control/invisible chars) — in this order:
    сначала маскируем, потом обрезаем, чтобы обрезка не оголила хвост секрета."""
    if not text:
        return text
    masked, _ = redact(text)
    return sanitize_user_content(masked, max_chars)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY,
    title       TEXT,
    username    TEXT,
    kind        TEXT,
    is_forum    INTEGER DEFAULT 0,
    members     INTEGER,
    synced_at   INTEGER
);

CREATE TABLE IF NOT EXISTS topics (
    chat_id     INTEGER NOT NULL,
    topic_id    INTEGER NOT NULL,
    title       TEXT,
    closed      INTEGER DEFAULT 0,
    messages    INTEGER DEFAULT 0,
    last_ts     INTEGER,
    PRIMARY KEY (chat_id, topic_id)
);

CREATE TABLE IF NOT EXISTS messages (
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    topic_id    INTEGER NOT NULL DEFAULT 1,
    ts          INTEGER,
    date_iso    TEXT,
    sender_id   INTEGER,
    sender_name TEXT,
    is_out      INTEGER DEFAULT 0,
    is_service  INTEGER DEFAULT 0,
    text        TEXT,
    media       TEXT,
    reply_to    INTEGER,
    views       INTEGER,
    forwards    INTEGER,
    reactions   TEXT,
    raw_json    TEXT,
    edited_ts   INTEGER,
    deleted     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE INDEX IF NOT EXISTS ix_msg_chat_ts      ON messages (chat_id, ts);
CREATE INDEX IF NOT EXISTS ix_msg_chat_topic   ON messages (chat_id, topic_id, ts);
CREATE INDEX IF NOT EXISTS ix_msg_sender       ON messages (chat_id, sender_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- то, за чем агент реально следит: конкретные темы в конкретных группах
CREATE TABLE IF NOT EXISTS watch (
    chat_id     INTEGER NOT NULL,
    topic_id    INTEGER NOT NULL,
    alias       TEXT,
    pinned      INTEGER DEFAULT 0,
    last_read   INTEGER DEFAULT 0,
    added_at    INTEGER,
    project     TEXT,
    PRIMARY KEY (chat_id, topic_id)
);

-- ссылки/упоминания/инвайты, вынутые из сообщений при синхронизации
-- (идея и regexp — из 0xMRK95/telegram-mcp-server, инструмент extract_refs)
CREATE TABLE IF NOT EXISTS refs (
    chat_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    kind    TEXT NOT NULL,          -- mention | invite | url | forward
    value   TEXT NOT NULL,
    PRIMARY KEY (chat_id, msg_id, kind, value)
);
CREATE INDEX IF NOT EXISTS ix_refs_chat ON refs (chat_id, kind);

-- папки Telegram (состав лежит прямо в объекте папки)
CREATE TABLE IF NOT EXISTS folders (
    folder_id INTEGER PRIMARY KEY,
    title     TEXT,
    synced_at INTEGER
);
CREATE TABLE IF NOT EXISTS folder_chats (
    folder_id INTEGER NOT NULL,
    chat_id   INTEGER NOT NULL,
    PRIMARY KEY (folder_id, chat_id)
);

-- сущности, извлечённые из переписки: сами сущности, их события и прогоны экстрактора
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pack        TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    topic_id    INTEGER NOT NULL DEFAULT 1,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    owner       TEXT,
    due_ts      INTEGER,
    project     TEXT,
    confidence  REAL,
    attrs_json  TEXT,
    dedup_key   TEXT,
    created_ts  INTEGER,
    updated_ts  INTEGER,
    closed_ts   INTEGER
);

CREATE TABLE IF NOT EXISTS entity_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id   INTEGER NOT NULL,
    run_id      INTEGER,
    ts          INTEGER NOT NULL,
    op          TEXT NOT NULL,
    field       TEXT,
    from_val    TEXT,
    to_val      TEXT,
    chat_id     INTEGER,
    msg_id      INTEGER,
    quote       TEXT
);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pack          TEXT NOT NULL,
    chat_id       INTEGER NOT NULL,
    topic_id      INTEGER NOT NULL,
    from_msg      INTEGER,
    upto_msg      INTEGER,
    ts            INTEGER,
    ops_applied   INTEGER DEFAULT 0,
    ops_rejected  INTEGER DEFAULT 0,
    ops_overflow  INTEGER DEFAULT 0,
    rejects_json  TEXT,
    status        TEXT,
    duration_ms   INTEGER,
    finish_reason TEXT,
    usage_prompt  INTEGER,
    usage_completion INTEGER
);

CREATE INDEX IF NOT EXISTS ix_ent_scope   ON entities (pack, chat_id, topic_id, status);
CREATE INDEX IF NOT EXISTS ix_ent_overdue ON entities (due_ts) WHERE closed_ts IS NULL AND due_ts IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_ent_project ON entities (pack, project, status);
CREATE INDEX IF NOT EXISTS ix_ev_entity   ON entity_events (entity_id, ts);
CREATE INDEX IF NOT EXISTS ix_ev_msg      ON entity_events (chat_id, msg_id);

-- дайджесты: LLM-выжимки новых сообщений по watch-листу
CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    topic_id    INTEGER,
    ts          INTEGER,
    since_msg   INTEGER,
    upto_msg    INTEGER,
    summary     TEXT
);

-- закреплённые сообщения («readme» темы/группы)
CREATE TABLE IF NOT EXISTS pins (
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    topic_id    INTEGER,
    ts          INTEGER,
    sender_name TEXT,
    text        TEXT,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS media_assets (
    chat_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    status  TEXT NOT NULL DEFAULT 'pending',
    text    TEXT,
    engine  TEXT,
    ts      INTEGER,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS integrations (
    entity_id   INTEGER NOT NULL,
    system      TEXT NOT NULL,
    external_id TEXT,
    state_json  TEXT,
    synced_ts   INTEGER,
    PRIMARY KEY (entity_id, system)
);

-- сущность может происходить из нескольких (chat, topic): дедуп глобальный по (pack, dedup_key)
CREATE TABLE IF NOT EXISTS entity_sources (
    entity_id     INTEGER NOT NULL,
    chat_id       INTEGER NOT NULL,
    topic_id      INTEGER NOT NULL DEFAULT 1,
    first_seen_ts INTEGER,
    last_seen_ts  INTEGER,
    PRIMARY KEY (entity_id, chat_id, topic_id)
);
CREATE INDEX IF NOT EXISTS ix_entsrc_scope ON entity_sources (chat_id, topic_id);

CREATE TABLE IF NOT EXISTS entity_aliases (
    old_entity_id INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL,
    merged_ts     INTEGER,
    reason        TEXT
);
"""

# Полнотекстовый поиск по истории (года переписки через LIKE искать долго).
# Если SQLite собран без FTS5, таблица просто не создастся — поиск откатится на LIKE.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    chat_id UNINDEXED, topic_id UNINDEXED, text,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS trg_msg_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, chat_id, topic_id, text)
    VALUES (new.rowid, new.chat_id, new.topic_id, COALESCE(new.text, ''));
END;
-- messages_fts — обычная fts5 (не contentless): чистим прямым DELETE, а не ('delete', ...)
CREATE TRIGGER IF NOT EXISTS trg_msg_ad AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.rowid;
END;
-- OF text: mark_deleted (UPDATE deleted=1) не должен возвращать текст в FTS
CREATE TRIGGER IF NOT EXISTS trg_msg_au AFTER UPDATE OF text ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.rowid;
    INSERT INTO messages_fts(rowid, chat_id, topic_id, text)
    VALUES (new.rowid, new.chat_id, new.topic_id, COALESCE(new.text, ''));
END;
"""

SCHEMA_VERSION = 3


class DB:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            self.conn = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA query_only=ON")
            self.fts = self._probe_fts()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")  # live.py + sync.py + ui.py могут писать одновременно
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            self.fts = self._probe_fts()
            return
        self.conn.executescript(SCHEMA)
        try:
            # IF NOT EXISTS не обновляет триггер в старых базах — дропаем принудительно
            self.conn.execute("DROP TRIGGER IF EXISTS trg_msg_ad")
            self.conn.execute("DROP TRIGGER IF EXISTS trg_msg_au")
            self.conn.executescript(FTS_SCHEMA)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False  # поиск уедет на LIKE
        # свежая база тоже приходит с user_version=0 — миграции идемпотентны
        if version < 1:
            self._migrate_message_edits()
            self._migrate_marked_ids()
            self._migrate_global_dedup()
            self._migrate_watch_project()
            self._migrate_entity_status()
            self._migrate_topic_ids()
            self._migrate_ops_overflow()
            self._migrate_extract_telemetry()
        if version < 2:
            self._migrate_v2_columns()
        if version < 3:
            self._migrate_status_done()
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "DB":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _probe_fts(self) -> bool:
        try:
            self.conn.execute("SELECT 1 FROM messages_fts LIMIT 1")
        except sqlite3.OperationalError:
            return False
        return True

    # таблицы, где chat_id нужно перевести при миграции на marked-форму
    _MIGRATE_TABLES = (
        ("messages", "chat_id, msg_id, topic_id, ts, date_iso, sender_id, sender_name, "
                     "is_out, is_service, text, media, reply_to, views, forwards, "
                     "reactions, raw_json, edited_ts, deleted"),
        ("topics", "chat_id, topic_id, title, closed, messages, last_ts"),
        ("watch", "chat_id, topic_id, alias, pinned, last_read, added_at, project"),
        ("refs", "chat_id, msg_id, kind, value"),
        ("folder_chats", "folder_id, chat_id"),
        ("pins", "chat_id, msg_id, topic_id, ts, sender_name, text"),
        ("entities", "pack, chat_id, topic_id, kind, title, body, status, owner, due_ts, "
                     "project, confidence, attrs_json, dedup_key, created_ts, updated_ts, closed_ts"),
        ("entity_events", "entity_id, run_id, ts, op, field, from_val, to_val, chat_id, msg_id, quote"),
        ("media_assets", "chat_id, msg_id, kind, status, text, engine, ts"),
    )

    def _migrate_message_edits(self) -> None:
        for ddl in (
                "ALTER TABLE messages ADD COLUMN edited_ts INTEGER",
                "ALTER TABLE messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

    def _migrate_marked_ids(self) -> None:
        """До 0.3.1 чаты хранились с сырыми положительными id Telethon; переносим
        их на marked-форму, иначе после апгрейда watch/курсоры/live осиротеют,
        а история задвоится под двумя id."""
        rows = self.conn.execute(
            "SELECT id, kind FROM chats WHERE id > 0 "
            "AND kind IN ('channel', 'supergroup', 'group')").fetchall()
        if not rows:
            return
        mapping = {
            r["id"]: (-1000000000000 - r["id"]
                      if r["kind"] in ("channel", "supergroup") else -r["id"])
            for r in rows
        }
        self.conn.execute("CREATE TEMP TABLE id_map(old_id INTEGER PRIMARY KEY, new_id INTEGER)")
        self.conn.executemany("INSERT INTO id_map VALUES(?, ?)", mapping.items())
        self.conn.execute(
            "INSERT OR IGNORE INTO chats(id, title, username, kind, is_forum, members, synced_at) "
            "SELECT new_id, c.title, c.username, c.kind, c.is_forum, c.members, c.synced_at "
            "FROM chats c JOIN id_map ON id_map.old_id = c.id")
        self.conn.execute("DELETE FROM chats WHERE id IN (SELECT old_id FROM id_map)")
        for table, cols in self._MIGRATE_TABLES:
            sel = ", ".join(
                "(SELECT new_id FROM id_map WHERE old_id = t.chat_id)" if c.strip() == "chat_id"
                else f"t.{c.strip()}"
                for c in cols.split(","))
            self.conn.execute(
                f"INSERT OR IGNORE INTO {table}({cols}) SELECT {sel} FROM {table} t "
                f"WHERE t.chat_id IN (SELECT old_id FROM id_map)")
            self.conn.execute(f"DELETE FROM {table} WHERE chat_id IN (SELECT old_id FROM id_map)")
        self.conn.execute(
            "INSERT INTO digests(chat_id, topic_id, ts, since_msg, upto_msg, summary) "
            "SELECT (SELECT new_id FROM id_map WHERE old_id = d.chat_id), d.topic_id, d.ts, "
            "d.since_msg, d.upto_msg, d.summary FROM digests d "
            "WHERE d.chat_id IN (SELECT old_id FROM id_map)")
        self.conn.execute("DELETE FROM digests WHERE chat_id IN (SELECT old_id FROM id_map)")
        for r in self.conn.execute("SELECT key FROM meta").fetchall():
            parts = r["key"].split(":")
            if len(parts) >= 2 and parts[0] in ("cursor", "backfill", "digest_upto"):
                try:
                    old = int(parts[1])
                except ValueError:
                    continue
                if old in mapping:
                    new_key = ":".join([parts[0], str(mapping[old])] + parts[2:])
                    self.conn.execute("UPDATE meta SET key=? WHERE key=?", (new_key, r["key"]))
        self.conn.commit()
        self.conn.execute("DROP TABLE id_map")

    def _migrate_global_dedup(self) -> None:
        # признак непроведённой миграции — старый scope-local индекс; открывается БД часто, гейт обязан быть дешёвым
        old = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_ent_dedup'"
        ).fetchone()
        if old is not None:
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("PRAGMA busy_timeout=60000")
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT OR IGNORE INTO entity_sources(entity_id, chat_id, topic_id, "
                "first_seen_ts, last_seen_ts) "
                "SELECT id, chat_id, topic_id, created_ts, COALESCE(updated_ts, created_ts) "
                "FROM entities")
            clusters = self.conn.execute(
                "SELECT pack, dedup_key FROM entities WHERE closed_ts IS NULL "
                "AND dedup_key IS NOT NULL GROUP BY pack, dedup_key HAVING COUNT(*) > 1"
            ).fetchall()
            now = int(time.time())
            for c in clusters:
                rows = self.conn.execute(
                    "SELECT id FROM entities WHERE pack=? AND dedup_key=? "
                    "AND closed_ts IS NULL ORDER BY created_ts, id",
                    (c["pack"], c["dedup_key"])).fetchall()
                self.merge_entities(rows[0]["id"], [r["id"] for r in rows[1:]],
                                    "exact", now)
            self.conn.execute("DROP INDEX ux_ent_dedup")
            self.conn.commit()
            self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ent_dedup_global "
            "ON entities(pack, dedup_key) WHERE closed_ts IS NULL")

    def _migrate_watch_project(self) -> None:
        try:
            self.conn.execute("ALTER TABLE watch ADD COLUMN project TEXT")
        except sqlite3.OperationalError:
            pass

    def _migrate_entity_status(self) -> None:
        self.conn.execute(
            "UPDATE entities SET status='closed' "
            "WHERE closed_ts IS NOT NULL AND status <> 'closed'")

    def _migrate_status_done(self) -> None:
        self.conn.execute("UPDATE entities SET status='done' WHERE status='closed'")

    def _migrate_topic_ids(self) -> None:
        """topic_id NOT NULL: старые базы писали NULL для «без темы» — это тот же
        General (1). Индекс ix_msg_chat_topic используется только прямым
        сравнением topic_id = ?, COALESCE его обходит."""
        self.conn.execute("UPDATE messages SET topic_id = 1 WHERE topic_id IS NULL")

    def _migrate_ops_overflow(self) -> None:
        try:
            self.conn.execute("ALTER TABLE extraction_runs ADD COLUMN ops_overflow INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

    def _migrate_extract_telemetry(self) -> None:
        for ddl in (
                "ALTER TABLE extraction_runs ADD COLUMN duration_ms INTEGER",
                "ALTER TABLE extraction_runs ADD COLUMN finish_reason TEXT",
                "ALTER TABLE extraction_runs ADD COLUMN usage_prompt INTEGER",
                "ALTER TABLE extraction_runs ADD COLUMN usage_completion INTEGER"):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

    def _migrate_v2_columns(self) -> None:
        for ddl in (
                "ALTER TABLE extraction_runs ADD COLUMN rejects_json TEXT",
                "ALTER TABLE integrations ADD COLUMN external_id TEXT"):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

    # ---------------- cursors (для инкрементальной синхронизации) ----------------

    def get_cursor(self, chat_id: int, topic_id: int = 0) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"cursor:{chat_id}:{topic_id}",)
        ).fetchone()
        return int(row["value"]) if row else 0

    def set_cursor(self, chat_id: int, topic_id: int, msg_id: int) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"cursor:{chat_id}:{topic_id}", str(msg_id)),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---------------- папки (пишет sync.py, читает ui) ----------------

    def save_folders(self, folders: list[dict]) -> None:
        for f in folders:
            self.conn.execute(
                "INSERT INTO folders(folder_id, title, synced_at) VALUES(?,?,?) "
                "ON CONFLICT(folder_id) DO UPDATE SET title=excluded.title, "
                "synced_at=excluded.synced_at",
                (f["id"], f["title"], int(time.time())),
            )
            self.conn.execute("DELETE FROM folder_chats WHERE folder_id=?", (f["id"],))
            self.conn.executemany(
                "INSERT OR IGNORE INTO folder_chats(folder_id, chat_id) VALUES(?,?)",
                [(f["id"], cid) for cid in f["peers"]],
            )
        self.conn.commit()

    def folders_list(self) -> list[dict]:
        out = []
        for f in self.conn.execute(
            "SELECT folder_id, title FROM folders ORDER BY title COLLATE NOCASE"
        ):
            chats = [
                r["chat_id"]
                for r in self.conn.execute(
                    "SELECT chat_id FROM folder_chats WHERE folder_id=?", (f["folder_id"],)
                )
            ]
            out.append({"id": f["folder_id"], "title": f["title"], "chat_ids": chats})
        return out

    # ---------------- refs / pins (пишет sync.py, читают server/ui) ----------------

    def insert_refs(self, chat_id: int, msg_id: int, refs: list[tuple[str, str]]) -> None:
        if not refs:
            return
        self.conn.executemany(
            "INSERT OR IGNORE INTO refs(chat_id, msg_id, kind, value) VALUES(?,?,?,?)",
            [(chat_id, msg_id, k, v) for k, v in refs],
        )

    def refs_summary(
        self,
        chat_id: Optional[int] = None,
        chat_ids: Optional[list[int]] = None,
        since_ts: Optional[int] = None,
        limit: int = 25,
    ) -> dict[str, list[dict]]:
        """Топ ссылок/упоминаний по чату или набору чатов (для watch-листа)."""
        sql = """
            SELECT r.kind, r.value, COUNT(*) n, MAX(m.ts) last_ts
            FROM refs r LEFT JOIN messages m ON m.chat_id = r.chat_id AND m.msg_id = r.msg_id
            WHERE 1=1
        """
        args: list[Any] = []
        if chat_id is not None:
            sql += " AND r.chat_id = ?"
            args.append(chat_id)
        elif chat_ids:
            sql += f" AND r.chat_id IN ({','.join('?' * len(chat_ids))})"
            args.extend(chat_ids)
        if since_ts:
            sql += " AND m.ts >= ?"
            args.append(since_ts)
        sql += " GROUP BY r.kind, r.value ORDER BY n DESC"
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["kind"], [])
            if len(out[r["kind"]]) < limit:
                out[r["kind"]].append(r)
        return out

    def digests_list(self, chat_id: Optional[int] = None,
                     topic_id: Optional[int] = None, limit: int = 10) -> list[dict]:
        sql = (
            "SELECT d.*, w.alias FROM digests d LEFT JOIN watch w "
            "  ON w.chat_id = d.chat_id "
            " AND COALESCE(w.topic_id, 1) = COALESCE(d.topic_id, 1) WHERE 1=1"
        )
        args: list[Any] = []
        if chat_id is not None:
            sql += " AND d.chat_id = ?"
            args.append(chat_id)
        if topic_id is not None:
            sql += " AND COALESCE(d.topic_id, 1) = ?"
            args.append(topic_id)
        sql += " ORDER BY d.id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def insert_pin(self, chat_id: int, msg_id: int, topic_id: Optional[int],
                   ts: Optional[int], sender_name: Optional[str], text: Optional[str]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO pins(chat_id, msg_id, topic_id, ts, sender_name, text) "
            "VALUES(?,?,?,?,?,?)",
            (chat_id, msg_id, topic_id, ts, sanitize_name(sender_name) if sender_name else None,
             clean_text(text) if text else None),
        )

    def pins(self, chat_id: int, topic_id: Optional[int] = None, limit: int = 5) -> list[dict]:
        sql = "SELECT * FROM pins WHERE chat_id = ?"
        args: list[Any] = [chat_id]
        if topic_id is not None:
            sql += " AND COALESCE(topic_id,1) = ?"
            args.append(1 if topic_id in (0, 1) else topic_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    # ---------------- write (использует только sync.py) ----------------

    def upsert_chat(
        self,
        chat_id: int,
        title: str,
        username: Optional[str],
        kind: str,
        is_forum: bool,
        members: Optional[int],
    ) -> None:
        self.conn.execute(
            "INSERT INTO chats(id, title, username, kind, is_forum, members, synced_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username, "
            "kind=excluded.kind, is_forum=excluded.is_forum, members=excluded.members, "
            "synced_at=excluded.synced_at",
            (chat_id, title, username, kind, int(is_forum), members, int(time.time())),
        )

    def upsert_topic(
        self,
        chat_id: int,
        topic_id: int,
        title: str,
        closed: bool = False,
        messages: Optional[int] = None,
        last_ts: Optional[int] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO topics(chat_id, topic_id, title, closed, messages, last_ts) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(chat_id, topic_id) DO UPDATE SET title=excluded.title, "
            "closed=excluded.closed, "
            "messages=COALESCE(excluded.messages, topics.messages), "
            "last_ts=COALESCE(excluded.last_ts, topics.last_ts)",
            (chat_id, topic_id, title, int(closed), messages, last_ts),
        )

    _MSG_COLS = ("chat_id, msg_id, topic_id, ts, date_iso, "
                 "sender_id, sender_name, is_out, is_service, text, media, reply_to, "
                 "views, forwards, reactions, raw_json, edited_ts, deleted")

    @staticmethod
    def _msg_values(msg: dict[str, Any]) -> tuple:
        return (
            msg["chat_id"],
            msg["msg_id"],
            int(msg.get("topic_id") or 1),
            msg.get("ts"),
            msg.get("date_iso"),
            msg.get("sender_id"),
            msg.get("sender_name"),
            int(msg.get("is_out") or 0),
            int(msg.get("is_service") or 0),
            msg.get("text"),
            msg.get("media"),
            msg.get("reply_to"),
            msg.get("views"),
            msg.get("forwards"),
            json.dumps(msg.get("reactions") or [], ensure_ascii=False),
            json.dumps(msg, ensure_ascii=False, default=str),
            msg.get("edited_ts"),
            int(msg.get("deleted") or 0),
        )

    def insert_message(self, msg: dict[str, Any]) -> bool:
        """Вставляет сообщение; возвращает False, если оно уже было."""
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO messages({self._MSG_COLS}) "
            f"VALUES({','.join('?' * 18)})",
            self._msg_values(msg),
        )
        if cur.rowcount:
            self.conn.commit()
            return True
        return False

    def insert_messages(self, rows: list[dict[str, Any]]) -> int:
        """Батч-вставка одним коммитом на чанк; возвращает число новых строк."""
        if not rows:
            return 0
        cur = self.conn.executemany(
            f"INSERT OR IGNORE INTO messages({self._MSG_COLS}) "
            f"VALUES({','.join('?' * 18)})",
            [self._msg_values(r) for r in rows],
        )
        return cur.rowcount

    def update_message(self, msg: dict[str, Any]) -> bool:
        """Обновляет правку сообщения; False, если строки нет или правка не новее
        сохранённой (повтор того же события/ретрай скана — идемпотентно)."""
        ets = msg.get("edited_ts")
        cur = self.conn.execute(
            "UPDATE messages SET text=?, media=?, reactions=?, views=?, forwards=?, "
            "edited_ts=?, raw_json=? "
            "WHERE chat_id=? AND msg_id=? AND (edited_ts IS NULL OR edited_ts < ?)",
            (msg.get("text"), msg.get("media"),
             json.dumps(msg.get("reactions") or [], ensure_ascii=False),
             msg.get("views"), msg.get("forwards"), ets,
             json.dumps(msg, ensure_ascii=False, default=str),
             msg["chat_id"], msg["msg_id"], ets),
        )
        if cur.rowcount:
            self._record_dirty(msg["chat_id"], msg.get("topic_id"), msg["msg_id"])
            self.conn.commit()
            return True
        return False

    def update_reactions(self, chat_id: int, msg_id: int, items: list[dict]) -> bool:
        """Снапшот реакций: [ {"emoji": str, "count": int}, ... ]. False, если
        строки нет. Текст не меняется — FTS и dirty не затрагиваются."""
        cur = self.conn.execute(
            "UPDATE messages SET reactions=? WHERE chat_id=? AND msg_id=?",
            (json.dumps(items, ensure_ascii=False), chat_id, msg_id),
        )
        if cur.rowcount:
            self.conn.commit()
            return True
        return False

    def mark_deleted(self, chat_id: int, msg_id: int) -> bool:
        row = self.conn.execute(
            "SELECT topic_id FROM messages WHERE chat_id=? AND msg_id=?",
            (chat_id, msg_id),
        ).fetchone()
        if row is None:
            return False
        cur = self.conn.execute(
            "UPDATE messages SET deleted=1 WHERE chat_id=? AND msg_id=? AND deleted=0",
            (chat_id, msg_id),
        )
        if not cur.rowcount:
            return False
        if self.fts:
            self.conn.execute(
                "DELETE FROM messages_fts WHERE rowid = "
                "(SELECT rowid FROM messages WHERE chat_id=? AND msg_id=?)",
                (chat_id, msg_id),
            )
        self._record_dirty(chat_id, row["topic_id"], msg_id)
        self.conn.commit()
        return True

    def _record_dirty(self, chat_id: int, topic_id: Optional[int], msg_id: int) -> None:
        # extract.py читает и гасит ключ dirty:{chat}:{topic} после перепрогона
        if topic_id is None:
            return
        topic_id = 1 if topic_id in (0, 1) else topic_id
        key = f"dirty:{chat_id}:{topic_id}"
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        ids: list[int] = []
        if row is not None and row["value"]:
            try:
                ids = [int(i) for i in json.loads(row["value"])]
            except (TypeError, ValueError):
                ids = []
        ids = [i for i in ids if i != msg_id]
        ids.append(msg_id)
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(ids[-200:])),
        )

    def set_topic_counters(self, chat_id: int, topic_id: int, messages: int, last_ts: Optional[int]) -> None:
        self.conn.execute(
            "UPDATE topics SET messages=?, last_ts=? WHERE chat_id=? AND topic_id=?",
            (messages, last_ts, chat_id, topic_id),
        )
        self.conn.commit()

    def recount_topics(self, chat_id: int, general_id: int = 1) -> None:
        """Пересчитывает счётчики тем по фактическим сообщениям.

        general_id=1 всегда: в форуме это General (id 1), в группе без тем все
        сообщения живут в той же псевдотеме 1 — watch-слой считает 0 и 1 одним.
        """
        self.conn.execute(
            """
            INSERT INTO topics(chat_id, topic_id, title, closed, messages, last_ts)
            SELECT chat_id, topic_id, 'General', 0, COUNT(*), MAX(ts)
            FROM messages WHERE chat_id = ?
            GROUP BY chat_id, topic_id
            ON CONFLICT(chat_id, topic_id) DO UPDATE SET
                messages = excluded.messages, last_ts = excluded.last_ts
            """,
            (chat_id,),
        )
        self.conn.commit()

    # ---------------- read (используют server.py / sql.py) ----------------

    def chats(self, query: Optional[str] = None, limit: int = 50) -> list[dict]:
        sql = """
            SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS msg_count
            FROM chats c
        """
        args: list[Any] = []
        if query:
            sql += " WHERE lower(coalesce(c.title,'')) LIKE ? OR lower(coalesce(c.username,'')) LIKE ?"
            args += [f"%{query.lower()}%", f"%{query.lower()}%"]
        sql += " ORDER BY msg_count DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def topics(self, chat_id: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT t.*, (SELECT COUNT(*) FROM messages m
                         WHERE m.chat_id = t.chat_id
                           AND m.topic_id = t.topic_id) AS local_count
            FROM topics t WHERE t.chat_id = ? ORDER BY t.topic_id
            """,
            (chat_id,),
        )
        return [dict(r) for r in rows]

    def all_topics(self) -> dict[int, list[dict]]:
        """Все темы одним запросом, сгруппированные по chat_id."""
        out: dict[int, list[dict]] = {}
        for r in self.conn.execute(
            """
            SELECT t.*, (SELECT COUNT(*) FROM messages m
                         WHERE m.chat_id = t.chat_id
                           AND m.topic_id = t.topic_id) AS local_count
            FROM topics t ORDER BY t.chat_id, t.topic_id
            """
        ):
            out.setdefault(r["chat_id"], []).append(dict(r))
        return out

    def history(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        limit: int = 100,
        since_ts: Optional[int] = None,
        sender: Optional[str] = None,
        max_chars: int = 1200,
        before_id: Optional[int] = None,
        date_from: Optional[int] = None,
        date_to: Optional[int] = None,
    ) -> list[dict]:
        sql = "SELECT * FROM messages WHERE chat_id = ? AND deleted = 0"
        args: list[Any] = [chat_id]
        if topic_id is not None:
            # 0 и 1 — General: сообщения без темы хранятся канонически как 1
            sql += " AND topic_id = ?"
            args.append(1 if topic_id in (0, 1) else topic_id)
        if since_ts:
            sql += " AND ts >= ?"
            args.append(since_ts)
        if date_from:
            sql += " AND ts >= ?"
            args.append(date_from)
        if date_to:
            sql += " AND ts <= ?"
            args.append(date_to)
        if before_id:
            sql += " AND msg_id < ?"
            args.append(before_id)
        if sender:
            sql += " AND lower(coalesce(sender_name,'')) LIKE ?"
            args.append(f"%{sender.lower()}%")
        sql += " ORDER BY ts DESC, msg_id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            if d.get("text"):
                d["text"] = clean_text(d["text"], max_chars)
            if d.get("sender_name"):
                d["sender_name"] = sanitize_name(d["sender_name"])
            d.pop("raw_json", None)
            out.append(d)
        out.reverse()  # хронологический порядок — удобнее читать агенту
        return out

    def search(
        self,
        query: str,
        chat_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        limit: int = 40,
        context: int = 0,
        chat_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        sql = """
            SELECT m.*, c.title AS chat_title FROM messages m
            LEFT JOIN chats c ON c.id = m.chat_id
            WHERE m.text LIKE ? AND m.deleted = 0
        """
        args: list[Any] = [f"%{query}%"]
        if chat_id is not None:
            sql += " AND m.chat_id = ?"
            args.append(chat_id)
        elif chat_ids:
            sql += f" AND m.chat_id IN ({','.join('?' * len(chat_ids))})"
            args.extend(chat_ids)
        if topic_id is not None:
            sql += " AND m.topic_id = ?"
            args.append(1 if topic_id in (0, 1) else topic_id)
        sql += " ORDER BY m.ts DESC LIMIT ?"
        args.append(limit)
        hits = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            d.pop("raw_json", None)
            if d.get("text"):
                d["text"] = clean_text(d["text"])
            if context:
                d["context_before"] = self._around(d, context, before=True)
                d["context_after"] = self._around(d, context, before=False)
            hits.append(d)
        return hits

    def _around(self, msg: dict, n: int, before: bool) -> list[dict]:
        op = "<" if before else ">"
        order = "DESC" if before else "ASC"
        rows = self.conn.execute(
            f"""
            SELECT msg_id, date_iso, sender_name, text FROM messages
            WHERE chat_id = ? AND topic_id = COALESCE(?, 1)
              AND (ts, msg_id) {op} (?, ?)
              AND deleted = 0
            ORDER BY ts {order}, msg_id {order} LIMIT ?
            """,
            (msg["chat_id"], msg.get("topic_id"), msg.get("ts") or 0, msg["msg_id"], n),
        )
        out = [dict(r) for r in rows]
        return out[::-1] if before else out

    def stats(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        since_ts: Optional[int] = None,
        top: int = 10,
    ) -> dict:
        where = "WHERE chat_id = ?"
        args: list[Any] = [chat_id]
        if topic_id is not None:
            where += " AND topic_id = ?"
            args.append(1 if topic_id in (0, 1) else topic_id)
        if since_ts:
            where += " AND ts >= ?"
            args.append(since_ts)

        def one(sql: str, a: Iterable[Any] = ()) -> Any:
            return self.conn.execute(sql, list(args) + list(a)).fetchone()

        total = one(f"SELECT COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts FROM messages {where}")
        by_topic = [
            dict(r)
            for r in self.conn.execute(
                f"""SELECT topic_id,
                           (SELECT title FROM topics t
                             WHERE t.chat_id = ? AND t.topic_id = messages.topic_id) AS title,
                           COUNT(*) AS n,
                           COUNT(DISTINCT sender_id) AS senders,
                           MAX(ts) AS last_ts
                    FROM messages {where}
                    GROUP BY topic_id ORDER BY n DESC""",
                [chat_id] + args,
            )
        ]
        by_sender = [
            dict(r)
            for r in self.conn.execute(
                f"""SELECT sender_id, sender_name, COUNT(*) AS n,
                           SUM(LENGTH(COALESCE(text,''))) AS chars, MAX(ts) AS last_ts
                    FROM messages {where} GROUP BY sender_id
                    ORDER BY n DESC LIMIT ?""",
                args + [top],
            )
        ]
        by_day = [
            dict(r)
            for r in self.conn.execute(
                f"""SELECT date(date_iso) AS day, COUNT(*) AS n FROM messages {where}
                    GROUP BY day ORDER BY day DESC LIMIT 31""",
                args,
            )
        ]
        media = [
            dict(r)
            for r in self.conn.execute(
                f"SELECT COALESCE(media,'text') AS kind, COUNT(*) AS n FROM messages {where} "
                f"GROUP BY kind ORDER BY n DESC",
                args,
            )
        ]
        top_re = [
            dict(r)
            for r in self.conn.execute(
                f"""SELECT m.msg_id, m.sender_name, substr(COALESCE(m.text,''),1,90) AS text, m.ts,
                           SUM(CAST(j.value->>'count' AS INTEGER)) AS score,
                           GROUP_CONCAT(DISTINCT j.value->>'emoji') AS emojis
                    FROM messages m, json_each(m.reactions) j
                    {where} AND m.reactions IS NOT NULL AND m.reactions != '[]'
                    GROUP BY m.msg_id HAVING score > 0
                    ORDER BY score DESC LIMIT 5""",
                args,
            )
        ]
        return {
            "chat_id": chat_id,
            "topic_id": topic_id,
            "since_ts": since_ts,
            "total_messages": total["n"],
            "first_ts": total["first_ts"],
            "last_ts": total["last_ts"],
            "by_topic": by_topic,
            "top_senders": by_sender,
            "by_day": by_day,
            "by_media": media,
            "top_reactions": top_re,
        }


    # ---------------- watch-лист (то, за чем следит агент) ----------------

    def watch_add(self, chat_id: int, topic_id: int, alias: Optional[str] = None,
                  pinned: bool = False, project: Optional[str] = None) -> None:
        # 0 и 1 — это одно и то же: General / группа без тем. Храним канонически 1.
        topic_id = 1 if topic_id in (0, 1) else topic_id
        self.conn.execute(
            "INSERT INTO watch(chat_id, topic_id, alias, pinned, added_at, project) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(chat_id, topic_id) DO UPDATE SET "
            "alias = COALESCE(excluded.alias, watch.alias), pinned = excluded.pinned, "
            "project = COALESCE(excluded.project, watch.project)",
            (chat_id, topic_id, alias, int(pinned), int(time.time()), project),
        )
        self.conn.commit()

    def watch_set_project(self, chat_id: int, topic_id: int, project: Optional[str]) -> bool:
        topic_id = 1 if topic_id in (0, 1) else topic_id
        row = self.conn.execute(
            "SELECT project FROM watch WHERE chat_id = ? AND topic_id = ?",
            (chat_id, topic_id),
        ).fetchone()
        if row is None:
            return False
        cur = self.conn.execute(
            "UPDATE watch SET project = ? WHERE chat_id = ? AND topic_id = ?",
            (project, chat_id, topic_id),
        )
        if row["project"] != project:
            self.conn.execute(
                "UPDATE entities SET project = ? WHERE chat_id = ? AND topic_id = ? "
                "AND closed_ts IS NULL",
                (project, chat_id, topic_id),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def watch_remove(self, chat_id: int, topic_id: int) -> bool:
        topic_id = 1 if topic_id in (0, 1) else topic_id
        cur = self.conn.execute(
            "DELETE FROM watch WHERE chat_id=? AND topic_id=?", (chat_id, topic_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def watched_pairs(self) -> set[tuple[int, int]]:
        """Пары (chat_id, topic_id) из watch-листа; тема General нормализована в 1."""
        return {(r["chat_id"], r["topic_id"]) for r in self.conn.execute(
            "SELECT chat_id, topic_id FROM watch")}

    def watch_list(self, chat_id: Optional[int] = None) -> list[dict]:
        sql = """
            SELECT w.*, c.title AS chat_title, c.username, c.is_forum,
                   (SELECT title FROM topics t
                     WHERE t.chat_id = w.chat_id AND t.topic_id = w.topic_id) AS topic_title,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.chat_id = w.chat_id AND m.topic_id = w.topic_id) AS total,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.chat_id = w.chat_id AND m.topic_id = w.topic_id
                       AND m.msg_id > w.last_read) AS new_since_read,
                   (SELECT MAX(ts) FROM messages m
                     WHERE m.chat_id = w.chat_id AND m.topic_id = w.topic_id) AS last_ts
            FROM watch w LEFT JOIN chats c ON c.id = w.chat_id
        """
        args: list[Any] = []
        if chat_id is not None:
            sql += " WHERE w.chat_id = ?"
            args.append(chat_id)
        sql += " ORDER BY w.pinned DESC, COALESCE(w.alias, c.title, '') COLLATE NOCASE"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def watch_mark_read(self, chat_id: int, topic_id: int, msg_id: int) -> bool:
        topic_id = 1 if topic_id in (0, 1) else topic_id
        cur = self.conn.execute(
            "UPDATE watch SET last_read = ? WHERE chat_id = ? AND topic_id = ?",
            (msg_id, chat_id, topic_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def watch_new(self, chat_id: int, topic_id: int, limit: int = 200,
                  max_chars: int = 1200) -> dict:
        """Новые сообщения темы с момента last_read + сколько их всего."""
        topic_id = 1 if topic_id in (0, 1) else topic_id
        row = self.conn.execute(
            "SELECT last_read FROM watch WHERE chat_id=? AND topic_id=?", (chat_id, topic_id)
        ).fetchone()
        if row is None:
            return {"error": f"тема {topic_id} группы {chat_id} не в watch-листе"}
        last_read = row["last_read"] or 0
        rows = self.conn.execute(
            """
            SELECT * FROM messages
            WHERE chat_id = ? AND topic_id = ? AND msg_id > ? AND deleted = 0
            ORDER BY msg_id LIMIT ?
            """,
            (chat_id, topic_id, last_read, limit),
        )
        out = []
        max_id = last_read
        for r in rows:
            d = dict(r)
            d.pop("raw_json", None)
            if d.get("text"):
                d["text"] = clean_text(d["text"], max_chars)
            if d.get("sender_name"):
                d["sender_name"] = sanitize_name(d["sender_name"])
            out.append(d)
            max_id = max(max_id, d["msg_id"])
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=? AND topic_id=? "
            "AND msg_id > ? AND deleted = 0",
            (chat_id, topic_id, last_read),
        ).fetchone()[0]
        return {
            "chat_id": chat_id,
            "topic_id": topic_id,
            "last_read": last_read,
            "returned": len(out),
            "pending_total": pending,
            "truncated": pending > len(out),
            "max_id": max_id,
            "messages": out,
        }

    # ---------------- сущности, извлечённые из переписки (читает агент) ----------------

    def entities_list(self, pack: Optional[str] = None, kind: Optional[str] = None,
                      status: Optional[str] = None, project: Optional[str] = None,
                      owner: Optional[str] = None, chat_id: Optional[int] = None,
                      query: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM entities WHERE 1=1"
        args: list[Any] = []
        if pack is not None:
            sql += " AND pack = ?"
            args.append(pack)
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        if project is not None:
            sql += " AND project = ?"
            args.append(project)
        if owner is not None:
            sql += " AND owner = ?"
            args.append(owner)
        if chat_id is not None:
            sql += (" AND (EXISTS(SELECT 1 FROM entity_sources es "
                    "WHERE es.entity_id = entities.id AND es.chat_id = ?) "
                    "OR (chat_id = ? AND NOT EXISTS(SELECT 1 FROM entity_sources es2 "
                    "WHERE es2.entity_id = entities.id)))")
            args += [chat_id, chat_id]
        if query:
            sql += " AND (title LIKE ? OR body LIKE ?)"
            args += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY updated_ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def entity_get(self, entity_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["events"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id = ? ORDER BY ts, id", (entity_id,))]
        return d

    _SRC_UPSERT = (
        "INSERT INTO entity_sources(entity_id, chat_id, topic_id, first_seen_ts, "
        "last_seen_ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(entity_id, chat_id, topic_id) DO UPDATE SET "
        "first_seen_ts=MIN(entity_sources.first_seen_ts, excluded.first_seen_ts), "
        "last_seen_ts=MAX(entity_sources.last_seen_ts, excluded.last_seen_ts)")

    def merge_entities(self, keeper_id: int, loser_ids: list[int], reason: str,
                       now: int) -> None:
        """Сливает дубли в keeper (hard-merge): поля fill-if-empty, события и
        интеграции репарентятся, источники объединяются, loser получает alias."""
        keeper = self.conn.execute(
            "SELECT * FROM entities WHERE id=?", (keeper_id,)).fetchone()
        if keeper is None:
            return
        donors = [r for r in (self.conn.execute(
            "SELECT * FROM entities WHERE id=?", (lid,)).fetchone()
            for lid in loser_ids) if r is not None]
        donors.sort(key=lambda r: r["updated_ts"] or 0, reverse=True)
        fills = {}
        for col in ("body", "owner", "due_ts", "project", "attrs_json"):
            val = keeper[col]
            if val is None or val == "":
                for d in donors:
                    if d[col] is not None and d[col] != "":
                        val = d[col]
                        break
            fills[col] = val
        confidence = keeper["confidence"]
        for d in donors:
            if d["confidence"] is not None and (
                    confidence is None or d["confidence"] > confidence):
                confidence = d["confidence"]
        updated = keeper["updated_ts"] or keeper["created_ts"] or now
        for d in donors:
            ts = d["updated_ts"] or d["created_ts"] or now
            if ts > updated:
                updated = ts
        self.conn.execute(
            "UPDATE entities SET body=?, owner=?, due_ts=?, project=?, attrs_json=?, "
            "confidence=?, updated_ts=? WHERE id=?",
            (fills["body"], fills["owner"], fills["due_ts"], fills["project"],
             fills["attrs_json"], confidence, updated, keeper_id))
        self.conn.execute(self._SRC_UPSERT, (
            keeper_id, keeper["chat_id"], keeper["topic_id"],
            keeper["created_ts"], keeper["updated_ts"] or keeper["created_ts"]))
        for d in donors:
            self.conn.execute(
                "UPDATE entity_events SET entity_id=? WHERE entity_id=?",
                (keeper_id, d["id"]))
            self.conn.execute(
                "INSERT INTO entity_events(entity_id, run_id, ts, op, field, from_val, "
                "to_val, chat_id, msg_id, quote) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (keeper_id, None, now, "merge", "dedup", str(d["id"]), str(keeper_id),
                 None, None, None))
            for s in self.conn.execute(
                    "SELECT chat_id, topic_id, first_seen_ts, last_seen_ts "
                    "FROM entity_sources WHERE entity_id=?", (d["id"],)).fetchall():
                self.conn.execute(self._SRC_UPSERT, (
                    keeper_id, s["chat_id"], s["topic_id"],
                    s["first_seen_ts"], s["last_seen_ts"]))
            self.conn.execute(self._SRC_UPSERT, (
                keeper_id, d["chat_id"], d["topic_id"],
                d["created_ts"], d["updated_ts"] or d["created_ts"]))
            for sysrow in self.conn.execute(
                    "SELECT system FROM integrations WHERE entity_id=?",
                    (d["id"],)).fetchall():
                clash = self.conn.execute(
                    "SELECT 1 FROM integrations WHERE entity_id=? AND system=?",
                    (keeper_id, sysrow["system"])).fetchone()
                if clash is not None:
                    self.conn.execute(
                        "DELETE FROM integrations WHERE entity_id=? AND system=?",
                        (d["id"], sysrow["system"]))
                else:
                    self.conn.execute(
                        "UPDATE integrations SET entity_id=? "
                        "WHERE entity_id=? AND system=?",
                        (keeper_id, d["id"], sysrow["system"]))
            self.conn.execute(
                "INSERT INTO entity_aliases(old_entity_id, entity_id, merged_ts, reason) "
                "VALUES(?,?,?,?)", (d["id"], keeper_id, now, reason))
            self.conn.execute(
                "DELETE FROM entity_sources WHERE entity_id=?", (d["id"],))
            self.conn.execute("DELETE FROM entities WHERE id=?", (d["id"],))

    def entity_in_chats(self, entity_id: int, chat_ids: Iterable[int]) -> bool:
        """Видимость сущности из любого чата: сначала bridge, при отсутствии
        sources-строк — фолбэк на первичный chat_id."""
        ids = list(set(chat_ids))
        if not ids:
            return False
        marks = ",".join("?" * len(ids))
        if self.conn.execute(
                f"SELECT 1 FROM entity_sources WHERE entity_id=? AND chat_id IN ({marks}) "
                "LIMIT 1", [entity_id, *ids]).fetchone() is not None:
            return True
        row = self.conn.execute(
            "SELECT chat_id FROM entities WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            return False
        has_src = self.conn.execute(
            "SELECT 1 FROM entity_sources WHERE entity_id=? LIMIT 1",
            (entity_id,)).fetchone()
        return has_src is None and row["chat_id"] in ids

    def entity_overdue(self, project: Optional[str] = None, days_ahead: int = 0,
                       limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM entities WHERE closed_ts IS NULL AND due_ts IS NOT NULL AND due_ts <= ?"
        args: list[Any] = [int(time.time()) + days_ahead * 86400]
        if project is not None:
            sql += " AND project = ?"
            args.append(project)
        sql += " ORDER BY due_ts LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def entity_changes(self, since_ts: int, pack: Optional[str] = None,
                       project: Optional[str] = None, limit: int = 500) -> dict:
        filt = ""
        args: list[Any] = []
        if pack is not None:
            filt += " AND e.pack = ?"
            args.append(pack)
        if project is not None:
            filt += " AND e.project = ?"
            args.append(project)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM entities e WHERE e.id IN "
            "(SELECT entity_id FROM entity_events WHERE ts > ?)" + filt,
            [since_ts, *args]).fetchone()[0]
        sql = ("SELECT e.* FROM entities e JOIN "
               "(SELECT entity_id, MAX(ts) AS max_ts FROM entity_events WHERE ts > ? "
               "GROUP BY entity_id) ch ON ch.entity_id = e.id WHERE 1=1" + filt +
               " ORDER BY max_ts DESC, e.id LIMIT ?")
        items = []
        for r in self.conn.execute(sql, [since_ts, *args, limit]):
            d = dict(r)
            d["events"] = [dict(ev) for ev in self.conn.execute(
                "SELECT ts, op, field, from_val, to_val, chat_id, msg_id, quote "
                "FROM entity_events WHERE entity_id = ? AND ts > ? ORDER BY ts, id",
                (d["id"], since_ts))]
            items.append(d)
        return {"since_ts": since_ts, "count": count, "items": items}

    def project_brief(self, project: str, pack: Optional[str] = None) -> dict:
        where = " WHERE e.project = ?"
        args: list[Any] = [project]
        if pack is not None:
            where += " AND e.pack = ?"
            args.append(pack)
        now = int(time.time())
        counts = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT e.status, COUNT(*) n FROM entities e" + where + " GROUP BY e.status",
            args)}
        open_n = self.conn.execute(
            "SELECT COUNT(*) FROM entities e" + where + " AND e.closed_ts IS NULL",
            args).fetchone()[0]
        overdue = self.conn.execute(
            "SELECT COUNT(*) FROM entities e" + where +
            " AND e.closed_ts IS NULL AND e.due_ts <= ?", args + [now]).fetchone()[0]
        next_due = self.conn.execute(
            "SELECT MIN(e.due_ts) FROM entities e" + where +
            " AND e.closed_ts IS NULL AND e.due_ts IS NOT NULL", args).fetchone()[0]
        open_questions = self.conn.execute(
            "SELECT COUNT(*) FROM entities e" + where +
            " AND e.kind = 'question' AND e.closed_ts IS NULL", args).fetchone()[0]
        promises_no_due = self.conn.execute(
            "SELECT COUNT(*) FROM entities e" + where +
            " AND e.kind = 'promise' AND e.closed_ts IS NULL AND e.due_ts IS NULL",
            args).fetchone()[0]
        promises_no_owner = self.conn.execute(
            "SELECT COUNT(*) FROM entities e" + where +
            " AND e.kind = 'promise' AND e.closed_ts IS NULL"
            " AND (e.owner IS NULL OR e.owner = '')", args).fetchone()[0]
        top_owners = [[r["owner"], r["n"]] for r in self.conn.execute(
            "SELECT e.owner, COUNT(*) n FROM entities e" + where +
            " AND e.closed_ts IS NULL AND e.owner IS NOT NULL AND e.owner != ''"
            " GROUP BY e.owner ORDER BY n DESC, e.owner LIMIT 5", args)]
        last_event_ts = self.conn.execute(
            "SELECT MAX(ev.ts) FROM entity_events ev JOIN entities e "
            "ON e.id = ev.entity_id" + where, args).fetchone()[0]
        return {
            "project": project,
            "pack": pack,
            "counts": counts,
            "open": open_n,
            "overdue": overdue,
            "next_due": next_due,
            "open_questions": open_questions,
            "promises_no_due": promises_no_due,
            "promises_no_owner": promises_no_owner,
            "top_owners": top_owners,
            "last_event_ts": last_event_ts,
        }

    def project_gaps(self, pack: Optional[str] = None, project: Optional[str] = None,
                     limit: int = 100) -> dict:
        where = (" WHERE closed_ts IS NULL AND (kind = 'question'"
                 " OR (kind = 'promise' AND due_ts IS NULL)"
                 " OR (kind = 'promise' AND (owner IS NULL OR owner = '')))")
        args: list[Any] = []
        if pack is not None:
            where += " AND pack = ?"
            args.append(pack)
        if project is not None:
            where += " AND project = ?"
            args.append(project)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM entities" + where, args).fetchone()[0]
        sql = ("SELECT id, pack, kind, title, owner, due_ts, project, created_ts, "
               "CASE WHEN kind = 'question' THEN 'вопрос без ответа' "
               "WHEN due_ts IS NULL THEN 'обещание без срока' "
               "ELSE 'обещание без ответственного' END AS reason "
               "FROM entities" + where + " ORDER BY created_ts, id LIMIT ?")
        items = [dict(r) for r in self.conn.execute(sql, args + [limit])]
        return {"count": count, "items": items}

    # ---------------- полнотекстовый поиск по истории ----------------

    def search_fts(self, query: str, chat_id: Optional[int] = None,
                   topic_id: Optional[int] = None, limit: int = 40,
                   chat_ids: Optional[list[int]] = None) -> dict:
        """FTS5-поиск; при любой ошибке синтаксиса/доступности падает на LIKE."""
        words = [w for w in re.split(r"\s+", query.strip()) if w]
        if not words:
            return {"engine": "none", "count": 0, "hits": []}
        if self.fts:
            match = " AND ".join('"' + w.replace('"', '""') + '"' for w in words)
            sql = """
                SELECT f.chat_id, f.topic_id, f.rowid AS rid, bm25(messages_fts) AS rank
                FROM messages_fts f WHERE messages_fts MATCH ?
            """
            args: list[Any] = [match]
            if chat_id is not None:
                sql += " AND f.chat_id = ?"
                args.append(chat_id)
            elif chat_ids:
                sql += f" AND f.chat_id IN ({','.join('?' * len(chat_ids))})"
                args.extend(chat_ids)
            if topic_id is not None:
                sql += " AND COALESCE(f.topic_id,1) = ?"
                args.append(1 if topic_id in (0, 1) else topic_id)
            sql += " ORDER BY rank LIMIT ?"
            args.append(limit)
            try:
                rids = list(self.conn.execute(sql, args))
            except sqlite3.OperationalError:
                rids = None
            if rids is not None:
                by_rid: dict[int, dict] = {}
                if rids:
                    marks = ",".join("?" * len(rids))
                    for m in self.conn.execute(
                        f"SELECT rowid, * FROM messages WHERE rowid IN ({marks}) "
                        f"AND deleted = 0",
                        [r["rid"] for r in rids],
                    ):
                        d = dict(m)
                        d.pop("raw_json", None)
                        if d.get("text"):
                            d["text"] = clean_text(d["text"])
                        by_rid[m["rowid"]] = d
                hits = []
                for r in rids:
                    d = by_rid.get(r["rid"])
                    if not d:
                        continue
                    d["rank"] = r["rank"]
                    hits.append(d)
                return {"engine": "fts5", "query": query, "count": len(hits), "hits": hits}
        like = "%" + " ".join(words) + "%"
        hits = self.search(like, chat_id=chat_id, topic_id=topic_id, limit=limit,
                           chat_ids=chat_ids)
        return {"engine": "like", "query": query, "count": len(hits), "hits": hits}



# ------------------- ссылки и закрепы -------------------

#regexp портированы из 0xMRK95/telegram-mcp-server (extract_refs, MIT)
_REF_MENTION = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")
_REF_TME_PUBLIC = re.compile(
    r"t\.me/(?!joinchat/|\+|share/|addstickers/|proxy/|socks/)([A-Za-z][A-Za-z0-9_]{3,31})")
_REF_INVITE = re.compile(r"t\.me/(\+[\w-]+|joinchat/[\w-]+)")
_REF_URL = re.compile(r"https?://[^\s<>\"']+")


def extract_refs(text: Optional[str], fwd_channel_id: Optional[int] = None) -> list[tuple[str, str]]:
    """Вытаскивает из текста сообщения все ссылки: @юзернеймы, публичные t.me,
    приватные инвайты, произвольные URL; плюс канал-источник пересланного."""
    t = text or ""
    out: list[tuple[str, str]] = []
    for m in _REF_MENTION.findall(t):
        out.append(("mention", m.lower()))
    for inv in _REF_INVITE.findall(t):
        out.append(("invite", inv))
    for pub in _REF_TME_PUBLIC.findall(t):
        out.append(("mention", pub.lower()))
    for u in _REF_URL.findall(t):
        out.append(("url", u))
    if fwd_channel_id:
        out.append(("forward", str(fwd_channel_id)))
    return out


# ------------------- безопасный SQL для агента -------------------

_ALLOWED = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

# коды действий authorizer: 20 READ / 21 SELECT / 31 FUNCTION / 33 RECURSIVE
_READONLY_ACTIONS = {20, 21, 31, 33}


def _read_only_authorizer(action: int, arg1: Any, arg2: Any, db_name: Any, trigger: Any) -> int:
    return 0 if action in _READONLY_ACTIONS else 1


def run_sql(db: DB, sql: str, params: tuple = (), limit: int = 200) -> dict:
    """Read-only SQL поверх индекса: только SELECT/WITH, отдельное соединение
    с PRAGMA query_only и authorizer, отклоняющим всё кроме чтения."""
    if not _ALLOWED.match(sql):
        return {"ok": False, "error": "только SELECT / WITH"}
    limit = min(max(int(limit), 1), 2000)
    conn = sqlite3.connect(str(db.path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_read_only_authorizer)
        cur = conn.execute(sql, params)
        columns = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(limit)
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"SQL: {exc}"}
    finally:
        conn.close()
    masked = [
        {k: (redact(v)[0] if isinstance(v, str) else v) for k, v in dict(r).items()}
        for r in rows
    ]
    return {
        "ok": True,
        "columns": columns,
        "rows": masked,
        "row_count": len(masked),
        "truncated": len(masked) == limit,
    }
