#!/usr/bin/env python3
"""Семантика пакетов извлечения: due-text фолбэк, изоляция проекта, supersedes.

Проверяет детерминированный парсинг due_text -> due_ts (абсолютные даты, русские
месяцы, «N числа», относительные формулировки, ISO; конец дня вместо полуночи),
no-downgrade при update, require_match в scope-фильтре (construction не заходит
в темы без проекта и проекта «сервисы»), связь supersedes, persist причин
отклонений (rejects_json), dirty-репроцесс отредактированных сообщений,
dedup_scope project/global и схему пакетов (minItems evidence, без
covered_upto_msg_id и confidence).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB  # noqa: E402
import extract as extract_mod  # noqa: E402
import fuzzy_dedup  # noqa: E402
from packs import PACKS  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def ts_utc(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def eod(y: int, m: int, d: int) -> int:
    return ts_utc(y, m, d) + 86399


def seed_messages(db: DB, chat_id: int, topic_id: int, texts: dict[int, str],
                  ts: int) -> None:
    for mid, text in texts.items():
        db.conn.execute(
            "INSERT INTO messages(chat_id, msg_id, topic_id, ts, date_iso, "
            "sender_id, sender_name, is_service, text) "
            "VALUES(?, ?, ?, ?, ?, 1, 'Иван', 0, ?)",
            (chat_id, mid, topic_id, ts,
             datetime.fromtimestamp(ts, timezone.utc).isoformat(), text))
    db.conn.commit()


def set_cursor(db: DB, chat_id: int, topic_id: int, pack_id: str, value: int) -> None:
    db.conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"extract:{chat_id}:{topic_id}:{pack_id}", str(value)))
    db.conn.commit()


def set_meta(db: DB, key: str, value: str) -> None:
    db.conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))
    db.conn.commit()


def add_rejects_col(db: DB) -> None:
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(extraction_runs)")}
    if "rejects_json" not in cols:
        db.conn.execute("ALTER TABLE extraction_runs ADD COLUMN rejects_json TEXT")
        db.conn.commit()


def test_parse_due_text() -> None:
    print("=== due_text: детерминированный парсинг дат ===")
    p = extract_mod._parse_due_text
    check(p("20.10.2026", None) == eod(2026, 10, 20),
          "due_text: dd.mm.yyyy -> конец дня UTC")
    check(p("до 20.10.2026", None) == eod(2026, 10, 20),
          "due_text: «до dd.mm.yyyy»")
    check(p("сдать до 20.10.2026 включительно", None) == eod(2026, 10, 20),
          "due_text: дата внутри фразы")
    check(p("20.10.26", None) == eod(2026, 10, 20),
          "due_text: dd.mm.yy -> 20xx")
    check(p("01.02.27", None) == eod(2027, 2, 1),
          "due_text: dd.mm.yy с ведущими нулями")
    check(p("2026-10-20", None) == eod(2026, 10, 20),
          "due_text: ISO YYYY-MM-DD")
    check(p("к 2026-10-20 включительно", None) == eod(2026, 10, 20),
          "due_text: ISO внутри фразы")
    msg_jun = ts_utc(2026, 6, 15)
    msg_nov = ts_utc(2026, 11, 25)
    check(p("20.10", msg_jun) == eod(2026, 10, 20),
          "due_text: dd.mm позже даты сообщения -> текущий год")
    check(p("20.10", msg_nov) == eod(2027, 10, 20),
          "due_text: dd.mm раньше даты сообщения -> следующий год")
    check(p("20.10", None) is None,
          "due_text: dd.mm без даты сообщения неопределим")
    check(p("19 августа", msg_jun) == eod(2026, 8, 19),
          "due_text: «19 августа» -> конец дня")
    check(p("до 19 августа", msg_jun) == eod(2026, 8, 19),
          "due_text: «до 19 августа»")
    check(p("19 авг", msg_jun) == eod(2026, 8, 19),
          "due_text: сокращённый месяц «19 авг»")
    check(p("19 августа 2027", msg_jun) == eod(2027, 8, 19),
          "due_text: месяц с явным годом")
    check(p("1 мая", msg_jun) == eod(2027, 5, 1),
          "due_text: месяц раньше даты сообщения -> следующий год")
    check(p("19 августа", None) is None,
          "due_text: месяц без года и без даты сообщения неопределим")
    check(p("до 25 числа", msg_jun) == eod(2026, 6, 25),
          "due_text: «до 25 числа» -> день текущего месяца")
    check(p("к 25 числу", ts_utc(2026, 6, 30)) == eod(2026, 7, 25),
          "due_text: «к 25 числу» позже конца месяца -> следующий месяц")
    check(p("25 числа", None) is None,
          "due_text: «N числа» без даты сообщения неопределим")
    check(p("сегодня", msg_jun) == eod(2026, 6, 15),
          "due_text: «сегодня» -> конец дня сообщения")
    check(p("завтра", msg_jun) == eod(2026, 6, 16),
          "due_text: «завтра»")
    check(p("послезавтра", msg_jun) == eod(2026, 6, 17),
          "due_text: «послезавтра» (не путается с «завтра»)")
    check(p("через 7 дней", msg_jun) == eod(2026, 6, 22),
          "due_text: «через 7 дней»")
    check(p("через 2 недели", msg_jun) == eod(2026, 6, 29),
          "due_text: «через 2 недели»")
    check(p("через месяц", msg_jun) == eod(2026, 7, 15),
          "due_text: «через месяц»")
    check(p("через 2 месяца", ts_utc(2026, 11, 30)) == eod(2027, 1, 30),
          "due_text: «через 2 месяца» через границу года")
    check(p("через месяц", ts_utc(2026, 1, 31)) == eod(2026, 2, 28),
          "due_text: «через месяц» с клампом дня (31 -> 28)")
    check(p("7 дней", msg_jun) == eod(2026, 6, 22),
          "due_text: голое «7 дней» — длительность от даты сообщения")
    check(p("неделя", msg_jun) == eod(2026, 6, 22),
          "due_text: голое «неделя»")
    check(p("через день", msg_jun) == eod(2026, 6, 16),
          "due_text: «через день»")
    check(p("завтра", None) is None,
          "due_text: относительное без даты сообщения неопределим")
    check(p("к пятнице", msg_jun) is None,
          "due_text: день недели не парсится")
    check(p("32.13.2026", None) is None,
          "due_text: несуществующая дата отклонена")
    check(p("2026-02-30", None) is None,
          "due_text: несуществующая ISO-дата отклонена")
    check(p("1.5 года", msg_jun) is None,
          "due_text: десятичная длительность не принимается за дату")
    check(p("мусорная фраза", msg_jun) is None,
          "due_text: мусор -> None")
    check(p(None, msg_jun) is None and p("", msg_jun) is None,
          "due_text: пустой ввод")


def test_due_fallback(root: Path) -> None:
    print("=== due_text: фолбэк и no-downgrade в _apply_new/_apply_update ===")
    db_path = root / "due_fallback.sqlite"
    chat, topic = -100950, 5
    msg_ts = ts_utc(2026, 6, 15)
    with DB(db_path) as db:
        db.upsert_chat(chat, "Объект", None, "supergroup", True, 6)
        db.upsert_topic(chat, topic, "Объект")
        db.watch_add(chat, topic, project="стройка")
        seed_messages(db, chat, topic, {
            601: "задача: подготовить смету, срок до 20.10.2026",
            602: "задача: заказать окна, срок 31.12",
            603: "задача: уточнить планировку",
            604: "задача: согласовать цвета",
            605: "задача: обновить смету",
            606: "задача: ревизия окон",
            607: "задача: финальная ревизия",
        }, msg_ts)
        set_cursor(db, chat, topic, "construction", 600)

    pack = PACKS["construction"]

    def op(kind, title, msg, quote, due_text=None, due_ts=None, entity_id=None,
           supersedes=None, confidence=None):
        return {"op": "new" if entity_id is None else "update", "kind": kind,
                "title": title, "entity_id": entity_id, "body": None,
                "owner": None, "due_text": due_text, "due_ts": due_ts,
                "confidence": confidence, "supersedes": supersedes,
                "evidence": [{"chat_id": chat, "msg_id": msg, "quote": quote}]}

    def run_ops(ops, upto):
        with DB(db_path) as db:
            set_cursor(db, chat, topic, "construction", upto)
        return extract_mod.run(str(db_path), packs=[pack], workers=1,
                               provider=lambda prompt, pk: {"ops": ops})

    run_ops([op("task", "Смета", 601, "смету", due_text="до 20.10.2026",
                confidence=0.9)], 600)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        check(e["due_ts"] == eod(2026, 10, 20),
              "фолбэк: «до 20.10.2026» без due_ts заполнил due_ts (конец дня)",
              f"due_ts={e and e['due_ts']}")
        check(e["attrs_json"] and json.loads(e["attrs_json"])["due_text"]
              == "до 20.10.2026",
              "фолбэк: due_text сохранён в attrs_json",
              f"attrs={e and e['attrs_json']}")
        check(e["confidence"] is None,
              "confidence: колонка есть, но extract больше не заполняет её",
              f"confidence={e and e['confidence']}")

    run_ops([op("task", "Окна", 602, "окна", due_text="31.12")], 601)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=2").fetchone()
        check(e["due_ts"] == eod(2026, 12, 31),
              "фолбэк: «31.12» разрешён по дате сообщения (2026-06-15 -> 2026-12-31)",
              f"due_ts={e and e['due_ts']}")

    run_ops([op("task", "Планировка", 603, "планировку",
                due_text="к пятнице")], 602)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=3").fetchone()
        check(e["due_ts"] is None and e["attrs_json"]
              and json.loads(e["attrs_json"])["due_text"] == "к пятнице",
              "фолбэк: неразрешимый due_ts остался None, due_text в attrs_json",
              f"due_ts={e and e['due_ts']} attrs={e and e['attrs_json']}")

    run_ops([op("task", "Цвета", 604, "цвета", due_text="20.10.2026",
                due_ts=1890000000)], 603)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=4").fetchone()
        check(e["due_ts"] == 1890000000,
              "фолбэк: due_ts от модели сильнее due_text",
              f"due_ts={e and e['due_ts']}")

    run_ops([op("task", "Смета v2", 605, "обновить смету", due_text="15.11",
                entity_id=1)], 604)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        check(e["due_ts"] == eod(2026, 10, 20),
              "no-downgrade: update с due_text без явного due_ts не затирает срок",
              f"due_ts={e and e['due_ts']}")
        check(json.loads(e["attrs_json"])["due_text"] == "15.11",
              "no-downgrade: due_text в attrs_json всё же обновлён",
              f"attrs={e and e['attrs_json']}")

    run_ops([op("task", "Смета v3", 606, "ревизия окон", due_ts=1890000001,
                entity_id=1)], 605)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        check(e["due_ts"] == 1890000001,
              "no-downgrade: явный due_ts от модели обновляет срок",
              f"due_ts={e and e['due_ts']}")
        ev = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=1 AND field='due_ts' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        check(ev is not None and ev["to_val"] == "1890000001",
              "no-downgrade: событие due_ts с from/to",
              f"to={ev and ev['to_val']}")

    run_ops([op("task", "Планировка v2", 607, "финальная ревизия",
                due_text="20.10.2026", entity_id=3)], 606)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=3").fetchone()
        check(e["due_ts"] == eod(2026, 10, 20),
              "no-downgrade: пустой срок заполняется из due_text при update",
              f"due_ts={e and e['due_ts']}")


def test_supersedes(root: Path) -> None:
    print("=== supersedes: закрытие старого решения и валидация ===")
    db_path = root / "supersedes.sqlite"
    chat, topic = -100951, 9
    msg_ts = ts_utc(2026, 6, 15)
    with DB(db_path) as db:
        db.upsert_chat(chat, "Объект-2", None, "supergroup", True, 6)
        db.upsert_topic(chat, topic, "Объект-2")
        db.watch_add(chat, topic, project="стройка")
        seed_messages(db, chat, topic, {
            701: "решено: фундамент из блоков",
            702: "решено: фундамент монолит вместо блоков",
            703: "решено: кровля металл",
            704: "решено: кровля мягкая",
            705: "решено: новый поставщик",
            706: "решено: пересмотр поставки",
            707: "решено: кровля металлочерепица",
        }, msg_ts)
        set_cursor(db, chat, topic, "construction", 700)

    pack = PACKS["construction"]

    def op(kind, title, msg, quote, entity_id=None, supersedes=None):
        return {"op": "new" if entity_id is None else "update", "kind": kind,
                "title": title, "entity_id": entity_id, "body": None,
                "owner": None, "due_text": None, "due_ts": None,
                "supersedes": supersedes,
                "evidence": [{"chat_id": chat, "msg_id": msg, "quote": quote}]}

    def run_ops(ops, upto):
        with DB(db_path) as db:
            set_cursor(db, chat, topic, "construction", upto)
        return extract_mod.run(str(db_path), packs=[pack], workers=1,
                               provider=lambda prompt, pk: {"ops": ops})

    def last_run(db):
        return db.conn.execute(
            "SELECT * FROM extraction_runs ORDER BY id DESC LIMIT 1").fetchone()

    run_ops([op("decision", "Фундамент из блоков", 701, "блоков")], 700)
    run_ops([op("decision", "Фундамент монолит", 702, "монолит",
                supersedes=1)], 701)
    with DB(db_path) as db:
        old = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        new = db.conn.execute("SELECT * FROM entities WHERE id=2").fetchone()
        check(old["closed_ts"] is not None and old["status"] == "done",
              "supersedes: старое решение закрыто (closed_ts, status)",
              f"closed_ts={old and old['closed_ts']} status={old and old['status']}")
        check(new["closed_ts"] is None and new["status"] == "open",
              "supersedes: новое решение открыто")
        ev = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=1 AND op='supersede'").fetchone()
        check(ev is not None and ev["field"] == "superseded_by"
              and ev["to_val"] == "2" and ev["msg_id"] == 702
              and ev["quote"] == "монолит",
              "supersedes: событие supersede/superseded_by с to_val и evidence",
              f"ev={dict(ev) if ev else None}")

    run_ops([op("decision", "Кровля металл", 703, "металл", supersedes=2),
             op("decision", "Кровля мягкая", 704, "мягкая", supersedes=2)], 702)
    with DB(db_path) as db:
        evs = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=2 AND op='supersede'").fetchall()
        check(len(evs) == 1 and evs[0]["to_val"] == "3",
              "supersedes: повторное закрытие в одном батче — идемпотентный no-op",
              f"evs={[(r['to_val']) for r in evs]}")
        n = db.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        check(n == 4, "supersedes: обе операции new применены", str(n))

    with DB(db_path) as db:
        db.conn.execute(
            "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, status, "
            "created_ts, updated_ts) VALUES(5, 'construction', ?, 99, 'decision', "
            "'Чужая тема', 'open', ?, ?)", (chat, msg_ts, msg_ts))
        db.conn.execute(
            "INSERT INTO entity_sources(entity_id, chat_id, topic_id, first_seen_ts, "
            "last_seen_ts) VALUES(5, ?, 99, ?, ?)", (chat, msg_ts, msg_ts))
        db.conn.commit()
    run_ops([op("decision", "Новый поставщик", 705, "поставщик",
                supersedes=5)], 704)
    with DB(db_path) as db:
        run = last_run(db)
        check(run["ops_applied"] == 0 and run["ops_rejected"] == 1,
              "supersedes: id из чужой темы отклонён",
              f"applied={run['ops_applied']} rejected={run['ops_rejected']}")
        e = db.conn.execute("SELECT * FROM entities WHERE id=5").fetchone()
        check(e["closed_ts"] is None,
              "supersedes: вне-скоуповая цель не закрыта")
        evs = db.conn.execute(
            "SELECT COUNT(*) c FROM entity_events WHERE entity_id=5 "
            "AND op='supersede'").fetchone()["c"]
        check(evs == 0, "supersedes: вне-скоуповой цели нет события")

    run_ops([op("decision", "Пересмотр поставки", 706, "пересмотр",
                supersedes=1)], 705)
    with DB(db_path) as db:
        run = last_run(db)
        check(run["ops_applied"] == 0 and run["ops_rejected"] == 1,
              "supersedes: закрытая цель отклонена",
              f"applied={run['ops_applied']} rejected={run['ops_rejected']}")
        evs = db.conn.execute(
            "SELECT COUNT(*) c FROM entity_events WHERE entity_id=1 "
            "AND op='supersede'").fetchone()["c"]
        check(evs == 1, "supersedes: закрытой цели не добавлено событий", str(evs))

    run_ops([op("decision", "Кровля металлочерепица", 707, "металлочерепица",
                entity_id=3, supersedes=4)], 706)
    with DB(db_path) as db:
        e4 = db.conn.execute("SELECT * FROM entities WHERE id=4").fetchone()
        check(e4["closed_ts"] is not None and e4["status"] == "done",
              "supersedes: update-операция тоже закрывает цель")
        ev = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=4 AND op='supersede'").fetchone()
        check(ev is not None and ev["to_val"] == "3",
              "supersedes: событие от update-операции ведёт на обновлённую сущность",
              f"to={ev and ev['to_val']}")
        e3 = db.conn.execute(
            "SELECT title FROM entities WHERE id=3").fetchone()
        check(e3["title"] == "Кровля металлочерепица",
              "supersedes: update применил и свои поля", str(e3["title"]))


def test_scope_isolation(root: Path) -> None:
    print("=== scope: require_match изолирует construction ===")
    con = PACKS["construction"]["scope_filter"]
    svc = PACKS["services"]["scope_filter"]
    check(extract_mod.scope_matches(con, {"chat_id": 1, "project": "сервисы"}) is False,
          "scope: construction не матчится на проект «сервисы»")
    check(extract_mod.scope_matches(con, {"chat_id": 1, "project": "стройка"}) is True,
          "scope: construction матчится на «стройка»")
    check(extract_mod.scope_matches(con, {"chat_id": 1, "project": "Объект Х"}) is True,
          "scope: construction матчится на другой проект")
    check(extract_mod.scope_matches(con, {"chat_id": 1, "project": None}) is False,
          "scope: construction не работает в темах без проекта (require_match)")
    check(extract_mod.scope_matches(
        {"require_match": True, "topic_title_regex": None, "alias_regex": None,
         "project_regex": None, "chat_ids": None},
        {"chat_id": 1, "project": "x"}) is False,
        "scope: require_match без единого include-ключа не матчится")
    check(extract_mod.scope_matches(
        {"require_match": True, "chat_ids": [7]},
        {"chat_id": 7, "project": None}) is True,
        "scope: require_match с chat_ids матчится по чату")
    check(extract_mod.scope_matches(svc, {"chat_id": 1, "project": "сервисы"}) is True,
          "scope: services по-прежнему только «сервисы»")
    check(extract_mod.scope_matches(svc, {"chat_id": 1, "project": "стройка"}) is False,
          "scope: services не заходит в «стройка»")

    db_path = root / "isolation.sqlite"
    chat = -100960
    now = int(time.time())
    with DB(db_path) as db:
        db.upsert_chat(chat, "Смешанный", None, "supergroup", True, 9)
        db.upsert_topic(chat, 20, "Триалы")
        db.upsert_topic(chat, 21, "Стройка")
        db.upsert_topic(chat, 22, "Общее")
        db.watch_add(chat, 20, project="сервисы")
        db.watch_add(chat, 21, project="стройка")
        db.watch_add(chat, 22)
        seed_messages(db, chat, 20, {
            801: "триал 30 дней на Acme Cloud",
            802: "решено: использовать Acme в отделке",
        }, now - 60)
        seed_messages(db, chat, 21, {811: "решено: фундамент монолит"}, now - 60)
        seed_messages(db, chat, 22, {821: "задача: закрыть контур"}, now - 60)

    extract_mod.run(str(db_path), workers=1)
    with DB(db_path) as db:
        con_topics = {r["topic_id"] for r in db.conn.execute(
            "SELECT DISTINCT topic_id FROM entities WHERE pack='construction'")}
        check(con_topics == {21},
              "изоляция: construction извлекал только в стройке",
              str(sorted(con_topics)))
        cur20 = db.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"extract:{chat}:20:construction",)).fetchone()
        cur22 = db.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"extract:{chat}:22:construction",)).fetchone()
        check(cur20 is None and cur22 is None,
              "изоляция: курсоров construction в «сервисах» и теме без проекта нет",
              f"cur20={cur20 and cur20['value']} cur22={cur22 and cur22['value']}")
        runs = db.conn.execute(
            "SELECT COUNT(*) c FROM extraction_runs WHERE pack='construction' "
            "AND topic_id IN (20, 22)").fetchone()["c"]
        check(runs == 0, "изоляция: прогонов construction по чужим темам нет")
        cur21 = db.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"extract:{chat}:21:construction",)).fetchone()
        check(cur21 is not None,
              "изоляция: construction отработал в стройке",
              f"cur21={cur21 and cur21['value']}")
        svc_topics = {r["topic_id"] for r in db.conn.execute(
            "SELECT DISTINCT topic_id FROM entities WHERE pack='services'")}
        check(svc_topics == {20},
              "изоляция: services извлекал только в «сервисах»",
              str(sorted(svc_topics)))


def test_schema() -> None:
    print("=== схема: contract пакетов ===")
    con = PACKS["construction"]
    check("promise" in con["kinds"], "схема: kind promise объявлен в пакете")
    item = con["json_schema"]["properties"]["ops"]["items"]
    kind_enum = item["properties"]["kind"]["enum"]
    check("promise" in kind_enum, "схема: promise в enum kind",
          str(kind_enum))
    check("supersedes" in item["required"],
          "схема: supersedes в required")
    check(item["properties"]["supersedes"]["type"] == ["integer", "null"],
          "схема: supersedes — nullable integer",
          str(item["properties"]["supersedes"]))
    check("supersedes" in con["kinds"]["decision"]["attrs"],
          "схема: supersedes остался в attrs решения")
    check("promise" in con["prompt_template"] and "supersedes" in con["prompt_template"]
          and "due_ts" in con["prompt_template"],
          "промпт: инструкции про promise, supersedes и due_ts")
    for pack_id, pack in PACKS.items():
        schema = pack["json_schema"]
        op_item = schema["properties"]["ops"]["items"]
        check("covered_upto_msg_id" not in schema["properties"]
              and "covered_upto_msg_id" not in schema["required"],
              f"схема: {pack_id} без covered_upto_msg_id")
        check("confidence" not in op_item["properties"]
              and "confidence" not in op_item["required"],
              f"схема: {pack_id} без confidence")
        check(op_item["properties"]["evidence"].get("minItems") == 1,
              f"схема: {pack_id} evidence с minItems=1")
        check("covered_upto_msg_id" not in pack["prompt_template"],
              f"промпт: {pack_id} без covered_upto_msg_id")
    svc_item = PACKS["services"]["json_schema"]["properties"]["ops"]["items"]
    check("supersedes" not in svc_item["required"]
          and "supersedes" not in svc_item["properties"],
          "схема: services без supersedes")
    stub_prompt = "чат -100999\n[901] 2026-06-15 Иван: задача: сделать ограждение"
    stub = extract_mod.provider_stub(stub_prompt, con)
    check("covered_upto_msg_id" not in stub,
          "stub: провайдер не возвращает covered_upto_msg_id")
    check(stub["ops"] and "confidence" not in stub["ops"][0],
          "stub: операции без confidence")


def test_rejects_json(root: Path) -> None:
    print("=== rejects: причины отклонений пишутся в extraction_runs ===")
    db_path = root / "rejects.sqlite"
    chat, topic = -100952, 11
    msg_ts = ts_utc(2026, 6, 15)
    with DB(db_path) as db:
        db.upsert_chat(chat, "Объект-3", None, "supergroup", True, 6)
        db.upsert_topic(chat, topic, "Объект-3")
        db.watch_add(chat, topic, project="стройка")
        seed_messages(db, chat, topic, {
            201: "задача: подготовить отчёт",
            202: "задача: согласовать график",
            203: "задача: подготовить смету",
            204: "задача: согласовать бюджет",
        }, msg_ts)
        set_cursor(db, chat, topic, "construction", 200)
        add_rejects_col(db)

    pack = PACKS["construction"]

    def bad_op(title, msg, quote):
        return {"op": "new", "kind": "task", "title": title, "entity_id": None,
                "body": None, "owner": None, "due_text": None, "due_ts": None,
                "supersedes": None,
                "evidence": [{"chat_id": chat, "msg_id": msg, "quote": quote}]}

    ops60 = [bad_op(f"Задача {i}", 999, "нет") for i in range(60)]
    made = extract_mod.run(str(db_path), packs=[pack], workers=1,
                           provider=lambda prompt, pk: {"ops": ops60})
    with DB(db_path) as db:
        run = db.conn.execute(
            "SELECT * FROM extraction_runs ORDER BY id DESC LIMIT 1").fetchone()
        check(run["ops_rejected"] == 60 and run["ops_applied"] == 0
              and run["status"] == "partial",
              "rejects: 60 отклонённых, статус partial",
              f"rejected={run['ops_rejected']} applied={run['ops_applied']}")
        rejects = json.loads(run["rejects_json"])
        check(isinstance(rejects, list) and len(rejects) == 50,
              "rejects: rejects_json усечён до 50 записей",
              f"len={len(rejects)}")
        check(all(set(r) == {"op", "kind", "title", "reason", "msg_id"}
                  for r in rejects),
              "rejects: поля записи op/kind/title/reason/msg_id")
        check(rejects[0]["msg_id"] == 999 and rejects[0]["op"] == "new"
              and rejects[0]["kind"] == "task",
              "rejects: значения полей из операции",
              str(rejects[0]))
    check(made and made[0]["ops_rejected"] == 60 and made[0]["ops_overflow"] == 0,
          "rejects: сводка прогона несёт ops_rejected/ops_overflow",
          str(made and made[0]))

    with DB(db_path) as db:
        set_cursor(db, chat, topic, "construction", 202)
    foreign = dict(bad_op("Чужой чат", 204, "бюджет"))
    foreign["evidence"] = [{"chat_id": chat + 1, "msg_id": 204, "quote": "бюджет"}]
    extract_mod.run(str(db_path), packs=[pack], workers=1,
                    provider=lambda prompt, pk: {"ops": [
                        bad_op("Ложная цитата", 203, "нет такой фразы"),
                        foreign,
                    ]})
    with DB(db_path) as db:
        run = db.conn.execute(
            "SELECT * FROM extraction_runs ORDER BY id DESC LIMIT 1").fetchone()
        rejects = json.loads(run["rejects_json"])
        check(run["ops_rejected"] == 2 and len(rejects) == 2,
              "rejects: короткий прогон — обе причины в rejects_json",
              f"rejected={run['ops_rejected']} len={len(rejects)}")
        check(any("quote" in r["reason"] for r in rejects),
              "rejects: причина про цитату видна",
              str([r["reason"] for r in rejects]))


def test_dirty_reprocess(root: Path) -> None:
    print("=== dirty: перепрогон отредактированных сообщений ===")
    db_path = root / "dirty.sqlite"
    chat, topic = -100953, 12
    msg_ts = ts_utc(2026, 6, 15)
    with DB(db_path) as db:
        db.upsert_chat(chat, "Объект-4", None, "supergroup", True, 6)
        db.upsert_topic(chat, topic, "Объект-4")
        db.watch_add(chat, topic, project="стройка")
        seed_messages(db, chat, topic, {
            101: "задача: подготовить площадку",
            102: "задача: заказать бетон",
            103: "задача: подготовить смету",
            104: "задача: согласовать подрядчика",
            105: "задача: финальная проверка",
        }, msg_ts)
        set_cursor(db, chat, topic, "construction", 100)

    pack = PACKS["construction"]

    def op(title, msg, quote, entity_id=None):
        return {"op": "new" if entity_id is None else "update", "kind": "task",
                "title": title, "entity_id": entity_id, "body": None,
                "owner": None, "due_text": None, "due_ts": None,
                "supersedes": None,
                "evidence": [{"chat_id": chat, "msg_id": msg, "quote": quote}]}

    extract_mod.run(str(db_path), packs=[pack], workers=1,
                    provider=lambda prompt, pk: {"ops": [
                        op("Смета", 103, "смету")]})
    with DB(db_path) as db:
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"extract:{chat}:{topic}:construction",)).fetchone()
        check(cur is not None and int(cur["value"]) == 105,
              "dirty: основной курсор дошёл до 105",
              str(cur and cur["value"]))

    with DB(db_path) as db:
        db.conn.execute(
            "UPDATE messages SET text='задача: переделать смету к пятнице' "
            "WHERE chat_id=? AND msg_id=103", (chat,))
        db.conn.commit()
        set_meta(db, f"dirty:{chat}:{topic}", json.dumps([103]))

    prompts: list[str] = []

    def dirty_prov(prompt: str, pk: dict) -> dict:
        prompts.append(prompt)
        return {"ops": [op("Смета v2", 103, "переделать смету", entity_id=1),
                        op("Старое сообщение", 101, "площадку")]}

    extract_mod.run(str(db_path), packs=[pack], workers=1, provider=dirty_prov)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        check(e["title"] == "Смета v2",
              "dirty: отредактированное сообщение перепрогнано, update применён",
              str(e and e["title"]))
        dirty = db.conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"dirty:{chat}:{topic}",)).fetchone()
        check(dirty is None, "dirty: ключ очищен после успешного прогона",
              str(dirty and dirty["value"]))
        cur = db.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"extract:{chat}:{topic}:construction",)).fetchone()
        check(int(cur["value"]) == 105,
              "dirty: основной курсор не двинулся",
              str(cur and cur["value"]))
        run = db.conn.execute(
            "SELECT * FROM extraction_runs ORDER BY id DESC LIMIT 1").fetchone()
        check(run["from_msg"] == 103 and run["upto_msg"] == 103,
              "dirty: прогон записан ровно по msg 103",
              f"from={run['from_msg']} upto={run['upto_msg']}")
        check(run["ops_applied"] == 1 and run["ops_rejected"] == 1,
              "dirty: evidence ниже floor (msg 101) отклонено",
              f"applied={run['ops_applied']} rejected={run['ops_rejected']}")
    check(prompts and "[103]" in prompts[0] and "[101]" not in prompts[0]
          and "[105]" not in prompts[0],
          "dirty: в промпт попало только отредактированное сообщение",
          prompts[0].splitlines()[0] if prompts else "")

    with DB(db_path) as db:
        set_meta(db, f"dirty:{chat}:{topic}", json.dumps([104]))
    extract_mod.run(str(db_path), packs=[pack], workers=1, dry_run=True,
                    provider=lambda prompt, pk: {"ops": []})
    with DB(db_path) as db:
        dirty = db.conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"dirty:{chat}:{topic}",)).fetchone()
        check(dirty is not None and json.loads(dirty["value"]) == [104],
              "dirty: dry-run не очищает ключ",
              str(dirty and dirty["value"]))
    extract_mod.run(str(db_path), packs=[pack], workers=1,
                    provider=lambda prompt, pk: {"ops": []})
    with DB(db_path) as db:
        dirty = db.conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"dirty:{chat}:{topic}",)).fetchone()
        check(dirty is None, "dirty: боевой прогон очищает ключ",
              str(dirty and dirty["value"]))


def test_dedup_scope(root: Path) -> None:
    print("=== dedup_scope: project делит сущности по проектам ===")
    db_path = root / "dedup_scope.sqlite"
    msg_ts = ts_utc(2026, 6, 15)
    scopes = [
        (-100963, 1, "Объект А", "задача: залить фундамент", 301),
        (-100964, 1, "Объект Б", "задача: залить фундамент", 401),
        (-100965, 1, "Объект А", "задача: залить фундамент", 501),
        (-100966, 1, "сервисы", "триал 30 дней на Acme Cloud", 601),
        (-100967, 1, "сервисы", "триал на Acme Cloud", 701),
    ]
    with DB(db_path) as db:
        for chat, topic, project, text, mid in scopes:
            db.upsert_chat(chat, f"Чат {chat}", None, "supergroup", True, 6)
            db.upsert_topic(chat, topic, f"Тема {chat}")
            db.watch_add(chat, topic, project=project)
            seed_messages(db, chat, topic, {mid: text}, msg_ts)
            set_cursor(db, chat, topic, "construction", mid - 1)
            set_cursor(db, chat, topic, "services", mid - 1)

    def scope_prov(prompt: str, pk: dict) -> dict:
        chat_m = extract_mod._CHAT_RE.search(prompt)
        chat_id = int(chat_m.group(1))
        line = next(ln for ln in prompt.splitlines()
                    if extract_mod._LINE_RE.match(ln))
        msg_id = int(extract_mod._LINE_RE.match(line).group(1))
        if pk["id"] == "construction":
            return {"ops": [{"op": "new", "kind": "task",
                             "title": "Заливка фундамента", "entity_id": None,
                             "body": None, "owner": None, "due_text": None,
                             "due_ts": None, "supersedes": None,
                             "evidence": [{"chat_id": chat_id, "msg_id": msg_id,
                                           "quote": "фундамент"}]}]}
        return {"ops": [{"op": "new", "kind": "trial", "title": "Acme Cloud",
                         "entity_id": None, "body": None, "owner": None,
                         "due_text": None, "due_ts": None,
                         "dedup_hint": f"acme{chat_id}",
                         "evidence": [{"chat_id": chat_id, "msg_id": msg_id,
                                       "quote": "Acme"}]}]}

    extract_mod.run(str(db_path), packs=[PACKS["construction"]], workers=1,
                    provider=scope_prov)
    with DB(db_path) as db:
        rows = db.conn.execute(
            "SELECT id, project, title FROM entities WHERE pack='construction' "
            "ORDER BY id").fetchall()
        check(len(rows) == 2 and {r["project"] for r in rows} == {"Объект А", "Объект Б"},
              "dedup_scope: одинаковый title в разных проектах — две сущности",
              str([(r["id"], r["project"]) for r in rows]))
        srcs = db.conn.execute(
            "SELECT COUNT(*) c FROM entity_sources WHERE entity_id=?", (rows[0]["id"],)
        ).fetchone()["c"]
        check(srcs == 2,
              "dedup_scope: тот же проект в другом чате склеился в одну сущность",
              str(srcs))

    extract_mod.run(str(db_path), packs=[PACKS["services"]], workers=1,
                    provider=scope_prov)
    with DB(db_path) as db:
        n = db.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE pack='services'").fetchone()["c"]
        check(n == 2, "dedup_scope: services глобальный — разные hint дают две сущности",
              str(n))

    rep = fuzzy_dedup.run(str(db_path), pack="construction")
    check(rep["clusters"] == 0 and rep["merged"] == 0,
          "dedup_scope: fuzzy не сливает одинаковый title из разных проектов",
          str(rep))
    rep = fuzzy_dedup.run(str(db_path), pack="services")
    check(rep["merged"] == 1,
          "dedup_scope: fuzzy сливает глобальный services по title",
          str(rep))
    with DB(db_path) as db:
        n_con = db.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE pack='construction'").fetchone()["c"]
        n_svc = db.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE pack='services'").fetchone()["c"]
        check(n_con == 2 and n_svc == 1,
              "dedup_scope: итог — construction 2, services 1",
              f"con={n_con} svc={n_svc}")


def test_yo_quote(root: Path) -> None:
    print("=== evidence: ё/е в цитатах валидируются ===")
    db_path = root / "yo_quote.sqlite"
    chat, topic = -100954, 13
    msg_ts = ts_utc(2026, 6, 15)
    with DB(db_path) as db:
        db.upsert_chat(chat, "Объект-5", None, "supergroup", True, 6)
        db.upsert_topic(chat, topic, "Объект-5")
        db.watch_add(chat, topic, project="стройка")
        seed_messages(db, chat, topic, {
            801: "задача: привезти ёмкости на объект",
            802: "задача: установить елку на площадке",
        }, msg_ts)
        set_cursor(db, chat, topic, "construction", 800)

    pack = PACKS["construction"]

    def op(title, msg, quote):
        return {"op": "new", "kind": "task", "title": title, "entity_id": None,
                "body": None, "owner": None, "due_text": None, "due_ts": None,
                "supersedes": None,
                "evidence": [{"chat_id": chat, "msg_id": msg, "quote": quote}]}

    extract_mod.run(str(db_path), packs=[pack], workers=1,
                    provider=lambda prompt, pk: {"ops": [
                        op("Ёмкости", 801, "емкости"),
                        op("Елка", 802, "ёлку"),
                    ]})
    with DB(db_path) as db:
        titles = {r["title"] for r in db.conn.execute(
            "SELECT title FROM entities WHERE pack='construction'")}
        check(titles == {"Ёмкости", "Елка"},
              "ё/е: цитата с е матчит сообщение с ё и наоборот",
              str(titles))


def test_status_transitions(root: Path) -> None:
    print("=== статусы: start/block/unblock, close→done, update не трогает ===")
    db_path = root / "status.sqlite"
    chat, topic = -100952, 3
    msg_ts = ts_utc(2026, 7, 1)
    with DB(db_path) as db:
        db.upsert_chat(chat, "Объект-3", None, "supergroup", True, 6)
        db.upsert_topic(chat, topic, "Объект-3")
        db.watch_add(chat, topic, project="стройка")
        seed_messages(db, chat, topic, {
            801: "задача: согласовать смету",
            802: "взяли смету в работу",
            803: "смета застряла на согласовании",
            804: "блок снят, продолжаем",
            805: "смета готова, закрываем",
            806: "смету переоткрыли",
            807: "уточнили ответственного по смете",
        }, msg_ts)
        set_cursor(db, chat, topic, "construction", 800)
    pack = PACKS["construction"]

    def op(name, msg, quote, entity_id=None):
        return {"op": name, "kind": "task", "title": "Смета", "entity_id": entity_id,
                "body": None, "owner": None, "due_text": None, "due_ts": None,
                "evidence": [{"chat_id": chat, "msg_id": msg, "quote": quote}]}

    def run_ops(ops, cursor):
        with DB(db_path) as db:
            set_cursor(db, chat, topic, "construction", cursor)
        return extract_mod.run(str(db_path), packs=[pack], workers=1,
                               provider=lambda prompt, pk: {"ops": ops})

    def state(db):
        return db.conn.execute(
            "SELECT status, closed_ts FROM entities WHERE id=1").fetchone()

    def last_rej(db):
        return db.conn.execute(
            "SELECT ops_rejected FROM extraction_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()["ops_rejected"]

    run_ops([op("new", 801, "согласовать смету")], 800)
    with DB(db_path) as db:
        s = state(db)
        check(s["status"] == "open" and s["closed_ts"] is None,
              "статус: new → open", f"{s['status']}/{s['closed_ts']}")

    run_ops([op("start", 802, "в работу", entity_id=1)], 801)
    with DB(db_path) as db:
        s = state(db)
        ev = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=1 AND op='start'").fetchone()
        check(s["status"] == "in_progress", "статус: start → in_progress", s["status"])
        check(ev is not None and ev["field"] == "status" and ev["from_val"] == "open"
              and ev["to_val"] == "in_progress",
              "статус: событие start с from/to",
              f"{ev and ev['from_val']}->{ev and ev['to_val']}")

    run_ops([op("block", 803, "застряла", entity_id=1)], 802)
    with DB(db_path) as db:
        check(state(db)["status"] == "blocked", "статус: block → blocked",
              state(db)["status"])

    run_ops([op("start", 804, "продолжаем", entity_id=1)], 803)
    with DB(db_path) as db:
        check(last_rej(db) == 1 and state(db)["status"] == "blocked",
              "статус: start из blocked отклонён",
              f"rej={last_rej(db)} status={state(db)['status']}")

    run_ops([op("unblock", 804, "продолжаем", entity_id=1)], 803)
    with DB(db_path) as db:
        check(state(db)["status"] == "open", "статус: unblock → open",
              state(db)["status"])

    run_ops([op("update", 807, "ответственного", entity_id=1)], 806)
    with DB(db_path) as db:
        check(state(db)["status"] == "open",
              "статус: update не трогает статус", state(db)["status"])

    run_ops([op("close", 805, "закрываем", entity_id=1)], 804)
    with DB(db_path) as db:
        s = state(db)
        check(s["status"] == "done" and s["closed_ts"] is not None,
              "статус: close → done + closed_ts", f"{s['status']}/{s['closed_ts']}")

    run_ops([op("start", 805, "закрываем", entity_id=1)], 804)
    with DB(db_path) as db:
        check(last_rej(db) == 1 and state(db)["status"] == "done",
              "статус: start по закрытой отклонён",
              f"rej={last_rej(db)} status={state(db)['status']}")

    run_ops([op("reopen", 806, "переоткрыли", entity_id=1)], 805)
    with DB(db_path) as db:
        s = state(db)
        check(s["status"] == "open" and s["closed_ts"] is None,
              "статус: reopen → open", f"{s['status']}/{s['closed_ts']}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="packs_semantics_"))
    test_parse_due_text()
    test_due_fallback(root)
    test_supersedes(root)
    test_scope_isolation(root)
    test_schema()
    test_rejects_json(root)
    test_dirty_reprocess(root)
    test_dedup_scope(root)
    test_yo_quote(root)
    test_status_transitions(root)
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
