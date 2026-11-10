#!/usr/bin/env python3
"""Мультиаккаунт: конфигурация, миграция v18, линза аккаунта, дедуп ингеста.

Каналы и супергруппы имеют глобальные chat_id/msg_id, поэтому два аккаунта
пишут одни и те же строки в общую БД и сущности сливаются штатным dedup_key.
Базовые группы и личные диалоги — account-scoped: их слияние не поддерживается
(см. README, «Ограничения мультиаккаунта»).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

import accounts as accounts_mod
import sync as sync_mod
import workspace_store
from db import DB, SCHEMA_VERSION

NOW = datetime.now(timezone.utc)
FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


@contextlib.contextmanager
def env_guard(**set_env):
    saved = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.startswith("TG_") or k.startswith("TELEGRAM_"):
                del os.environ[k]
        os.environ.update({k: str(v) for k, v in set_env.items()})
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ---------------------------------------------------------------- fake telethon

class User:
    def __init__(self, uid, first_name):
        self.id, self.first_name, self.last_name, self.username = uid, first_name, "", ""


class Msg:
    def __init__(self, mid, text, sender=None):
        self.id, self.date, self.message, self.media = mid, NOW, text, None
        self.sender_id = sender.id if sender else None
        self._sender = sender
        self.reply_to, self.out, self.action = None, False, None
        self.views, self.forwards, self.reactions = None, None, None
        self.pinned, self.fwd_from = False, None


class Channel:
    def __init__(self, cid, title, username="", forum=False):
        self.id, self.title, self.username = cid, title, username
        self.megagroup, self.broadcast, self.forum = True, False, forum
        self.participants_count = 10


class Dialog:
    def __init__(self, entity, name):
        self.entity, self.name = entity, name


class FakeClient:
    """Мини-Telethon для sync.run: один канал/супергруппа с сообщениями."""

    def __init__(self, entity, messages):
        self.entity, self.messages = entity, messages

    async def get_dialogs(self):
        return [Dialog(self.entity, self.entity.title)]

    async def iter_dialogs(self, folder=None, **kw):
        yield Dialog(self.entity, self.entity.title)

    async def get_entity(self, peer):
        return self.entity

    async def iter_messages(self, entity, limit=None, **kw):
        for m in sorted(self.messages, key=lambda x: x.id):
            yield m

    async def get_messages(self, entity, limit=None, filter=None, ids=None, **kw):
        return []

    async def __call__(self, req):
        return []


def _run_sync(db_path: Path, client: FakeClient, account: str | None = None) -> dict:
    return asyncio.run(sync_mod.run(
        db_path=str(db_path), client=client, names=[str(sync_mod.marked_id(client.entity))],
        account=account))


# ---------------------------------------------------------------- tests

def test_config_resolution(tmp: Path) -> None:
    print("=== конфигурация аккаунтов: неявный default и явные TG_ACCOUNTS ===")
    with env_guard(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="h",
                   TELEGRAM_SESSION_NAME="tg_agent_session"):
        check(accounts_mod.account_names() == ["default"],
              "без TG_ACCOUNTS: единственный неявный default", str(accounts_mod.account_names()))
        check(accounts_mod.is_multi() is False, "без TG_ACCOUNTS: is_multi False")
        check(accounts_mod.configured_accounts() == [],
              "без TG_ACCOUNTS: линза пуста (нулевая цена)", str(accounts_mod.configured_accounts()))
        cfg = accounts_mod.resolve()
        check(cfg["id"] == "default" and cfg["session_name"] == "tg_agent_session"
              and cfg["api_id"] == "1",
              "default: берёт текущие TELEGRAM_*", str(cfg))
    with env_guard(TG_ACCOUNTS="work,home", TELEGRAM_API_ID="1", TELEGRAM_API_HASH="h",
                   TG_ACCOUNT_WORK_SESSION="sess_work", TG_ACCOUNT_WORK_API_ID="22",
                   TG_ACCOUNT_WORK_API_HASH="wh"):
        check(accounts_mod.account_names() == ["work", "home"],
              "TG_ACCOUNTS: список имён", str(accounts_mod.account_names()))
        check(accounts_mod.is_multi() is True, "TG_ACCOUNTS: is_multi True")
        check(accounts_mod.configured_accounts() == ["work", "home"],
              "TG_ACCOUNTS: линза = настроенные аккаунты", str(accounts_mod.configured_accounts()))
        w = accounts_mod.resolve("work")
        check(w["session_name"] == "sess_work" and w["api_id"] == "22" and w["api_hash"] == "wh",
              "work: свои session/api_id/api_hash", str(w))
        h = accounts_mod.resolve("home")
        check(h["session_name"] == "tg_agent_session_home" and h["api_id"] == "1",
              "home: session по умолчанию, api_id из общего окружения", str(h))


def test_migration_v18(tmp: Path) -> None:
    print("=== миграция v18: accounts/chat_accounts, folders.account_id ===")
    p = tmp / "fresh.sqlite"
    with DB(p) as db:
        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        fcols = {r[1] for r in db.conn.execute("PRAGMA table_info(folders)")}
        fc = {r[1] for r in db.conn.execute("PRAGMA table_info(folder_chats)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check({"accounts", "chat_accounts"} <= tables, "fresh: таблицы аккаунтов", str(sorted(tables)))
    check("account_id" in fcols and "account_id" in fc,
          "fresh: folders/folder_chats с account_id", f"{sorted(fcols)}/{sorted(fc)}")
    check(uv == SCHEMA_VERSION, "fresh: user_version = SCHEMA_VERSION", str(uv))

    with DB(p) as db:
        db.conn.execute("INSERT INTO chat_accounts(chat_id, account_id, is_primary) "
                        "VALUES(-1001, 'work', 1)")
        db.conn.commit()
    rejected = False
    with DB(p) as db:
        try:
            db.conn.execute("INSERT INTO chat_accounts(chat_id, account_id, is_primary) "
                            "VALUES(-1001, 'home', 1)")
            db.conn.commit()
        except sqlite3.IntegrityError:
            rejected = True
    check(rejected, "fresh: частичный уникальный индекс — не больше одного primary на чат")

    p2 = tmp / "old.sqlite"
    with DB(p2) as db:
        db.conn.execute("INSERT INTO folders(account_id, folder_id, title, synced_at) "
                        "VALUES('default', 7, 'AI', 1)")
        db.conn.execute("INSERT INTO folder_chats(account_id, folder_id, chat_id) "
                        "VALUES('default', 7, -1001)")
        db.conn.commit()
    raw = sqlite3.connect(str(p2))
    raw.execute("DROP TABLE folders")
    raw.execute("CREATE TABLE folders(folder_id INTEGER PRIMARY KEY, title TEXT, synced_at INTEGER)")
    raw.execute("INSERT INTO folders(folder_id, title, synced_at) VALUES(7, 'AI', 1)")
    raw.execute("DROP TABLE folder_chats")
    raw.execute("CREATE TABLE folder_chats(folder_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, "
                "PRIMARY KEY(folder_id, chat_id))")
    raw.execute("INSERT INTO folder_chats(folder_id, chat_id) VALUES(7, -1001)")
    raw.execute("DROP TABLE accounts")
    raw.execute("DROP TABLE chat_accounts")
    raw.execute("PRAGMA user_version = 17")
    raw.commit()
    raw.close()
    with DB(p2) as db:
        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        fcols = {r[1] for r in db.conn.execute("PRAGMA table_info(folders)")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
        folders = db.folders_list()
    check({"accounts", "chat_accounts"} <= tables, "v17->v18: таблицы аккаунтов созданы")
    check("account_id" in fcols, "v17->v18: folders.account_id добавлен")
    check(uv == SCHEMA_VERSION, "v17->v18: user_version поднят", str(uv))
    check(len(folders) == 1 and folders[0]["account_id"] == "default"
          and folders[0]["chat_ids"] == [-1001],
          "v17->v18: старые папки сохранены под default", str(folders))


def test_single_account_noop(tmp: Path) -> None:
    print("=== один аккаунт: sync не пишет строк аккаунтов (нулевая цена) ===")
    db_path = tmp / "single.sqlite"
    client = FakeClient(Channel(555, "Proj", "proj"), [Msg(1, "hello", User(1, "A"))])
    with env_guard(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="h"):
        _run_sync(db_path, client)
    with DB(db_path) as db:
        accounts = db.accounts_list()
        chat_acc = db.chat_accounts()
        msgs = db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    check(accounts == [], "single: таблица accounts пуста", str(accounts))
    check(chat_acc == [], "single: chat_accounts пуста", str(chat_acc))
    check(msgs == 1, "single: сообщение записано как обычно", str(msgs))


def test_two_accounts_dedup(tmp: Path) -> None:
    print("=== два аккаунта: один канал -> одни строки, одна сущность ===")
    db_path = tmp / "multi.sqlite"
    client = FakeClient(Channel(555, "Proj", "proj"),
                        [Msg(1, "hello", User(1, "A")), Msg(2, "world", User(1, "A"))])
    with env_guard(TG_ACCOUNTS="work,home", TELEGRAM_API_ID="1", TELEGRAM_API_HASH="h"):
        _run_sync(db_path, client, account="work")
        _run_sync(db_path, client, account="home")
    with DB(db_path) as db:
        msgs = db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        accounts = {a["id"] for a in db.accounts_list()}
        chat_acc = db.chat_accounts(-1000000000555)
        primaries = [c["account_id"] for c in chat_acc if c["is_primary"]]
    check(msgs == 2, "multi: сообщения не задвоены (chat_id+msg_id дедуп)", str(msgs))
    check(accounts == {"work", "home"}, "multi: оба аккаунта в реестре", str(accounts))
    check({c["account_id"] for c in chat_acc} == {"work", "home"},
          "multi: чат виден обоим аккаунтам", str(chat_acc))
    check(len(primaries) == 1, "multi: ровно один primary на чат", str(primaries))

    with DB(db_path) as db:
        db.conn.execute("INSERT INTO entities(pack, chat_id, topic_id, kind, title, dedup_key, "
                        "created_ts) VALUES('services', -1000000000555, 1, 'service', 'Acme', "
                        "'acme', 1)")
        db.conn.commit()
    rejected = False
    with DB(db_path) as db:
        try:
            db.conn.execute("INSERT INTO entities(pack, chat_id, topic_id, kind, title, dedup_key, "
                            "created_ts) VALUES('services', -1000000000555, 1, 'service', 'Acme', "
                            "'acme', 2)")
            db.conn.commit()
        except sqlite3.IntegrityError:
            rejected = True
    check(rejected, "multi: глобальный dedup_key не даёт второй открытой сущности")


def test_account_lens(tmp: Path) -> None:
    print("=== линза аккаунта: фильтр списков/счётчиков, мультиисточник ===")
    db_path = tmp / "lens.sqlite"
    with DB(db_path) as db:
        db.conn.execute("INSERT INTO chats(id, title) VALUES(-1001, 'A')")
        db.conn.execute("INSERT INTO chats(id, title) VALUES(-1002, 'B')")
        db.conn.execute("INSERT INTO chat_accounts(chat_id, account_id, is_primary) "
                        "VALUES(-1001, 'work', 1)")
        db.conn.execute("INSERT INTO chat_accounts(chat_id, account_id, is_primary) "
                        "VALUES(-1002, 'home', 1)")
        db.conn.execute("INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, "
                        "created_ts, updated_ts) VALUES(1, 'services', -1001, 1, 'service', "
                        "'OnlyWork', 1, 1)")
        db.conn.execute("INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, "
                        "created_ts, updated_ts) VALUES(2, 'services', -1002, 1, 'service', "
                        "'OnlyHome', 1, 1)")
        db.conn.execute("INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, "
                        "created_ts, updated_ts) VALUES(3, 'services', -1001, 1, 'service', "
                        "'Shared', 1, 1)")
        db.conn.execute("INSERT INTO entity_sources(entity_id, chat_id, topic_id) "
                        "VALUES(3, -1001, 1)")
        db.conn.execute("INSERT INTO entity_sources(entity_id, chat_id, topic_id) "
                        "VALUES(3, -1002, 1)")
        db.conn.commit()
        work = workspace_store.entities(db, {"account": "work"})
        home = workspace_store.entities(db, {"account": "home"})
        allv = workspace_store.entities(db, {})
        detail = workspace_store.entity(db, 3)
    check(work["total"] == 2 and {i["title"] for i in work["items"]} == {"OnlyWork", "Shared"},
          "линза work: только чаты work", str(work["total"]))
    check(home["total"] == 2 and {i["title"] for i in home["items"]} == {"OnlyHome", "Shared"},
          "линза home: только чаты home", str(home["total"]))
    check(allv["total"] == 3, "без линзы: все сущности", str(allv["total"]))
    check(detail["entity"]["id"] == 3 and len(detail["entity"]["sources"]) == 2,
          "мультиисточник: один id, оба источника в детали",
          str(detail["entity"]["sources"]))


def test_boundary_documented(tmp: Path) -> None:
    print("=== граница: измерение аккаунта не прошито в идентичность сообщений ===")
    db_path = tmp / "boundary.sqlite"
    with DB(db_path) as db:
        mcols = {r[1] for r in db.conn.execute("PRAGMA table_info(messages)")}
        pk = [r[1] for r in db.conn.execute("PRAGMA table_info(messages)") if r[5]]
        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    check("account_id" not in mcols,
          "messages без account_id: каналы/супергруппы дедупятся глобально", str(sorted(mcols)))
    check(pk == ["chat_id", "msg_id"], "PK messages = (chat_id, msg_id)", str(pk))
    check("chat_accounts" in tables,
          "доступ к чату — отдельная таблица (задел под account-scoped группы)")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_accounts_"))
    try:
        test_config_resolution(tmp)
        test_migration_v18(tmp)
        test_single_account_noop(tmp)
        test_two_accounts_dedup(tmp)
        test_account_lens(tmp)
        test_boundary_documented(tmp)
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
