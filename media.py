#!/usr/bin/env python3
"""Транскрипция медиа из индекса (голосовые, документы).

    MEDIA_PROVIDER=stub python3 media.py

sync.py и live.py ставят голосовые и документы в очередь (таблица
media_assets, status=pending); этот воркер разгребает её провайдером:
stub пишет детерминированную заглушку, openai шлёт multipart-запрос в
{MEDIA_URL}/audio/transcriptions (MEDIA_KEY, MEDIA_MODEL, по умолчанию
whisper-1). Готовые транскрипции extract.py подмешивает в промпт
извлечения сущностей. Скачивание файла из Telegram — отдельный инкремент,
поэтому openai-провайдер без готовых байтов поднимает NotImplementedError.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB, clean_text  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

MEDIA_KINDS = ("voice", "document")


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


def provider_stub(chat_id: int, msg_id: int, kind: str) -> dict:
    return {"text": f"[транскрипция {kind} {chat_id}/{msg_id}]", "engine": "stub"}


def provider_openai(chat_id: int, msg_id: int, kind: str,
                    audio: bytes | None = None) -> dict:
    if audio is None:
        raise NotImplementedError("скачивание медиа из Telegram: следующий инкремент")
    url = os.environ["MEDIA_URL"].rstrip("/") + "/audio/transcriptions"
    model = os.environ.get("MEDIA_MODEL", "whisper-1")
    boundary = "----tg-agent-media"
    fname = f"{msg_id}.ogg" if kind == "voice" else f"{msg_id}.bin"
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'
        .encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
         f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode(),
        audio,
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    key = os.environ.get("MEDIA_KEY")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return {"text": data.get("text") or "", "engine": model}


def run(db_path: str = DB_PATH, provider=None) -> dict:
    if provider is None:
        name = os.environ.get("MEDIA_PROVIDER", "stub")
        if name == "stub":
            provider = provider_stub
        elif name == "openai":
            provider = provider_openai
        else:
            raise RuntimeError(f"неизвестный MEDIA_PROVIDER: {name}")
    done = failed = 0
    with DB(db_path) as db:
        rows = db.conn.execute(
            "SELECT chat_id, msg_id, kind FROM media_assets WHERE status='pending' "
            "ORDER BY chat_id, msg_id").fetchall()
        for r in rows:
            now = int(time.time())
            try:
                res = provider(r["chat_id"], r["msg_id"], r["kind"])
            except Exception:
                db.conn.execute(
                    "UPDATE media_assets SET status='failed', ts=? "
                    "WHERE chat_id=? AND msg_id=?",
                    (now, r["chat_id"], r["msg_id"]))
                failed += 1
            else:
                db.conn.execute(
                    "UPDATE media_assets SET status='done', text=?, engine=?, ts=? "
                    "WHERE chat_id=? AND msg_id=?",
                    (clean_text(res.get("text")), res.get("engine"), now,
                     r["chat_id"], r["msg_id"]))
                done += 1
            db.conn.commit()
    return {"done": done, "failed": failed}


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
    res = run()
    print(f"медиа обработано: {res['done']}, сбоев: {res['failed']}")


if __name__ == "__main__":
    main()
