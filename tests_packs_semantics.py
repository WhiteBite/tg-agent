#!/usr/bin/env python3
"""Семантика пакетов извлечения: due-text фолбэк, изоляция проекта, supersedes.

Проверяет детерминированный парсинг due_text -> due_ts, exclude_project_regex
в scope-фильтре (construction не заходит в темы проекта «сервисы»), связь
supersedes (закрытие старой сущности + событие superseded_by + отказ на
вне-скоуповый id) и наличие kind promise в схеме пакета construction.
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
from packs import PACKS  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def ts_utc(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


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


def test_parse_due_text() -> None:
    print("=== due_text: детерминированный парсинг дат ===")
    p = extract_mod._parse_due_text
    check(p("20.10.2026", None) == ts_utc(2026, 10, 20),
          "due_text: dd.mm.yyyy -> полночь UTC")
    check(p("до 20.10.2026", None) == ts_utc(2026, 10, 20),
          "due_text: «до dd.mm.yyyy»")
    check(p("сдать до 20.10.2026 включительно", None) == ts_utc(2026, 10, 20),
          "due_text: дата внутри фразы")
    check(p("20.10.26", None) == ts_utc(2026, 10, 20),
          "due_text: dd.mm.yy -> 20xx")
    check(p("01.02.27", None) == ts_utc(2027, 2, 1),
          "due_text: dd.mm.yy с ведущими нулями")
    msg_jun = ts_utc(2026, 6, 15)
    check(p("20.10", msg_jun) == ts_utc(2026, 10, 20),
          "due_text: dd.mm позже даты сообщения -> текущий год")
    msg_nov = ts_utc(2026, 11, 25)
    check(p("20.10", msg_nov) == ts_utc(2027, 10, 20),
          "due_text: dd.mm раньше даты сообщения -> следующий год")
    check(p("20.10", None) is None,
          "due_text: dd.mm без даты сообщения неопределим")
    check(p("к пятнице", msg_jun) is None,
          "due_text: относительная формулировка не парсится")
    check(p("32.13.2026", None) is None,
          "due_text: несуществующая дата отклонена")
    check(p("30 дней", msg_jun) is None,
          "due_text: длительность без точек не парсится")
    check(p("1.5 года", msg_jun) is None,
          "due_text: десятичная длительность не принимается за дату")
    check(p(None, msg_jun) is None and p("", msg_jun) is None,
          "due_text: пустой ввод")


def test_due_fallback(root: Path) -> None:
    print("=== due_text: фолбэк в _apply_new/_apply_update ===")
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
        }, msg_ts)
        set_cursor(db, chat, topic, "construction", 600)

    pack = PACKS["construction"]

    def op(kind, title, msg, quote, due_text=None, due_ts=None, entity_id=None,
           supersedes=None):
        return {"op": "new" if entity_id is None else "update", "kind": kind,
                "title": title, "entity_id": entity_id, "body": None,
                "owner": None, "due_text": due_text, "due_ts": due_ts,
                "confidence": 0.9, "supersedes": supersedes,
                "evidence": [{"chat_id": chat, "msg_id": msg, "quote": quote}]}

    def run_ops(ops, covered):
        return extract_mod.run(str(db_path), packs=[pack], workers=1,
                               provider=lambda prompt, pk: {
                                   "ops": ops, "covered_upto_msg_id": covered})

    run_ops([op("task", "Смета", 601, "смету", due_text="до 20.10.2026")], 601)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        check(e["due_ts"] == ts_utc(2026, 10, 20),
              "фолбэк: «до 20.10.2026» без due_ts заполнил due_ts",
              f"due_ts={e and e['due_ts']}")
        check(e["attrs_json"] and json.loads(e["attrs_json"])["due_text"]
              == "до 20.10.2026",
              "фолбэк: due_text сохранён в attrs_json",
              f"attrs={e and e['attrs_json']}")

    run_ops([op("task", "Окна", 602, "окна", due_text="31.12")], 602)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=2").fetchone()
        check(e["due_ts"] == ts_utc(2026, 12, 31),
              "фолбэк: «31.12» разрешён по дате сообщения (2026-06-15 -> 2026-12-31)",
              f"due_ts={e and e['due_ts']}")

    run_ops([op("task", "Планировка", 603, "планировку",
                due_text="к пятнице")], 603)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=3").fetchone()
        check(e["due_ts"] is None and e["attrs_json"]
              and json.loads(e["attrs_json"])["due_text"] == "к пятнице",
              "фолбэк: неразрешимый due_ts остался None, due_text в attrs_json",
              f"due_ts={e and e['due_ts']} attrs={e and e['attrs_json']}")

    run_ops([op("task", "Цвета", 604, "цвета", due_text="20.10.2026",
                due_ts=1890000000)], 604)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=4").fetchone()
        check(e["due_ts"] == 1890000000,
              "фолбэк: due_ts от модели сильнее due_text",
              f"due_ts={e and e['due_ts']}")

    run_ops([op("task", "Смета v2", 605, "обновить смету", due_text="15.11",
                entity_id=1)], 605)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        check(e["due_ts"] == ts_utc(2026, 11, 15),
              "фолбэк: update с годом без цифр обновил due_ts",
              f"due_ts={e and e['due_ts']}")
        ev = db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=1 AND field='due_ts' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        check(ev is not None and ev["to_val"] == str(ts_utc(2026, 11, 15)),
              "фолбэк: событие due_ts с from/to",
              f"to={ev and ev['to_val']}")

    run_ops([op("task", "Окна v2", 606, "ревизия окон", due_text="к понедельнику",
                entity_id=2)], 606)
    with DB(db_path) as db:
        e = db.conn.execute("SELECT * FROM entities WHERE id=2").fetchone()
        check(e["due_ts"] == ts_utc(2026, 12, 31)
              and json.loads(e["attrs_json"])["due_text"] == "к понедельнику",
              "фолбэк: update с неразрешимым due_text не затёр due_ts",
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
                "confidence": 0.9, "supersedes": supersedes,
                "evidence": [{"chat_id": chat, "msg_id": msg, "quote": quote}]}

    def run_ops(ops, covered):
        return extract_mod.run(str(db_path), packs=[pack], workers=1,
                               provider=lambda prompt, pk: {
                                   "ops": ops, "covered_upto_msg_id": covered})

    def last_run(db):
        return db.conn.execute(
            "SELECT * FROM extraction_runs ORDER BY id DESC LIMIT 1").fetchone()

    run_ops([op("decision", "Фундамент из блоков", 701, "блоков")], 701)
    run_ops([op("decision", "Фундамент монолит", 702, "монолит",
                supersedes=1)], 702)
    with DB(db_path) as db:
        old = db.conn.execute("SELECT * FROM entities WHERE id=1").fetchone()
        new = db.conn.execute("SELECT * FROM entities WHERE id=2").fetchone()
        check(old["closed_ts"] is not None and old["status"] == "closed",
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
             op("decision", "Кровля мягкая", 704, "мягкая", supersedes=2)], 704)
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
                supersedes=5)], 705)
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
                supersedes=1)], 706)
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
                entity_id=3, supersedes=4)], 707)
    with DB(db_path) as db:
        e4 = db.conn.execute("SELECT * FROM entities WHERE id=4").fetchone()
        check(e4["closed_ts"] is not None and e4["status"] == "closed",
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
    print("=== scope: exclude_project_regex изолирует construction от «сервисы» ===")
    con = PACKS["construction"]["scope_filter"]
    svc = PACKS["services"]["scope_filter"]
    check(extract_mod.scope_matches(con, {"chat_id": 1, "project": "сервисы"}) is False,
          "scope: construction не матчится на проект «сервисы»")
    check(extract_mod.scope_matches(con, {"chat_id": 1, "project": "стройка"}) is True,
          "scope: construction матчится на «стройка»")
    check(extract_mod.scope_matches(con, {"chat_id": 1, "project": None}) is True,
          "scope: construction работает в темах без проекта")
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
        check(con_topics == {21, 22},
              "изоляция: construction извлекал только в стройке и теме без проекта",
              str(sorted(con_topics)))
        cur20 = db.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"extract:{chat}:20:construction",)).fetchone()
        check(cur20 is None,
              "изоляция: курсора construction в теме «сервисы» нет",
              str(cur20 and cur20["value"]))
        runs20 = db.conn.execute(
            "SELECT COUNT(*) c FROM extraction_runs WHERE pack='construction' "
            "AND topic_id=20").fetchone()["c"]
        check(runs20 == 0, "изоляция: прогонов construction по теме «сервисы» нет")
        cur21 = db.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"extract:{chat}:21:construction",)).fetchone()
        cur22 = db.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"extract:{chat}:22:construction",)).fetchone()
        check(cur21 is not None and cur22 is not None,
              "изоляция: construction отработал в остальных темах",
              f"cur21={cur21 and cur21['value']} cur22={cur22 and cur22['value']}")
        svc_topics = {r["topic_id"] for r in db.conn.execute(
            "SELECT DISTINCT topic_id FROM entities WHERE pack='services'")}
        check(svc_topics == {20},
              "изоляция: services извлекал только в «сервисах»",
              str(sorted(svc_topics)))


def test_schema() -> None:
    print("=== схема: promise и supersedes в пакете construction ===")
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
    svc_item = PACKS["services"]["json_schema"]["properties"]["ops"]["items"]
    check("supersedes" not in svc_item["required"]
          and "supersedes" not in svc_item["properties"],
          "схема: services без supersedes")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="packs_semantics_"))
    test_parse_due_text()
    test_due_fallback(root)
    test_supersedes(root)
    test_scope_isolation(root)
    test_schema()
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
