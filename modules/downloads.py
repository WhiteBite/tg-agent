"""Модуль скачивания медиа из индекса в локальную библиотеку с дедупликацией.

Точные дубли убираются двумя слоями:
  1. до скачивания — по id файла Telegram (doc:/photo:): пересланный в другой
     чат файл не качается повторно; id заново залитого того же ролика
     записывается алиасом на уже скачанный файл;
  2. после скачивания — по sha256 содержимого: пережатая копия остаётся
     одной строкой media_files, у файла прибавляются сообщения-источники.

Хранение контентно-адресное: downloads/files/<sha[:2]>/<sha><ext>, расширение —
из имени файла или mime. Задание — snapshot сообщений (jobs/job_items),
прогон идемпотентен: прерванный запуск продолжается с места остановки,
повторное задание по умолчанию не включает уже скачанные сообщения.

Режим forward (mode="forward", to=<адресат>) вместо скачивания пересылает
сообщения серверно: байты медиа через клиента не идут. Дедуп — только слой 1,
по id файла и адресу (forward_files/forward_sources); контентный sha256-дедуп
здесь невозможен. FloodWait до 120 с пережидается молча, дольше — задание
уходит в pause. Каналы с запретом пересылки пропускаются (forwards-restricted).

Гейт: TG_ALLOW_DOWNLOADS=1 (MCP-инструменты скачивания),
TG_ALLOW_FORWARDS=1 (пересылка — операция записи в Telegram);
каталог — TG_DOWNLOADS_DIR; лимит размера — TG_DOWNLOADS_MAX_MB (0 — без лимита).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from env import load_env
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

load_env()

import jobs
import sources
from db import DB
from workspace_store import WorkspaceError

HERE = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))
ROOT = Path(os.environ.get("TG_DOWNLOADS_DIR") or HERE / "downloads")

KNOWN_KINDS = ("video", "videonote", "photo")
KNOWN_MODES = ("download", "forward")
_KIND_MEDIA = {"video": "document", "videonote": "document", "photo": "photo"}
_TARGET_ALIASES = ("saved", "me", "self")
_FLOOD_INLINE_MAX = 120

_MIME_EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov",
             "video/x-matroska": ".mkv", "video/webm": ".webm",
             "image/jpeg": ".jpg"}
_KIND_EXT = {"video": ".mp4", "videonote": ".mp4", "photo": ".jpg"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS media_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256      TEXT NOT NULL UNIQUE,
    doc_id      TEXT NOT NULL UNIQUE,
    kind        TEXT,
    mime        TEXT,
    filename    TEXT,
    size        INTEGER,
    path        TEXT NOT NULL,
    created_ts  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_media_files_kind ON media_files (kind);

CREATE TABLE IF NOT EXISTS media_doc_alias (
    doc_id  TEXT PRIMARY KEY,
    file_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS media_file_sources (
    file_id  INTEGER NOT NULL,
    chat_id  INTEGER NOT NULL,
    msg_id   INTEGER NOT NULL,
    PRIMARY KEY (file_id, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_mfs_chat ON media_file_sources (chat_id, msg_id);

CREATE TABLE IF NOT EXISTS forward_files (
    doc_id         TEXT NOT NULL,
    target_chat_id INTEGER NOT NULL,
    target_msg_id  INTEGER NOT NULL,
    kind           TEXT,
    size           INTEGER,
    created_ts     INTEGER,
    PRIMARY KEY (doc_id, target_chat_id)
);
CREATE INDEX IF NOT EXISTS ix_forward_files_target ON forward_files (target_chat_id);

CREATE TABLE IF NOT EXISTS forward_sources (
    doc_id  TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    PRIMARY KEY (doc_id, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_fs_chat ON forward_sources (chat_id, msg_id);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)


def _max_mb(override: int | None = None) -> int:
    if override is not None:
        return max(0, int(override))
    return max(0, int(os.environ.get("TG_DOWNLOADS_MAX_MB") or 2000))


def _parse_kinds(kinds) -> tuple[str, ...]:
    if isinstance(kinds, str):
        parts = [p.strip() for p in kinds.split(",")]
    else:
        parts = [str(p).strip() for p in kinds or []]
    out = tuple(p for p in parts if p)
    if not out:
        raise ValueError("kinds не может быть пустым")
    unknown = [p for p in out if p not in KNOWN_KINDS]
    if unknown:
        raise ValueError(
            f"неизвестные kinds: {unknown} (доступны: {', '.join(KNOWN_KINDS)})")
    return out


def plan_job(db: DB, sources_spec: str = "watch", kinds: str = "video",
             days: int = 0, max_mb: int | None = None,
             include_downloaded: bool = False, mode: str = "download",
             to: str = "") -> dict:
    """Создать задание: выбрать сообщения по источникам/kinds/окну и снять snapshot."""
    kind_list = _parse_kinds(kinds)
    if mode not in KNOWN_MODES:
        raise ValueError(f"неизвестный mode: {mode!r} (доступны: download, forward)")
    to = (to or "").strip()
    if mode == "forward":
        _parse_to(to)
    chats = sources.resolve(db, sources_spec)
    chat_ids = [c["chat_id"] for c in chats]
    labels = sorted({_KIND_MEDIA[k] for k in kind_list})
    sql = (f"SELECT m.chat_id, m.msg_id FROM messages m "
           f"WHERE m.deleted=0 AND m.media IN ({','.join('?' * len(labels))})")
    args: list = list(labels)
    if chat_ids:
        sql += f" AND m.chat_id IN ({','.join('?' * len(chat_ids))})"
        args += chat_ids
    if days and days > 0:
        sql += " AND m.ts >= ?"
        args.append(int(time.time()) - int(days) * 86400)
    if not include_downloaded:
        done_tbl = "forward_sources" if mode == "forward" else "media_file_sources"
        sql += (f" AND NOT EXISTS (SELECT 1 FROM {done_tbl} s "
                f"WHERE s.chat_id=m.chat_id AND s.msg_id=m.msg_id)")
    sql += " ORDER BY m.chat_id, m.msg_id"
    items = [dict(r) for r in db.conn.execute(sql, args)]
    params = {"sources": sources_spec, "kinds": list(kind_list), "days": int(days),
              "max_mb": _max_mb(max_mb), "include_downloaded": bool(include_downloaded),
              "mode": mode, "to": to or None}
    job_id = jobs.create_job(db, "downloads", params, items)
    return {"job_id": job_id, "items": len(items), "chats": chats, "params": params}


async def run_async(db_path: str = DB_PATH, job_id: int | None = None, *,
                    sources_spec: str = "watch", kinds: str = "video",
                    days: int = 0, max_mb: int | None = None,
                    include_downloaded: bool = False, mode: str = "download",
                    to: str = "",
                    root: Path | None = None, client=None,
                    force: bool = False, retry: bool = False) -> dict:
    """Исполнить задание: claim -> пункты по одному -> отчёт. Идемпотентно."""
    claimed = False
    with DB(db_path) as db:
        try:
            if job_id is None:
                job_id = plan_job(db, sources_spec, kinds=kinds, days=days,
                                  max_mb=max_mb,
                                  include_downloaded=include_downloaded,
                                  mode=mode, to=to)["job_id"]
            else:
                existing = jobs.job_get(db, job_id)
                if existing is None or existing["kind"] != "downloads":
                    raise RuntimeError(f"задание не найдено или не downloads: {job_id}")
            if retry:
                jobs.retry_failed(db, job_id)
            if not jobs.claim(db, job_id, force=force):
                raise RuntimeError(
                    f"задание {job_id} не взято: выполняется или уже завершено "
                    "(force=True повторно забирает running/failed)")
            claimed = True
            params = jobs.job_get(db, job_id)["params"]
            kind_set = set(_parse_kinds(params.get("kinds") or ("video",)))
            limit_mb = int(params.get("max_mb") or 0)
            run_mode = params.get("mode") or "download"
            dst_root = Path(root) if root is not None else ROOT
            own = client is None
            if own:
                import sync as sync_mod
                client = sync_mod.make_client()
                await client.connect()
            try:
                fwd = None
                if run_mode == "forward":
                    entity, target_id = await _resolve_target(
                        client, params.get("to") or "")
                    fwd = (entity, target_id)
                while jobs.is_active(db, job_id):
                    item = jobs.item_next(db, job_id)
                    if item is None:
                        break
                    try:
                        await _process(db, client, job_id, item, kind_set,
                                       limit_mb, dst_root, fwd)
                    except FloodWaitError as fw:
                        seconds = int(getattr(fw, "seconds", 0) or 0)
                        if seconds <= _FLOOD_INLINE_MAX:
                            await asyncio.sleep(seconds)
                            continue
                        jobs.pause(db, job_id)
                        break
            finally:
                if own:
                    await client.disconnect()
            state = jobs.job_get(db, job_id)["state"]
            rep = jobs.report(db, job_id)
            jobs.finish(db, job_id, "done" if state == "running" else state,
                        result=rep)
            return {"job_id": job_id, "mode": run_mode, **rep}
        except BaseException as exc:
            if claimed:
                jobs.finish(db, job_id, "failed", error=str(exc)[:500])
            raise


def _parse_to(to: str):
    peer = (to or "").strip()
    if not peer:
        raise ValueError("forward: не указан адресат (to: saved | @username | id)")
    if peer.lower() in _TARGET_ALIASES:
        return "me"
    if peer.startswith("@"):
        return peer
    if peer.lstrip("-").isdigit():
        return int(peer)
    raise ValueError(
        f"forward: непонятный адресат {to!r} (saved | @username | числовой id)")


async def _resolve_target(client, to: str):
    peer = _parse_to(to)
    entity = await client.get_entity(peer)
    target_id = getattr(entity, "id", None)
    if target_id is None:
        raise ValueError(f"forward: адресат без id: {to!r}")
    return entity, int(target_id)


def _file_info(msg) -> dict | None:
    media = getattr(msg, "media", None)
    doc = getattr(media, "document", None)
    if doc is None:
        photo = getattr(media, "photo", None)
        if photo is None:
            return None
        return {"kind": "photo", "doc_id": f"photo:{photo.id}",
                "size": None, "mime": "image/jpeg", "filename": None}
    attrs = list(getattr(doc, "attributes", None) or [])
    names = {type(a).__name__ for a in attrs}
    if "DocumentAttributeVideo" in names:
        kind = "videonote" if any(
            getattr(a, "round_message", False)
            for a in attrs if type(a).__name__ == "DocumentAttributeVideo") else "video"
    elif "DocumentAttributeAudio" in names:
        kind = "voice" if any(
            getattr(a, "voice", False)
            for a in attrs if type(a).__name__ == "DocumentAttributeAudio") else "audio"
    else:
        kind = "file"
    filename = next((getattr(a, "file_name", None) for a in attrs
                     if getattr(a, "file_name", None)), None)
    return {"kind": kind, "doc_id": f"doc:{doc.id}",
            "size": getattr(doc, "size", None),
            "mime": getattr(doc, "mime_type", None) or "",
            "filename": filename}


async def _process(db: DB, client, job_id: int, item: dict,
                   kind_set: set, limit_mb: int, root: Path,
                   fwd: tuple | None = None) -> None:
    chat_id, msg_id = int(item["chat_id"]), int(item["msg_id"])

    def mark(state: str, detail: str | None = None) -> None:
        jobs.mark_item(db, job_id, chat_id, msg_id, state, detail)

    try:
        got = await client.get_messages(chat_id, ids=[msg_id])
    except ValueError:
        mark("skipped", "chat-unavailable")
        return
    except FloodWaitError:
        raise
    except Exception as exc:
        mark("failed", str(exc)[:200])
        return
    msg = next(iter(got or []), None)
    if msg is None:
        mark("skipped", "missing")
        return
    info = _file_info(msg)
    if info is None or info["kind"] not in kind_set:
        mark("skipped", "kind")
        return
    if limit_mb and (info["size"] or 0) > limit_mb * 1024 * 1024:
        mark("skipped", "too-big")
        return
    if fwd is not None:
        await _process_forward(db, client, chat_id, msg_id, info, fwd, mark)
        return
    fid = _known_file(db, info["doc_id"])
    if fid is not None:
        _add_source(db, fid, chat_id, msg_id)
        mark("skipped", "duplicate")
        return
    tmp = root / "tmp" / f"{job_id}_{chat_id}_{msg_id}.part"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        await client.download_media(msg, file=str(tmp))
        sha = _sha256(tmp)
        size = tmp.stat().st_size
        fid = _file_by_sha(db, sha)
        if fid is not None:
            tmp.unlink(missing_ok=True)
            _add_source(db, fid, chat_id, msg_id)
            _alias(db, info["doc_id"], fid)
            mark("skipped", "duplicate-content")
            return
        final = root / "files" / sha[:2] / (sha + _ext(info))
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, final)
        fid = _insert_file(db, sha, {**info, "size": size},
                           final.relative_to(root).as_posix())
        _add_source(db, fid, chat_id, msg_id)
        mark("done")
    except FloodWaitError:
        raise
    except Exception as exc:
        mark("failed", str(exc)[:200])


async def _process_forward(db: DB, client, chat_id: int, msg_id: int,
                           info: dict, fwd: tuple, mark) -> None:
    entity, target_id = fwd
    if _known_forward(db, info["doc_id"], target_id) is not None:
        _add_forward_source(db, info["doc_id"], chat_id, msg_id)
        mark("skipped", "duplicate")
        return
    try:
        sent = await client.forward_messages(entity, msg_id, from_peer=chat_id)
    except ChatForwardsRestrictedError:
        mark("skipped", "forwards-restricted")
        return
    except FloodWaitError:
        raise
    except Exception as exc:
        mark("failed", str(exc)[:200])
        return
    new_id = getattr(sent, "id", None)
    if new_id is None and isinstance(sent, list) and sent:
        new_id = getattr(sent[0], "id", None)
    _insert_forward(db, info["doc_id"], target_id, int(new_id or 0), info)
    _add_forward_source(db, info["doc_id"], chat_id, msg_id)
    mark("done")


def _known_file(db: DB, doc_id: str) -> int | None:
    row = db.conn.execute(
        "SELECT id AS file_id FROM media_files WHERE doc_id=? "
        "UNION ALL SELECT file_id FROM media_doc_alias WHERE doc_id=? LIMIT 1",
        (doc_id, doc_id)).fetchone()
    return row["file_id"] if row is not None else None


def _file_by_sha(db: DB, sha: str) -> int | None:
    row = db.conn.execute("SELECT id FROM media_files WHERE sha256=?",
                          (sha,)).fetchone()
    return row["id"] if row is not None else None


def _add_source(db: DB, file_id: int, chat_id: int, msg_id: int) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO media_file_sources(file_id, chat_id, msg_id) "
        "VALUES(?,?,?)", (file_id, chat_id, msg_id))
    db.conn.commit()


def _alias(db: DB, doc_id: str, file_id: int) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO media_doc_alias(doc_id, file_id) VALUES(?,?)",
        (doc_id, file_id))
    db.conn.commit()


def _insert_file(db: DB, sha: str, info: dict, rel_path: str) -> int:
    cur = db.conn.execute(
        "INSERT INTO media_files(sha256, doc_id, kind, mime, filename, size, "
        "path, created_ts) VALUES(?,?,?,?,?,?,?,?)",
        (sha, info["doc_id"], info["kind"], info["mime"], info["filename"],
         info["size"], rel_path, int(time.time())))
    db.conn.commit()
    return int(cur.lastrowid)


def _known_forward(db: DB, doc_id: str, target_chat_id: int) -> int | None:
    row = db.conn.execute(
        "SELECT target_msg_id FROM forward_files "
        "WHERE doc_id=? AND target_chat_id=?", (doc_id, target_chat_id)).fetchone()
    return row["target_msg_id"] if row is not None else None


def _insert_forward(db: DB, doc_id: str, target_chat_id: int, target_msg_id: int,
                    info: dict) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO forward_files(doc_id, target_chat_id, "
        "target_msg_id, kind, size, created_ts) VALUES(?,?,?,?,?,?)",
        (doc_id, target_chat_id, target_msg_id, info["kind"], info.get("size"),
         int(time.time())))
    db.conn.commit()


def _add_forward_source(db: DB, doc_id: str, chat_id: int, msg_id: int) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO forward_sources(doc_id, chat_id, msg_id) "
        "VALUES(?,?,?)", (doc_id, chat_id, msg_id))
    db.conn.commit()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ext(info: dict) -> str:
    name = info.get("filename") or ""
    if "." in name:
        suffix = name.rsplit(".", 1)[1].lower()
        if 1 <= len(suffix) <= 5 and suffix.isalnum():
            return "." + suffix
    return _MIME_EXT.get((info.get("mime") or "").lower()) \
        or _KIND_EXT.get(info["kind"], ".bin")


def status(db: DB, job_id: int = 0) -> dict:
    if job_id:
        job = jobs.job_get(db, job_id)
        if job is None or job["kind"] != "downloads":
            return {"error": f"задание не найдено: {job_id}"}
        return {"job": job, "report": jobs.report(db, job_id)}
    return {"jobs": jobs.job_list(db, kind="downloads"),
            "files": _files_summary(db),
            "forwards": _forwards_summary(db)}


def _files_summary(db: DB) -> dict:
    row = db.conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes "
        "FROM media_files").fetchone()
    by_kind = [dict(r) for r in db.conn.execute(
        "SELECT kind, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes "
        "FROM media_files GROUP BY kind ORDER BY n DESC")]
    return {"count": row["n"], "bytes": row["bytes"], "by_kind": by_kind}


def _forwards_summary(db: DB) -> dict:
    row = db.conn.execute("SELECT COUNT(*) AS n FROM forward_files").fetchone()
    by_target = [dict(r) for r in db.conn.execute(
        "SELECT target_chat_id, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes "
        "FROM forward_files GROUP BY target_chat_id ORDER BY n DESC")]
    return {"count": row["n"], "by_target": by_target}


def files(db: DB, chat_id: int | None = None, limit: int = 100) -> list[dict]:
    sql = ("SELECT f.id, f.sha256, f.kind, f.filename, f.size, f.path, "
           "f.created_ts, "
           "(SELECT COUNT(*) FROM media_file_sources s WHERE s.file_id=f.id) "
           "AS sources FROM media_files f")
    args: list = []
    if chat_id:
        sql += (" WHERE EXISTS (SELECT 1 FROM media_file_sources s "
                "WHERE s.file_id=f.id AND s.chat_id=?)")
        args.append(chat_id)
    sql += " ORDER BY f.id DESC LIMIT ?"
    args.append(min(int(limit), 500))
    return [dict(r) for r in db.conn.execute(sql, args)]


def forwards(db: DB, chat_id: int | None = None, limit: int = 100) -> list[dict]:
    sql = ("SELECT f.doc_id, f.target_chat_id, f.target_msg_id, f.kind, f.size, "
           "f.created_ts, "
           "(SELECT COUNT(*) FROM forward_sources s WHERE s.doc_id=f.doc_id) "
           "AS sources FROM forward_files f")
    args: list = []
    if chat_id:
        sql += (" WHERE EXISTS (SELECT 1 FROM forward_sources s "
                "WHERE s.doc_id=f.doc_id AND s.chat_id=?)")
        args.append(chat_id)
    sql += " ORDER BY f.created_ts DESC, f.doc_id LIMIT ?"
    args.append(min(int(limit), 500))
    return [dict(r) for r in db.conn.execute(sql, args)]


def _runner(job_id: int) -> dict:
    cmd = [sys.executable, str(HERE / "downloads.py"), "run", "--job", str(job_id)]
    kwargs: dict = {"cwd": str(HERE)}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    log = open(HERE / "downloads.log", "ab")
    try:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    except Exception as exc:
        return {"ok": False, "error": f"не удалось запустить загрузчик: {exc}"}
    finally:
        log.close()
    return {"ok": True, "pid": proc.pid}


def _int_or(data: dict, name: str) -> int:
    try:
        return int(data.get(name) or 0)
    except (TypeError, ValueError):
        raise WorkspaceError(f"{name} должен быть числом") from None


def _optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise WorkspaceError("max_mb должен быть числом") from None


def ws_jobs(db, params) -> dict:
    return {"jobs": jobs.job_list(db, kind="downloads")}


def ws_job(db, params) -> dict:
    jid = _int_or(params, "id")
    job = jobs.job_get(db, jid)
    if job is None or job["kind"] != "downloads":
        raise WorkspaceError("Задание не найдено", 404)
    return {"job": job, "report": jobs.report(db, jid)}


def ws_job_create(db, body) -> dict:
    try:
        return plan_job(db,
                        sources_spec=str(body.get("sources") or "watch"),
                        kinds=str(body.get("kinds") or "video"),
                        days=int(body.get("days") or 0),
                        max_mb=_optional_int(body.get("max_mb")),
                        include_downloaded=bool(body.get("include_downloaded")))
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from None


def ws_run(db, body) -> dict:
    jid = _int_or(body, "job_id")
    job = jobs.job_get(db, jid)
    if job is None or job["kind"] != "downloads":
        raise WorkspaceError("Задание не найдено", 404)
    if job["state"] in ("done", "failed", "cancelled"):
        raise WorkspaceError(f"Задание уже завершено: {job['state']}", 409)
    res = _runner(jid)
    if not res.get("ok"):
        raise WorkspaceError(res.get("error") or "загрузчик не запущен", 500)
    return {"ok": True, "job_id": jid, "pid": res.get("pid")}


def ws_forward(db, body) -> dict:
    try:
        res = plan_job(db,
                       sources_spec=str(body.get("sources") or "watch"),
                       kinds=str(body.get("kinds") or "video"),
                       days=int(body.get("days") or 0),
                       max_mb=_optional_int(body.get("max_mb")),
                       include_downloaded=bool(body.get("include_downloaded")),
                       mode="forward",
                       to=str(body.get("to") or ""))
    except (ValueError, TypeError) as exc:
        raise WorkspaceError(str(exc)) from None
    jid = res["job_id"]
    run = _runner(jid)
    if not run.get("ok"):
        raise WorkspaceError(run.get("error") or "загрузчик не запущен", 500)
    return {"ok": True, "job_id": jid, "items": res["items"],
            "pid": run.get("pid")}


def ws_job_pause(db, body) -> dict:
    return {"ok": jobs.pause(db, _int_or(body, "job_id"))}


def ws_job_resume(db, body) -> dict:
    return {"ok": jobs.resume(db, _int_or(body, "job_id"))}


def ws_job_cancel(db, body) -> dict:
    return {"ok": jobs.cancel(db, _int_or(body, "job_id"))}


def ws_files(db, params) -> dict:
    chat = _int_or(params, "chat_id") or None
    return {"files": files(db, chat_id=chat, limit=_int_or(params, "limit") or 100)}


def ws_forwards(db, params) -> dict:
    chat = _int_or(params, "chat_id") or None
    return {"forwards": forwards(db, chat_id=chat,
                                 limit=_int_or(params, "limit") or 100)}


def ws_collections(db, params) -> dict:
    return {"collections": sources.collection_list(db)}


def ws_collection_save(db, body) -> dict:
    ids = body.get("chat_ids")
    if (not isinstance(ids, list)
            or not all(isinstance(c, int) and not isinstance(c, bool) for c in ids)):
        raise WorkspaceError("chat_ids — список чисел")
    try:
        res = sources.collection_save(db, str(body.get("name") or ""), ids)
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from None
    return {"ok": True, **res}


def ws_collection_delete(db, body) -> dict:
    if not sources.collection_delete(db, str(body.get("name") or "")):
        raise WorkspaceError("Подборка не найдена", 404)
    return {"ok": True}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def mcp_run(sources: str = "watch", days: int = 0, kinds: str = "video",
            max_mb: int = 0, include_downloaded: bool = False,
            confirm: bool = False, spawn: bool = True) -> str:
    if not confirm:
        return _json({"error": "confirm=False: скачивание — операция записи "
                               "(диск и трафик), требуется явный confirm=True"})
    try:
        with DB(DB_PATH) as db:
            res = plan_job(db, sources, kinds=kinds, days=days,
                           max_mb=max_mb or None,
                           include_downloaded=include_downloaded)
        if spawn:
            res["runner"] = _runner(res["job_id"])
        res["note"] = ("задание создано; прогресс — tg_downloads_status; "
                       "исполнитель — фоновый процесс downloads.py run --job N")
        return _json(res)
    except Exception as exc:
        return _json({"error": f"downloads: {exc}"})


def mcp_forward(sources: str = "watch", to: str = "", days: int = 0,
                kinds: str = "video", confirm: bool = False,
                spawn: bool = True) -> str:
    if not confirm:
        return _json({"error": "confirm=False: пересылка пишет сообщения в "
                               "указанный чат Telegram, требуется явный "
                               "confirm=True"})
    try:
        with DB(DB_PATH) as db:
            res = plan_job(db, sources, kinds=kinds, days=days, mode="forward",
                           to=to)
        if spawn:
            res["runner"] = _runner(res["job_id"])
        res["note"] = ("задание создано; прогресс — tg_downloads_status; "
                       "исполнитель — фоновый процесс downloads.py run --job N")
        return _json(res)
    except Exception as exc:
        return _json({"error": f"downloads: {exc}"})


def mcp_status(job_id: int = 0) -> str:
    with DB(DB_PATH) as db:
        return _json(status(db, job_id))


def mcp_collections() -> str:
    with DB(DB_PATH) as db:
        return _json({"collections": sources.collection_list(db)})


MODULE = {
    "id": "downloads",
    "version": 2,
    "schema": ensure_schema,
    "workspace": {
        ("GET", "downloads/jobs"): ws_jobs,
        ("GET", "downloads/job"): ws_job,
        ("GET", "downloads/files"): ws_files,
        ("GET", "downloads/forwards"): ws_forwards,
        ("GET", "downloads/collections"): ws_collections,
        ("POST", "downloads/job"): ws_job_create,
        ("POST", "downloads/run"): ws_run,
        ("POST", "downloads/forward"): ws_forward,
        ("POST", "downloads/job/pause"): ws_job_pause,
        ("POST", "downloads/job/resume"): ws_job_resume,
        ("POST", "downloads/job/cancel"): ws_job_cancel,
        ("POST", "downloads/collections"): ws_collection_save,
        ("POST", "downloads/collections/delete"): ws_collection_delete,
    },
    "mcp": [
        {
            "name": "tg_downloads_run",
            "gate": "downloads",
            "description": (
                "Создать задание скачивания медиа из выбранных источников и "
                "запустить фоновый загрузчик. sources через запятую: watch "
                "(выделенные чаты/темы) | all | folder:<id> | collection:<имя> | "
                "chat:<id|@username>; kinds: video (умолчание), videonote, photo; "
                "days — окно в днях, 0 — вся история; max_mb — лимит размера "
                "(0 — TG_DOWNLOADS_MAX_MB); include_downloaded=True — переснять "
                "даже уже скачанное; spawn=False — только "
                "создать задание. Точные дубликаты не качаются повторно: по id "
                "файла Telegram и по sha256 содержимого. Операция записи: "
                "требует confirm=True."
            ),
            "fn": mcp_run,
        },
        {
            "name": "tg_downloads_forward",
            "gate": "forwards",
            "description": (
                "Создать задание пересылки медиа (без скачивания: байты через "
                "клиента не идут) и запустить фоновый исполнитель. Аргументы "
                "как у tg_downloads_run плюс to — адресат: saved (избранное), "
                "@username или числовой id чата. Дедуп — по id файла Telegram: "
                "тот же файл в любой позиции источников не пересылается "
                "повторно; контентный дедуп (перезалитые копии) здесь "
                "невозможен. Пишет сообщения в Telegram: требует "
                "confirm=True и гейт TG_ALLOW_FORWARDS."
            ),
            "fn": mcp_forward,
        },
        {
            "name": "tg_downloads_status",
            "gate": "any",
            "description": (
                "Задания скачивания/пересылки: список с прогрессом и сводные "
                "библиотек (файлы по kind с суммарным размером; пересылки по "
                "адресатам); с job_id — одно задание: параметры, счётчики "
                "пунктов done/skipped/failed и причины пропусков (duplicate, "
                "duplicate-content, too-big, missing, kind, forwards-restricted)."
            ),
            "fn": mcp_status,
        },
        {
            "name": "tg_downloads_collections",
            "gate": "any",
            "description": (
                "Подборки каналов для повторяемых заданий скачивания: имя и "
                "chat_id. Используются в sources как collection:<имя>; watch-лист "
                "и доступ агента не меняют."
            ),
            "fn": mcp_collections,
        },
    ],
    "runner": _runner,
}
