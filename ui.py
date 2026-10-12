#!/usr/bin/env python3
"""Веб-визуал «как в тг» поверх локального индекса: чаты → темы → сообщения.

Отмечаешь темы звёздочкой (или «отметить все»), даёшь им алиасы — и это сразу
видит агент в opencode: UI пишет в ту же таблицу watch, которую читает server.py.

Запуск:  python3 ui.py            # http://127.0.0.1:8899
         TG_DB=... PORT=9000 HOST=0.0.0.0 python3 ui.py

Ничего не отправляет в Telegram; кнопка «синхронизировать» просто дёргает
sync.py --watched в фоне (нужны креды, без них покажет ошибку в статусе).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from db import DB

HERE = Path(__file__).parent
DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))
PORT = int(os.environ.get("PORT", "8899"))
HOST = os.environ.get("HOST", "127.0.0.1")
# если UI торчит наружу (HOST=0.0.0.0) — задай TG_UI_TOKEN, все /api/* станут требовать ?token=
TOKEN = os.environ.get("TG_UI_TOKEN", "")


def _authed(q) -> bool:
    return (not TOKEN) or (q.get("token", [""])[0] == TOKEN)

_sync = {"running": False, "lines": [], "code": None}
_lock = threading.Lock()


def _db() -> DB:
    return DB(DB_PATH)


def build_state() -> dict:
    with _db() as db:
        chats = db.chats(limit=500)
        for c in chats:
            row = db.conn.execute(
                "SELECT text, sender_name, ts FROM messages WHERE chat_id=? "
                "ORDER BY ts DESC, msg_id DESC LIMIT 1", (c["id"],)
            ).fetchone()
            c["last_text"] = row["text"] if row else None
            c["last_sender"] = row["sender_name"] if row else None
            c["last_ts"] = row["ts"] if row else c.get("last_ts")
            c["topics"] = db.topics(c["id"])
            c["watched"] = db.watch_list(c["id"])
            c["pins"] = db.pins(c["id"], limit=10)
            c["new_total"] = sum(w.get("new_since_read") or 0 for w in c["watched"])
        stats = dict(db.conn.execute(
            "SELECT (SELECT COUNT(*) FROM chats) chats, "
            "(SELECT COUNT(*) FROM topics) topics, "
            "(SELECT COUNT(*) FROM messages) messages, "
            "(SELECT COUNT(*) FROM watch) watched, "
            "(SELECT MAX(synced_at) FROM chats) synced_at"
        ).fetchone())
        folders = db.folders_list()
    return {"chats": chats, "stats": stats, "folders": folders}


def run_sync() -> None:
    with _lock:
        if _sync["running"]:
            return
        _sync.update(running=True, lines=[], code=None)
    env = dict(os.environ, TG_DB=DB_PATH)
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "sync.py"), "--watched"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )

    def pump() -> None:
        for line in proc.stdout or []:
            with _lock:
                _sync["lines"].append(line.rstrip())
                _sync["lines"] = _sync["lines"][-40:]
        proc.wait()
        with _lock:
            _sync["running"] = False
            _sync["code"] = proc.returncode

    threading.Thread(target=pump, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # тихо
        pass

    def _send(self, data: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8", code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/") and not _authed(q):
            self._json({"error": "нужен ?token=<TG_UI_TOKEN>"}, code=401)
            return
        if u.path in ("/", "/index.html"):
            html = (HERE / "ui.html").read_bytes()
            self._send(html, "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self._json(build_state())
        elif u.path == "/api/messages":
            chat = int(q["chat_id"][0]); topic = int(q.get("topic_id", ["0"])[0])
            before = int(q.get("before", ["0"])[0])
            around = int(q.get("around", ["0"])[0])
            limit = min(int(q.get("limit", ["60"])[0]), 300)
            with _db() as db:
                if around:
                    rows = db.conn.execute(
                        """SELECT msg_id, topic_id, ts, date_iso, sender_name, is_service,
                                  text, media, reply_to FROM messages
                           WHERE chat_id=? AND COALESCE(topic_id,1)=? AND ts<=?
                           ORDER BY msg_id DESC LIMIT ?""",
                        (chat, 1 if topic in (0, 1) else topic, around, limit),
                    ).fetchall()
                else:
                    rows = db.conn.execute(
                        """SELECT msg_id, topic_id, ts, date_iso, sender_name, is_service,
                                  text, media, reply_to FROM messages
                           WHERE chat_id=? AND COALESCE(topic_id,1)=? AND (?=0 OR msg_id<?)
                           ORDER BY msg_id DESC LIMIT ?""",
                        (chat, 1 if topic in (0, 1) else topic, before, before, limit),
                    ).fetchall()
            msgs = [dict(r) for r in reversed(rows)]
            self._json({"chat_id": chat, "topic_id": topic,
                        "messages": msgs, "oldest": msgs[0]["msg_id"] if msgs else None,
                        "count": len(msgs)})
        elif u.path == "/api/search":
            query = q.get("q", [""])[0]
            chat = q.get("chat_id", [None])[0]
            topic = q.get("topic_id", [None])[0]
            with _db() as db:
                res = db.search_fts(
                    query,
                    chat_id=int(chat) if chat else None,
                    topic_id=int(topic) if topic else None,
                    limit=min(int(q.get("limit", ["50"])[0]), 200),
                )
            hits = res.get("hits") or []
            self._json({"engine": res.get("engine"), "query": query,
                        "count": len(hits), "hits": hits})
        elif u.path == "/api/poll":
            since = int(q.get("since", ["0"])[0])
            with _db() as db:
                rows = db.conn.execute(
                    """SELECT chat_id, msg_id, topic_id, ts, sender_name, text, is_service
                       FROM messages WHERE ts > ? ORDER BY msg_id LIMIT 300""", (since,)
                ).fetchall()
            self._json({"since": since, "messages": [dict(r) for r in rows]})
        elif u.path == "/api/digests":
            chat = q.get("chat_id", [None])[0]
            topic = q.get("topic_id", [None])[0]
            with _db() as db:
                rows = db.digests_list(
                    int(chat) if chat else None,
                    int(topic) if topic else None, 3)
            self._json({"digests": rows})
        elif u.path == "/api/sync_status":
            with _lock:
                self._json({"running": _sync["running"], "code": _sync["code"],
                            "lines": _sync["lines"][-20:]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path.startswith("/api/") and not _authed(parse_qs(u.query)):
            self._json({"error": "нужен ?token=<TG_UI_TOKEN>"}, code=401)
            return
        b = self._body()
        try:
            if u.path == "/api/watch":
                with _db() as db:
                    db.watch_add(int(b["chat_id"]), int(b["topic_id"]),
                                 alias=b.get("alias") or None,
                                 pinned=bool(b.get("pinned")))
                self._json({"ok": True})
            elif u.path == "/api/unwatch":
                with _db() as db:
                    ok = db.watch_remove(int(b["chat_id"]), int(b["topic_id"]))
                self._json({"ok": ok})
            elif u.path == "/api/alias":
                alias = b.get("alias")
                with _db() as db:
                    db.conn.execute(
                        "UPDATE watch SET alias=? WHERE chat_id=? AND topic_id=?",
                        (alias or None, int(b["chat_id"]),
                         1 if int(b["topic_id"]) in (0, 1) else int(b["topic_id"])),
                    )
                    db.conn.commit()
                self._json({"ok": True})
            elif u.path == "/api/pin":
                with _db() as db:
                    db.conn.execute(
                        "UPDATE watch SET pinned=? WHERE chat_id=? AND topic_id=?",
                        (int(bool(b.get("pinned"))), int(b["chat_id"]),
                         1 if int(b["topic_id"]) in (0, 1) else int(b["topic_id"])),
                    )
                    db.conn.commit()
                self._json({"ok": True})
            elif u.path == "/api/mark_read":
                chat, topic = int(b["chat_id"]), int(b["topic_id"])
                with _db() as db:
                    row = db.conn.execute(
                        "SELECT MAX(msg_id) m FROM messages WHERE chat_id=? AND COALESCE(topic_id,1)=?",
                        (chat, 1 if topic in (0, 1) else topic)).fetchone()
                    ok = bool(row and row["m"] and db.watch_mark_read(chat, topic, row["m"]))
                self._json({"ok": ok})
            elif u.path == "/api/sync":
                run_sync()
                self._json({"ok": True, "started": True})
            else:
                self._json({"error": "not found"}, 404)
        except (KeyError, ValueError, TypeError) as e:
            self._json({"error": str(e)}, 400)


def main() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"TG-агент визуал: http://{HOST}:{PORT}  (индекс: {DB_PATH})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
