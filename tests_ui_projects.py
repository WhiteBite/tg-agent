#!/usr/bin/env python3
"""Офлайн-тесты панели проектов в UI.

Запускает ui.py как подпроцесс против temp-базы, проверяет:
- GET /api/projects
- POST /api/projects/visit
- авторизацию (token, Content-Type)
- наличие панельного контейнера в index.html
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

HERE = Path(__file__).parent
DB_PATH = HERE / "db.py"


def _make_db(db_path: Path) -> None:
    sys.path.insert(0, str(HERE))
    from db import DB  # noqa: E402

    now = int(time.time())
    with DB(db_path) as db:
        db.conn.execute("DELETE FROM watch")
        db.conn.execute("DELETE FROM entities")
        db.conn.execute("DELETE FROM entity_events")
        db.conn.execute("DELETE FROM meta")
        db.conn.execute("DELETE FROM messages")
        db.conn.execute("DELETE FROM chats")
        db.conn.commit()

        db.conn.execute(
            "INSERT INTO chats(id, title, kind, is_forum, members, synced_at) "
            "VALUES(-100111, 'Стройка / Объекд', 'supergroup', 1, 5, ?)",
            (now,))
        db.conn.execute(
            "INSERT INTO chats(id, title, kind, is_forum, members, synced_at) "
            "VALUES(-100222, 'Сервисы', 'supergroup', 1, 3, ?)",
            (now,))
        db.conn.commit()

        db.watch_add(-100111, 5, project="Объекд", alias="Стройка-релизы")
        db.watch_add(-100111, 1, project="Объекд", alias="Общий")
        db.watch_add(-100222, 42, project="сервисы", alias="AI-сервисы")
        db.watch_add(-100222, 1, project=None, alias="Без проекта")

        _seed_entity(db, 1, "Объекд", now, "task", "open", owner="Иван",
                     due_ts=now - 86400)
        _seed_entity(db, 2, "Объекд", now, "task", "in_progress", owner="Петя")
        _seed_entity(db, 3, "Объекд", now, "task", "blocked", owner="Иван",
                     due_ts=now + 86400)
        _seed_entity(db, 4, "Объекд", now, "task", "done", owner="Петя",
                     closed_ts=now - 5000)
        _seed_entity(db, 5, "Объекд", now, "question", "open")
        _seed_entity(db, 6, "Объекд", now, "question", "done", closed_ts=now - 100)
        _seed_entity(db, 7, "Объекд", now, "promise", "open", owner="Иван")
        _seed_entity(db, 8, "Объекд", now, "promise", "open")
        _seed_entity(db, 9, "Объекд", now, "promise", "open", due_ts=now - 3600)

        _seed_entity(db, 10, "сервисы", now, "task", "open", owner="Аня")

        db.conn.execute(
            "INSERT INTO entity_events(entity_id, ts, op) VALUES(1, ?, 'create')",
            (now - 1000,))
        db.conn.execute(
            "INSERT INTO entity_events(entity_id, ts, op, field, to_val) "
            "VALUES(1, ?, 'update', 'status', 'open')",
            (now - 500,))


def _seed_entity(db, eid: int, project: str, now: int, kind: str,
                 status: str, owner: str | None = None,
                 due_ts: int | None = None, closed_ts: int | None = None) -> None:
    db.conn.execute(
        "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, status, "
        "owner, due_ts, project, created_ts, updated_ts, closed_ts) "
        "VALUES(?, 'construction', -100111, 5, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (eid, kind, f"{kind} #{eid}", status, owner, due_ts, project,
         now - 2000, now - 1000, closed_ts))


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get_json(url: str, token: str | None = None) -> dict:
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    return json.loads(urllib.request.urlopen(url, timeout=3).read())


def _post_json(url: str, body: dict, token: str | None = None,
               ctype: str = "application/json") -> tuple[int, dict]:
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        return e.code, body


FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}"
          + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def test_projects_api() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "proj.sqlite")
        _make_db(Path(db_path))

        port = _find_free_port()
        env = {**os.environ, "TG_DB": db_path, "PORT": str(port),
               "HOST": "127.0.0.1", "TG_UI_TOKEN": "t9"}
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(HERE / "ui.py")],
            env=env, cwd=str(HERE),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        try:
            base = f"http://127.0.0.1:{port}"

            for _ in range(60):
                try:
                    urllib.request.urlopen(f"{base}/api/poll?since=0&token=t9",
                                           timeout=0.5)
                    break
                except Exception:
                    time.sleep(0.15)
            else:
                check(False, "startup: сервер не ответил за 9с")
                return

            # --- auth gates ---
            try:
                urllib.request.urlopen(f"{base}/api/projects", timeout=2)
                check(False, "401: без токена должен быть 401")
            except urllib.error.HTTPError as e:
                check(e.code == 401, "auth: GET /api/projects без token -> 401",
                      str(e.code))

            _, no_ct = _post_json(f"{base}/api/projects/visit",
                                  {"project": "x"}, token="t9",
                                  ctype="text/plain")
            check(415, "csrf: POST без application/json -> 415",
                  str(no_ct))

            # --- GET /api/projects ---
            data = _get_json(f"{base}/api/projects", token="t9")
            check("projects" in data,
                  "GET: /api/projects возвращает поле projects",
                  str(list(data)[:5]))
            projects = data["projects"]
            names = [p["name"] for p in projects]
            check("Объекд" in names and "сервисы" in names,
                  "GET: в проектах есть Объекд и сервисы",
                  str(names))
            check(all(p.get("name") != "None" for p in projects),
                  "GET: project=None не попал в список",
                  str([p["name"] for p in projects]))

            obj = next(p for p in projects if p["name"] == "Объекд")
            check(obj["open"] == 7,
                  "GET: Объекд open=7 (все незакрытые)",
                  str({k: obj[k] for k in ["open", "in_progress", "blocked", "done",
                                           "overdue", "open_questions",
                                           "promises_no_due", "promises_no_owner"]}))
            check(obj["in_progress"] == 1, "GET: Объекд in_progress=1")
            check(obj["blocked"] == 1, "GET: Объекд blocked=1")
            check(obj["done"] == 2, "GET: Объекд done=2 (#4 task, #6 question)")
            check(obj["overdue"] == 2,
                  "GET: Объекд просрочено=2 (#1 task, #9 promise)",
                  str(obj["overdue"]))
            check(obj["open_questions"] == 1,
                  "GET: Объекд открытых вопросов=1",
                  str(obj["open_questions"]))
            check(obj["promises_no_due"] == 2,
                  "GET: Объекд обещаний без срока=2 (#7, #8)",
                  str(obj["promises_no_due"]))
            check(obj["promises_no_owner"] == 2,
                  "GET: Объекд обещаний без ответственного=2 (#8, #9)",
                  str(obj["promises_no_owner"]))
            check(obj.get("last_visit") is None,
                  "GET: last_visit=None до визита")
            check(obj.get("new_since_visit") == 0,
                  "GET: new_since_visit=0 когда не было визита",
                  str(obj.get("new_since_visit")))

            serv = next(p for p in projects if p["name"] == "сервисы")
            check(serv["open"] == 1,
                  "GET: сервисы open=1", str(serv))

            # --- POST /api/projects/visit ---
            _, v1 = _post_json(f"{base}/api/projects/visit",
                               {"project": "Объекд"}, token="t9")
            check(v1.get("ok") is True and v1.get("project") == "Объекд",
                  "POST: /api/projects/visit -> ok",
                  str(v1))

            _, bad = _post_json(f"{base}/api/projects/visit",
                                {"project": "Несуществующий"}, token="t9")
            check(bad.get("ok") is False and bad.get("error"),
                  "POST: неизвестный проект -> ошибка",
                  str(bad))

            _, missing = _post_json(f"{base}/api/projects/visit",
                                    {}, token="t9")
            check(missing.get("ok") is False,
                  "POST: без поля project -> ошибка",
                  str(missing))

            # --- GET after visit ---
            data2 = _get_json(f"{base}/api/projects", token="t9")
            obj2 = next(p for p in data2["projects"] if p["name"] == "Объекд")
            check(isinstance(obj2.get("last_visit"), int),
                  "GET после визита: last_visit стало int",
                  str(obj2.get("last_visit")))
            check(obj2.get("new_since_visit") == 0,
                  "GET после визита: new_since_visit==0",
                  str(obj2.get("new_since_visit")))

            # --- HTML содержит контейнер панели ---
            html = urllib.request.urlopen(f"{base}/", timeout=2).read().decode()
            check("api/projects" in html,
                  "HTML: index.html содержит api/projects")
            check("projpanel" in html or "project-panel" in html
                  or "prj-" in html,
                  "HTML: index.html содержит контейнер панели проектов")

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    print("=== tests_ui_projects.py ===")
    test_projects_api()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES:")
        for f in FAILURES:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("Все проверки пройдены.")