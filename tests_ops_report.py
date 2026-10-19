#!/usr/bin/env python3
"""Операционные проверки автономки: notify изнутри живого event loop,
бюджет шагов конвейера, lock разового прогона, дедуп отправки сводки,
зависшие вопросы, ops_rejected в отчёте, doctor --quality.

Герметично: своего .env нет (TG_ENV_FILE=""), свой временный каталог,
сеть не трогается (транспорт notify подменён, провайдеры stub).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB  # noqa: E402

NOW = datetime.now(timezone.utc)
FAILURES: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="tg_ops_report_"))


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


class User:
    def __init__(self, uid: int, first_name: str):
        self.id, self.first_name = uid, first_name
        self.last_name, self.username = "", ""


class Channel:
    def __init__(self, cid: int, title: str, forum: bool = False):
        self.id, self.title, self.username = cid, title, None
        self.megagroup, self.broadcast, self.forum = True, False, forum
        self.participants_count = 10


class Msg:
    def __init__(self, mid: int, date, sender, text):
        self.id, self.date, self.message = mid, date, text
        self.sender_id = sender.id if sender else None
        self._sender = sender
        self.reply_to, self.out, self.action = None, False, None
        self.views, self.forwards, self.reactions = None, None, None
        self.pinned, self.fwd_from, self.media = False, None, None
        self.edit_date = None


class FakeClient:
    def __init__(self, groups: dict[int, dict]):
        self.groups = groups

    async def get_entity(self, peer):
        k = int(getattr(peer, "id", peer))
        if k not in self.groups and k > 0:
            k = -1000000000000 - k
        return self.groups[k]["entity"]

    async def iter_messages(self, entity, limit=None, offset_id=0, reverse=False, **kw):
        msgs = self.groups[int(getattr(entity, "id", entity))]["messages"]
        if offset_id:
            msgs = [m for m in msgs if m.id > offset_id]
        msgs = sorted(msgs, key=lambda m: m.id, reverse=not reverse)
        if limit:
            msgs = msgs[:limit]
        for m in msgs:
            yield m

    async def get_messages(self, entity, limit=None, filter=None, ids=None, **kw):
        return []

    async def __call__(self, req):
        name = type(req).__name__
        assert name == "GetDialogFiltersRequest", name
        return []


def _seed_watch_db(db: Path, chat_id: int = -100900, project: str | None = None) -> None:
    with DB(db) as d:
        d.upsert_chat(chat_id, "Операции", None, "supergroup", False, 2)
        d.watch_add(chat_id, 1, project=project)


def test_notify_event_loop() -> None:
    print("=== notify: отправка изнутри живого event loop и снаружи ===")
    import notify as notify_mod

    calls: list[list[str]] = []

    async def fake_transport(messages: list[str]) -> tuple[int, list[str]]:
        calls.append(list(messages))
        return len(messages), []

    saved = notify_mod._telegram_send
    notify_mod._telegram_send = fake_transport
    try:
        async def scenario() -> dict:
            return notify_mod._send(["изнутри loop"])

        res = asyncio.run(scenario())
        check(res.get("ok") is True and res.get("sent") == 1 and len(calls) == 1,
              "notify._send изнутри running loop: без RuntimeError про event loop",
              str(res))
        res2 = notify_mod._send(["снаружи loop"])
        check(res2.get("ok") is True and res2.get("sent") == 1 and len(calls) == 2,
              "notify._send standalone: работает и без живого loop", str(res2))
    finally:
        notify_mod._telegram_send = saved


def test_pipeline_send_end_to_end() -> None:
    print("=== pipeline --send: полный прогон на фейк-клиенте со стаб-транспортом ===")
    import notify as notify_mod
    import pipeline as pipeline_mod

    db = TMP / "morning.sqlite"
    _seed_watch_db(db, project="утро")
    users = {1: User(1, "Анна")}
    msgs = [Msg(i, NOW - timedelta(minutes=30 - i), users[1], f"сообщение {i}")
            for i in range(1, 6)]
    client = FakeClient({-100900: {"entity": Channel(-100900, "Операции"), "messages": msgs}})
    now = int(time.time())
    with DB(db) as d:
        d.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "due_ts, project, created_ts, updated_ts) "
            "VALUES('tg', -100900, 1, 'task', 'Утренняя задача', 'open', "
            "?, 'утро', ?, ?)",
            (now - 7200, now - 7200, now - 7200))
        d.conn.commit()

    sent: list[str] = []

    async def fake_transport(messages: list[str]) -> tuple[int, list[str]]:
        sent.extend(messages)
        return len(messages), []

    saved_t = notify_mod._telegram_send
    notify_mod._telegram_send = fake_transport
    os.environ["TG_ALLOW_SEND"] = "1"
    os.environ.pop("TG_MONITOR_FORCE", None)
    try:
        rep = asyncio.run(pipeline_mod.run(str(db), client=client, send=True))
    finally:
        notify_mod._telegram_send = saved_t
        os.environ.pop("TG_ALLOW_SEND", None)

    mon = rep.get("monitor", {})
    check(mon.get("ok") is True and "event loop" not in str(mon.get("error", "")),
          "pipeline --send: monitor-шаг не падает на asyncio.run изнутри loop",
          str(mon)[:200])
    check(mon.get("notify", {}).get("ok") is True and sent
          and any("Мониторинг" in ln for ln in sent),
          "pipeline --send: сводка дошла до стаб-транспорта",
          str(mon.get("notify"))[:200])
    check(rep.get("sync", {}).get("ok") is True and rep["sync"]["new_messages"] == 5,
          "pipeline --send: sync на фейк-клиенте отработал", str(rep.get("sync")))


def test_step_budget() -> None:
    print("=== бюджет шага: зависший шаг становится провалом, а не зависанием ===")
    import extract as extract_mod
    import pipeline as pipeline_mod

    db = TMP / "budget.sqlite"
    _seed_watch_db(db)
    release = threading.Event()

    def hanging_extract(*a, **k):
        release.wait(timeout=30)
        return []

    saved = extract_mod.run
    extract_mod.run = hanging_extract
    os.environ["PIPELINE_STEP_BUDGET"] = "1"
    try:
        t0 = time.monotonic()
        rep = asyncio.run(pipeline_mod.run(str(db), skip_sync=True, skip_media=True))
        took = time.monotonic() - t0
    finally:
        extract_mod.run = saved
        os.environ.pop("PIPELINE_STEP_BUDGET", None)
        release.set()

    ex = rep.get("extract", {})
    check(ex.get("ok") is False and "таймаут" in str(ex.get("error", "")),
          "бюджет: зависший extract помечен проваленным с таймаутом", str(ex))
    check(took < 10, "бюджет: run вернулся, а не завис", f"{took:.1f}с")
    check(rep.get("monitor", {}).get("ok") is True,
          "бюджет: шаги после проваленного всё равно отработали",
          str(rep.get("monitor"))[:120])


def test_oneshot_lock() -> None:
    print("=== lock: разовый прогон не соревнуется с держателем lockfile ===")
    import pipeline as pipeline_mod
    import proclock

    db = TMP / "lock.sqlite"
    _seed_watch_db(db)
    fh = proclock.acquire_instance_lock(str(db) + ".pipeline.lock")
    check(fh is not None, "lock: тестовый захват lockfile успешен")
    try:
        rep = asyncio.run(pipeline_mod.run(str(db), skip_sync=True, skip_media=True))
        check(rep.get("already_running") is True and "monitor" not in rep,
              "lock: run при занятом lockfile выходит без прогона шагов",
              str(sorted(rep)))
    finally:
        fh.close()
    rep2 = asyncio.run(pipeline_mod.run(str(db), skip_sync=True, skip_media=True))
    check(rep2.get("already_running") is None
          and rep2.get("monitor", {}).get("ok") is True,
          "lock: после освобождения прогон проходит", str(sorted(rep2)))


def test_notify_hash_dedup() -> None:
    print("=== monitor --send: повтор без изменений не отправляется ===")
    import monitor as monitor_mod
    import notify as notify_mod

    db = TMP / "dedup.sqlite"
    _seed_watch_db(db, project="утро")
    now = int(time.time())
    with DB(db) as d:
        d.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "due_ts, project, created_ts, updated_ts) "
            "VALUES('tg', -100900, 1, 'task', 'Дедуп задача', 'open', "
            "?, 'утро', ?, ?)",
            (now - 7200, now - 7200, now - 7200))
        d.conn.commit()

    calls: list[list[str]] = []

    async def fake_transport(messages: list[str]) -> tuple[int, list[str]]:
        calls.append(list(messages))
        return len(messages), []

    saved_t = notify_mod._telegram_send
    notify_mod._telegram_send = fake_transport
    os.environ["TG_ALLOW_SEND"] = "1"
    os.environ.pop("TG_MONITOR_FORCE", None)
    try:
        r1 = monitor_mod.run(str(db), send=True, dry_run=False)
        check(r1["notify"].get("ok") is True and len(calls) == 1,
              "дедуп: первая отправка ушла", str(r1["notify"]))
        r2 = monitor_mod.run(str(db), send=True, dry_run=False)
        check(r2["notify"].get("skipped") is True and len(calls) == 1,
              "дедуп: повтор без изменений не отправляется", str(r2["notify"]))
        os.environ["TG_MONITOR_FORCE"] = "1"
        r3 = monitor_mod.run(str(db), send=True, dry_run=False)
        check(r3["notify"].get("ok") is True and not r3["notify"].get("skipped")
              and len(calls) == 2,
              "дедуп: TG_MONITOR_FORCE=1 обходит проверку", str(r3["notify"]))
    finally:
        notify_mod._telegram_send = saved_t
        os.environ.pop("TG_ALLOW_SEND", None)
        os.environ.pop("TG_MONITOR_FORCE", None)


def test_aging_questions() -> None:
    print("=== monitor: блок зависших вопросов старше N дней ===")
    import monitor as monitor_mod

    db = TMP / "questions.sqlite"
    _seed_watch_db(db)
    now = int(time.time())
    with DB(db) as d:
        d.conn.executemany(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "created_ts, updated_ts, closed_ts) "
            "VALUES('tg', -100900, 1, 'question', ?, 'open', ?, ?, NULL)",
            [("Старый вопрос", now - 5 * 86400, now - 5 * 86400),
             ("Свежий вопрос", now - 3600, now - 3600)])
        d.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "created_ts, updated_ts, closed_ts) "
            "VALUES('tg', -100900, 1, 'question', 'Закрытый старый', 'done', "
            "?, ?, ?)",
            (now - 5 * 86400, now - 5 * 86400, now))
        d.conn.commit()

    os.environ["MONITOR_QUESTION_DAYS"] = "3"
    try:
        s = monitor_mod.collect(str(db))
    finally:
        os.environ.pop("MONITOR_QUESTION_DAYS", None)
    titles = [e["title"] for e in s["questions"]]
    check(titles == ["Старый вопрос"],
          "вопросы: только открытые старше 3 дн., топ-10", str(titles))
    lines = monitor_mod.format_report(s)
    check(any("Старый вопрос" in ln and "вопрос" in ln for ln in lines),
          "вопросы: блок виден в сводке", str(lines[:5]))
    check("открытых вопросов" in lines[0],
          "вопросы: счётчик в шапке сводки", lines[0][:160])


def test_monitor_promise_gaps() -> None:
    print("=== monitor: обещания без срока и без ответственного ===")
    import monitor as monitor_mod

    db = TMP / "promise_gaps.sqlite"
    _seed_watch_db(db, project="стройка")
    now = int(time.time())
    with DB(db) as d:
        d.conn.executemany(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "owner, due_ts, project, created_ts, updated_ts) "
            "VALUES('construction', -100900, 1, 'promise', ?, 'open', ?, ?, 'стройка', ?, ?)",
            [("Обещание без срока", "Иван", None, now - 2, now),
             ("Обещание без ответственного", None, now + 7 * 86400, now - 1, now),
             ("Полное обещание", "Мария", now + 3 * 86400, now, now)])
        d.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "owner, due_ts, project, created_ts, updated_ts, closed_ts) "
            "VALUES('construction', -100900, 1, 'promise', 'Закрытое без срока', "
            "'done', NULL, NULL, 'стройка', ?, ?, ?)",
            (now - 3, now - 3, now))
        d.conn.commit()

    s = monitor_mod.collect(str(db))
    nd = [e["title"] for e in s["promises_no_due"]]
    no = [e["title"] for e in s["promises_no_owner"]]
    check(nd == ["Обещание без срока"],
          "promises_no_due: только открытые promise без due_ts", str(nd))
    check(no == ["Обещание без ответственного"],
          "promises_no_owner: только открытые promise без owner", str(no))
    lines = monitor_mod.format_report(s)
    check("обещаний без срока: 1" in lines[0],
          "promises: счётчик без срока в шапке", lines[0][:200])
    check("обещаний без ответственного: 1" in lines[0],
          "promises: счётчик без ответственного в шапке", lines[0][:200])
    check(any("обещание без срока:" in ln and "Обещание без срока" in ln for ln in lines),
          "promises: блок без срока виден в сводке", str(lines[:8]))
    check(any("обещание без ответственного:" in ln and "Обещание без ответственного" in ln
              for ln in lines),
          "promises: блок без ответственного виден в сводке", str(lines[:8]))

    empty = TMP / "promise_gaps_empty.sqlite"
    _seed_watch_db(empty)
    s2 = monitor_mod.collect(str(empty))
    check(s2["promises_no_due"] == [] and s2["promises_no_owner"] == [],
          "promises: пустая БД — списки пусты",
          str((s2["promises_no_due"], s2["promises_no_owner"])))
    lines2 = monitor_mod.format_report(s2)
    check(not any("без срока" in ln or "без ответственного" in ln for ln in lines2),
          "promises: пустые списки не добавляют строк", str(lines2))

    filt = monitor_mod.collect(str(db), project="баня")
    check(filt["promises_no_due"] == [] and filt["promises_no_owner"] == [],
          "promises: фильтр по чужому проекту даёт пустые списки",
          str((filt["promises_no_due"], filt["promises_no_owner"])))


def test_monitor_stuck() -> None:
    print("=== monitor: blocked+просрочено = застряло, in_progress остаётся просроченным ===")
    import monitor as monitor_mod

    db = TMP / "stuck.sqlite"
    _seed_watch_db(db, project="стройка")
    now = int(time.time())
    with DB(db) as d:
        d.conn.executemany(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "owner, due_ts, project, created_ts, updated_ts) "
            "VALUES('construction', -100900, 1, 'task', ?, ?, 'Иван', ?, 'стройка', ?, ?)",
            [("Застрявшая задача", "blocked", now - 2 * 86400, now - 3 * 86400, now),
             ("Простая просрочка", "in_progress", now - 86400, now - 3 * 86400, now),
             ("Заблокирована в срок", "blocked", now + 5 * 86400, now - 3 * 86400, now)])
        d.conn.execute(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "owner, due_ts, project, created_ts, updated_ts, closed_ts) "
            "VALUES('construction', -100900, 1, 'task', 'Закрытая застрявшая', 'blocked', "
            "'Иван', ?, 'стройка', ?, ?, ?)",
            (now - 2 * 86400, now - 3 * 86400, now, now))
        d.conn.commit()

    s = monitor_mod.collect(str(db))
    st = [e["title"] for e in s["stuck"]]
    check(st == ["Застрявшая задача"],
          "stuck: только blocked и просроченные открытые", str(st))
    ov = [e["title"] for e in s["overdue"]]
    check("Простая просрочка" in ov and "Застрявшая задача" in ov,
          "stuck: просроченные остаются и в общем overdue", str(ov))
    p = s["projects"]["стройка"]
    check([e["title"] for e in p["stuck"]] == ["Застрявшая задача"]
          and [e["title"] for e in p["overdue"]] == ["Простая просрочка"],
          "stuck: проект разделяет застрявшее и обычное просроченное",
          str(([e["title"] for e in p["stuck"]], [e["title"] for e in p["overdue"]])))
    lines = monitor_mod.format_report(s)
    check("застряло: 1" in lines[0], "stuck: счётчик в шапке", lines[0][:200])
    check(any("застряло:" in ln and "Застрявшая задача" in ln for ln in lines),
          "stuck: маркер «застряло:» в строке проекта", str(lines))
    proj_line = next(ln for ln in lines if ln.startswith("стройка:"))
    segs = proj_line.split(" | ")
    od_seg = next((s for s in segs if s.startswith("просрочено:")), "")
    st_seg = next((s for s in segs if s.startswith("застряло:")), "")
    check("Простая просрочка" in od_seg and "Застрявшая задача" not in od_seg
          and "Застрявшая задача" in st_seg and "Простая просрочка" not in st_seg,
          "stuck: in_progress рендерится обычным «просрочено:», blocked — «застряло:»",
          proj_line)

    r = monitor_mod.run(str(db))
    check("stuck" in r and "promises_no_due" in r and "promises_no_owner" in r,
          "run(): новые ключи в возвращаемом подмножестве", str(sorted(r)))

    filt = monitor_mod.collect(str(db), project="баня")
    check(filt["stuck"] == [], "stuck: фильтр по чужому проекту даёт пустой список",
          str(filt["stuck"]))

    empty = TMP / "stuck_empty.sqlite"
    _seed_watch_db(empty)
    s2 = monitor_mod.collect(str(empty))
    check(s2["stuck"] == [], "stuck: пустая БД — список пуст")
    lines2 = monitor_mod.format_report(s2)
    check(not any("застряло" in ln for ln in lines2),
          "stuck: пустой список не добавляет строк", str(lines2))


def test_extract_report_rejected() -> None:
    print("=== pipeline: ops_rejected/ops_overflow в отчёте и в сводке ===")
    import extract as extract_mod
    import pipeline as pipeline_mod

    db = TMP / "rejected.sqlite"
    _seed_watch_db(db)
    now = int(time.time())
    with DB(db) as d:
        d.conn.execute(
            "INSERT INTO extraction_runs(pack, chat_id, topic_id, ts, ops_applied, "
            "ops_rejected, ops_overflow, status) "
            "VALUES('tg', -100900, 1, ?, 2, 5, 1, 'partial')", (now,))
        d.conn.commit()

    def fake_extract(db_path, pack_id=None, until_done=True, **kw):
        return [{"pack": "tg", "chat_id": -100900, "topic_id": 1, "messages": 3,
                 "ops_applied": 2, "ops_rejected": 4, "ops_overflow": 1, "upto_msg": 9}]

    saved = extract_mod.run
    extract_mod.run = fake_extract
    try:
        rep = asyncio.run(pipeline_mod.run(str(db), skip_sync=True, skip_media=True))
    finally:
        extract_mod.run = saved
    ex = rep.get("extract", {})
    check(ex.get("ok") is True and ex.get("ops_rejected") == 4
          and ex.get("ops_overflow") == 1,
          "pipeline: ops_rejected и ops_overflow в отчёте extract", str(ex))
    check(any("отклонено операций 5" in ln for ln in rep["monitor"]["lines"]),
          "pipeline: строка rejected в сводке мониторинга",
          str(rep["monitor"]["lines"][:4]))


def test_doctor_quality() -> None:
    print("=== doctor --quality: заполненность полей и гистограмма отказов ===")
    import doctor

    db = TMP / "quality.sqlite"
    _seed_watch_db(db)
    now = int(time.time())
    with DB(db) as d:
        d.conn.executemany(
            "INSERT INTO entities(pack, chat_id, topic_id, kind, title, status, "
            "owner, due_ts, body, created_ts, updated_ts) "
            "VALUES('construction', -100900, 1, 'task', ?, 'open', ?, ?, ?, ?, ?)",
            [("с owner", "Иван", None, "тело", now, now),
             ("без ничего", None, None, None, now, now),
             ("с due", None, now, None, now, now)])
        d.conn.commit()
        try:
            d.conn.execute("ALTER TABLE extraction_runs ADD COLUMN rejects_json TEXT")
        except sqlite3.OperationalError:
            pass
        d.conn.commit()
        d.conn.execute(
            "INSERT INTO extraction_runs(pack, chat_id, topic_id, ts, ops_applied, "
            "ops_rejected, status, rejects_json) "
            "VALUES('construction', -100900, 1, ?, 1, 3, 'partial', ?)",
            (now, json.dumps([{"reason": "evidence не найден"},
                              {"reason": "evidence не найден"},
                              {"reason": "нет поля kind"}])))
        d.conn.commit()

    checks = doctor.quality_checks(str(db))
    by = {c["name"]: c for c in checks}
    fill = by.get("заполненность [construction]", {})
    check("n=3" in fill.get("detail", "") and "owner 33%" in fill.get("detail", "")
          and "due 33%" in fill.get("detail", "") and "body 33%" in fill.get("detail", ""),
          "doctor --quality: заполненность полей по пакету", fill.get("detail"))
    rej = by.get("причины отказов", {})
    check("evidence не найден: 2" in rej.get("detail", "")
          and "нет поля kind: 1" in rej.get("detail", ""),
          "doctor --quality: гистограмма причин из rejects_json", rej.get("detail"))

    fresh = TMP / "quality_fresh.sqlite"
    with DB(fresh) as d:
        d.conn.execute("DROP TABLE extraction_runs")
        d.conn.execute(
            "CREATE TABLE extraction_runs(pack TEXT, chat_id INTEGER, topic_id INTEGER, "
            "ts INTEGER, ops_applied INTEGER, ops_rejected INTEGER, status TEXT)")
        d.conn.commit()
    by2 = {c["name"]: c for c in doctor.quality_checks(str(fresh))}
    check("rejects_json" in by2.get("причины отказов", {}).get("detail", ""),
          "doctor --quality: без колонки rejects_json говорит об этом прямо",
          by2.get("причины отказов", {}).get("detail"))

    stub = doctor._provider_check("EXTRACT")
    check(stub["ok"] is None and "неприменима" in stub["detail"],
          "doctor --providers: без openai-провайдера сеть не трогается",
          stub["detail"])

    env = dict(os.environ, TG_DB=str(db), TG_ENV_FILE="")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8",
         str(Path(__file__).parent / "doctor.py"), "--quality"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    out = proc.stdout + proc.stderr
    check("owner 33%" in out and "evidence не найден: 2" in out,
          "doctor --quality (CLI): рендер заполненности и гистограммы",
          out[-300:])


def test_doctor_cost() -> None:
    print("=== doctor --cost: расход токенов по пакетам и прогоны без данных ===")
    import doctor

    db = TMP / "cost.sqlite"
    _seed_watch_db(db)
    now = int(time.time())
    with DB(db) as d:
        d.conn.executemany(
            "INSERT INTO extraction_runs(pack, chat_id, topic_id, ts, ops_applied, "
            "status, usage_prompt, usage_completion) "
            "VALUES('construction', -100900, 1, ?, 1, 'ok', ?, ?)",
            [(now, 100, 50), (now + 1, None, None)])
        d.conn.commit()

    checks = doctor.cost_checks(str(db))
    by = {c["name"]: c for c in checks}
    det = by.get("стоимость [construction]", {}).get("detail", "")
    check("prompt=100" in det and "completion=50" in det and "прогонов без данных: 1" in det,
          "doctor --cost: суммы usage и счётчик NULL-прогонов по пакету", det)
    total = by.get("стоимость: итого", {})
    check("runs=2" in total.get("detail", "")
          and "prompt=100" in total.get("detail", "")
          and "completion=50" in total.get("detail", ""),
          "doctor --cost: итоговая строка по всем прогонам", total.get("detail"))

    empty = TMP / "cost_empty.sqlite"
    with DB(empty):
        pass
    ec = doctor.cost_checks(str(empty))
    check(len(ec) == 1 and ec[0]["ok"] is True and "нет данных" in ec[0]["detail"],
          "doctor --cost: пустая БД — одна проверка «нет данных» без исключения",
          str(ec))

    env = dict(os.environ, TG_DB=str(db), TG_ENV_FILE="",
               TELEGRAM_API_ID="1", TELEGRAM_API_HASH="h")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8",
         str(Path(__file__).parent / "doctor.py"), "--cost"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    out = proc.stdout + proc.stderr
    check(proc.returncode == 0 and "prompt=100" in out and "completion=50" in out
          and "прогонов без данных: 1" in out,
          "doctor --cost (CLI): exit 0 и рендер сумм", f"rc={proc.returncode} {out[-300:]}")


def test_digest_bound() -> None:
    print("=== digest: лимит строк не теряет хвост (курсор подбирает) ===")
    import digest as digest_mod

    db = TMP / "digest.sqlite"
    _seed_watch_db(db)
    now = int(time.time())
    with DB(db) as d:
        for i in (1, 2, 3):
            d.conn.execute(
                "INSERT INTO messages(chat_id, msg_id, topic_id, ts, sender_name, text) "
                "VALUES(-100900, ?, 1, ?, 'Анна', ?)", (i, now - 60 * i, f"текст {i}"))
        d.conn.commit()

    os.environ["DIGEST_MAX_ROWS"] = "2"
    try:
        made1 = digest_mod.run(str(db))
        made2 = digest_mod.run(str(db))
    finally:
        os.environ.pop("DIGEST_MAX_ROWS", None)
    check(len(made1) == 1 and made1[0]["messages"] == 2,
          "digest: за прогон взято не больше DIGEST_MAX_ROWS строк",
          str(made1))
    check(len(made2) == 1 and made2[0]["messages"] == 1,
          "digest: остаток подобран следующим прогоном по курсору", str(made2))


def test_loop_until_done_false() -> None:
    print("=== loop: extract без until_done, run() внутри run_loop не дедлокится ===")
    import extract as extract_mod
    import pipeline as pipeline_mod
    import sync as sync_mod

    db = TMP / "loop.sqlite"
    _seed_watch_db(db)
    seen: dict = {}

    def fake_extract(db_path, pack_id=None, until_done=True, **kw):
        seen["until_done"] = until_done
        return []

    async def fake_sync(**kw):
        return {"chats": [], "dry_run": False}

    async def stop_loop(sec):
        raise KeyboardInterrupt

    saved_run = extract_mod.run
    saved_sync = sync_mod.run
    saved_sleep = asyncio.sleep
    extract_mod.run = fake_extract
    sync_mod.run = fake_sync
    asyncio.sleep = stop_loop
    try:
        asyncio.run(pipeline_mod.run_loop(str(db), None, 7, False, 30))
        check(False, "loop: цикл должен был остановиться KeyboardInterrupt'ом")
    except KeyboardInterrupt:
        pass
    finally:
        extract_mod.run = saved_run
        sync_mod.run = saved_sync
        asyncio.sleep = saved_sleep
    check(seen.get("until_done") is False,
          "loop: extract зовётся с until_done=False (курсоры доводят хвост)",
          str(seen))


def main() -> int:
    try:
        test_notify_event_loop()
        test_pipeline_send_end_to_end()
        test_step_budget()
        test_oneshot_lock()
        test_notify_hash_dedup()
        test_aging_questions()
        test_monitor_promise_gaps()
        test_monitor_stuck()
        test_extract_report_rejected()
        test_doctor_quality()
        test_doctor_cost()
        test_digest_bound()
        test_loop_until_done_false()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
