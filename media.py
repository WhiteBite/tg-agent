#!/usr/bin/env python3
"""Транскрипция медиа из индекса (голосовые, аудио-документы).

    MEDIA_PROVIDER=stub python3 media.py                # заглушка без сети
    MEDIA_PROVIDER=openai python3 media.py --limit 50   # скачивание + транскрипция

sync.py и live.py ставят в очередь (таблица media_assets, status=pending) только
аудио — голосовые и аудио-документы (не-аудио документы не попадают); воркер
рефетчит сообщение из Telegram по id
(file_reference живёт только на свежем объекте), классифицирует аудио по
атрибутам (DocumentAttributeVoice/Audio, mime audio/* как фолбэк) и до
скачивания отсекает не-аудио, >10MB и >5мин статусом skipped; удалённые
сообщения — missing, сбой провайдера — failed. openai-провайдер — DashScope
qwen3-asr-flash через chat/completions: аудио инлайнится base64 data-URL,
asr_options идёт верхним уровнем. Готовые транскрипции extract.py подмешивает
в промпт извлечения (и evidence-проверка учитывает их текст).
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB, clean_text  # noqa: E402
from llm_http import call_with_deadline, key_list, post_json_keys  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

MEDIA_KINDS = ("voice", "document")
_MAX_BYTES = 10 * 1024 * 1024
_MAX_SECONDS = 300


def enqueue(db: DB, chat_id: int, msg_id: int, kind: str | None) -> None:
    """Ставит медиа в очередь транскрипции; не должна ронять синк/live."""
    if kind not in MEDIA_KINDS:
        return
    try:
        db.conn.execute(
            "INSERT OR IGNORE INTO media_assets(chat_id, msg_id, kind, status) "
            "VALUES(?,?,?,'pending')",
            (chat_id, msg_id, kind))
        db.conn.commit()
    except Exception:
        pass


def provider_stub(chat_id: int, msg_id: int, kind: str,
                  audio: bytes | None = None, mime: str | None = None) -> dict:
    return {"text": f"[транскрипция {kind} {chat_id}/{msg_id}]", "engine": "stub"}


def provider_openai(chat_id: int, msg_id: int, kind: str,
                    audio: bytes | None = None, mime: str | None = None) -> dict:
    if audio is None:
        raise ValueError("openai-провайдер медиа требует скачанные байты (audio)")
    model = os.environ.get("MEDIA_MODEL", "qwen3-asr-flash")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [{
            "type": "input_audio",
            "input_audio": {"data": f"data:{mime or 'audio/ogg'};base64,"
                                    f"{base64.b64encode(audio).decode()}"}}]}],
        "stream": False,
        "asr_options": {"enable_itn": False},
    }
    url = os.environ["MEDIA_URL"].rstrip("/") + "/chat/completions"
    deadline = float(os.environ.get("MEDIA_TIMEOUT") or 300)
    data = call_with_deadline(post_json_keys, deadline, url,
                              key_list(os.environ.get("MEDIA_KEY")), payload, 120)
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return {"text": text, "engine": model}


def _resolve(provider):
    if provider is None:
        provider = os.environ.get("MEDIA_PROVIDER", "stub")
    if isinstance(provider, str):
        if provider == "stub":
            return provider_stub
        if provider == "openai":
            return provider_openai
        raise RuntimeError(f"неизвестный MEDIA_PROVIDER: {provider}")
    return provider


def _mark(db: DB, chat_id: int, msg_id: int, status: str,
          text: str | None = None, engine: str | None = None) -> None:
    db.conn.execute(
        "UPDATE media_assets SET status=?, text=?, engine=?, ts=? "
        "WHERE chat_id=? AND msg_id=?",
        (status, text, engine, int(time.time()), chat_id, msg_id))
    db.conn.commit()


def _audio_info(msg) -> dict | None:
    doc = getattr(getattr(msg, "media", None), "document", None)
    if doc is None:
        return None
    attrs = getattr(doc, "attributes", None) or []
    mime = getattr(doc, "mime_type", "") or ""
    is_audio = mime.startswith("audio/") or bool(
        {type(a).__name__ for a in attrs}
        & {"DocumentAttributeVoice", "DocumentAttributeAudio"})
    if not is_audio:
        return None
    duration = next((a.duration for a in attrs
                     if getattr(a, "duration", None) is not None), None)
    return {"size": getattr(doc, "size", None), "duration": duration, "mime": mime}


def is_transcribable(msg) -> bool:
    return _audio_info(msg) is not None


def _pending(db: DB, limit: int | None, kinds: tuple) -> list[dict]:
    marks = ",".join("?" * len(kinds))
    sql = ("SELECT chat_id, msg_id, kind FROM media_assets "
           f"WHERE status='pending' AND kind IN ({marks}) "
           "ORDER BY chat_id, msg_id")
    args: list = [*kinds]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(r) for r in db.conn.execute(sql, args)]


def _run_offline(db_path, prov, limit: int | None, kinds: tuple) -> dict:
    done = failed = 0
    with DB(db_path) as db:
        for r in _pending(db, limit, kinds):
            try:
                res = prov(r["chat_id"], r["msg_id"], r["kind"])
            except Exception:
                _mark(db, r["chat_id"], r["msg_id"], "failed")
                failed += 1
            else:
                _mark(db, r["chat_id"], r["msg_id"], "done",
                      clean_text(res.get("text")), res.get("engine"))
                done += 1
    return {"done": done, "failed": failed}


async def _run_with_client(db_path, prov, client, limit: int, kinds: tuple) -> dict:
    stats = {"done": 0, "skipped": 0, "missing": 0, "failed": 0}
    own = client is None
    if own:
        import sync as sync_mod
        client = sync_mod.make_client()
        await client.connect()
    try:
        with DB(db_path) as db:
            by_chat: dict[int, list[dict]] = {}
            for r in _pending(db, limit, kinds):
                by_chat.setdefault(r["chat_id"], []).append(r)
            for chat_id, items in by_chat.items():
                try:
                    got = await client.get_messages(
                        chat_id, ids=[i["msg_id"] for i in items])
                except ValueError:
                    # кэш сессии не знает entity чата — сообщения недоступны
                    for it in items:
                        _mark(db, chat_id, it["msg_id"], "missing")
                        stats["missing"] += 1
                    continue
                found = {m.id: m for m in (got or []) if m is not None}
                for it in items:
                    msg = found.get(it["msg_id"])
                    if msg is None:
                        _mark(db, chat_id, it["msg_id"], "missing")
                        stats["missing"] += 1
                        continue
                    info = _audio_info(msg)
                    if (info is None or (info["size"] or 0) > _MAX_BYTES
                            or (info["duration"] or 0) > _MAX_SECONDS):
                        _mark(db, chat_id, it["msg_id"], "skipped")
                        stats["skipped"] += 1
                        continue
                    try:
                        buf = io.BytesIO()
                        await client.download_media(msg, file=buf)
                        res = prov(chat_id, it["msg_id"], it["kind"],
                                   audio=buf.getvalue(), mime=info["mime"])
                    except Exception:
                        _mark(db, chat_id, it["msg_id"], "failed")
                        stats["failed"] += 1
                    else:
                        _mark(db, chat_id, it["msg_id"], "done",
                              clean_text(res.get("text")), res.get("engine"))
                        stats["done"] += 1
    finally:
        if own:
            await client.disconnect()
    return stats


async def run_async(db_path: str = DB_PATH, provider=None, client=None,
                    limit: int | None = None, kinds=None) -> dict:
    prov = _resolve(provider)
    if client is None and prov is not provider_openai:
        return _run_offline(db_path, prov, limit, tuple(kinds or MEDIA_KINDS))
    limit = limit or int(os.environ.get("MEDIA_LIMIT") or 25)
    kinds = tuple(kinds or (os.environ.get("MEDIA_KINDS")
                            or "voice,document").split(","))
    return await _run_with_client(db_path, prov, client, limit, kinds)


def run(db_path: str = DB_PATH, provider=None, client=None,
        limit: int | None = None, kinds=None) -> dict:
    prov = _resolve(provider)
    if client is None and prov is not provider_openai:
        return _run_offline(db_path, prov, limit, tuple(kinds or MEDIA_KINDS))
    return asyncio.run(run_async(db_path, provider=prov, client=client,
                                 limit=limit, kinds=kinds))


def transcript_map(db: DB, chat_id: int, msg_ids: list[int]) -> dict[int, str]:
    if not msg_ids:
        return {}
    marks = ",".join("?" * len(msg_ids))
    rows = db.conn.execute(
        f"SELECT msg_id, text FROM media_assets "
        f"WHERE chat_id=? AND status='done' AND msg_id IN ({marks})",
        [chat_id] + list(msg_ids)).fetchall()
    return {r["msg_id"]: r["text"] for r in rows if r["text"]}


def main() -> None:
    args = sys.argv[1:]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    kinds = (args[args.index("--kinds") + 1].split(",")
             if "--kinds" in args else None)
    res = run(limit=limit, kinds=kinds)
    print(f"медиа: done={res.get('done', 0)} skipped={res.get('skipped', 0)} "
          f"missing={res.get('missing', 0)} failed={res.get('failed', 0)}")


if __name__ == "__main__":
    main()
