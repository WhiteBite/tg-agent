#!/usr/bin/env python3
"""Проверки обратной синхронизации export.py (pull) на фейковом транспорте.

Сторонний реестр PULLERS/PROVIDERS грузится через тот же TG_EXTRA_PROVIDERS.
Реально исполняются: превью vs применение, 3-way merge (remote-only, local-only,
converged, conflict), идемпотентность реплея ревизии, запись external_evidence,
событие origin='external' с null chat/msg и цитатой-подстрокой снимка,
эхо-гашение (не шлём обратно в ту же систему, шлём в другую), гейт
TG_ALLOW_PULL + confirm, сохранение dedup_key, запись конфликта в state_json,
маскировка секретов/bidi в снимке, полях и цитате, живучесть конфликта через
no-op pull, ограничение external_evidence 20 ревизиями, отсутствие строки
evidence без применения.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""
os.environ.pop("TG_EXTRA_PROVIDERS", None)

sys.path.insert(0, str(Path(__file__).parent))

import export as export_mod
from db import DB

FAILURES: list[str] = []

FAKE_MODULE = '''
import json
import os

from export import _EntityFailed, _export_entities, _pull_entities


def _fetch(entity_id, external_id):
    with open(os.environ["FAKE_PULL_FILE"], encoding="utf-8") as fh:
        data = json.load(fh)
    rec = data.get(str(external_id))
    if rec is None:
        return None
    if rec.get("fail"):
        raise _EntityFailed("фейковый сбой")
    return rec


def _push(db, system, env_key):
    path = os.environ.get(env_key)
    records = []

    def emit(ent, events, now):
        records.append({"entity": ent["id"], "ops": [e["op"] for e in events],
                        "origins": [e["origin"] for e in events]})
        return None

    exported, failed, error = _export_entities(db, system, emit)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\\n")
    res = {"ok": not failed, "exported": exported}
    if failed:
        res["failed"] = failed
        res["error"] = error
    return res


def _provider_fake(db, out):
    return _push(db, "fake", "FAKE_PUSH_FILE")


def _provider_fake2(db, out):
    return _push(db, "fake2", "FAKE_PUSH2_FILE")


def _pull_fake(db, ctx):
    res = _pull_entities(db, "fake", _fetch, ctx.get("apply"))
    out = {"ok": not res["failed"], "imported": res["imported"], "failed": res["failed"],
           "cursor": res["cursor"], "preview": not ctx.get("apply"), "would": res["would"]}
    if res["error"]:
        out["error"] = res["error"]
    return out


PROVIDERS = {"fake": _provider_fake, "fake2": _provider_fake2}
PULLERS = {"fake": _pull_fake}
'''

ENV_KEYS = ("TG_ALLOW_PULL", "TG_EXTRA_PROVIDERS", "FAKE_PULL_FILE",
            "FAKE_PUSH_FILE", "FAKE_PUSH2_FILE")


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


def set_env(tmp: Path, **kv) -> None:
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["TG_EXTRA_PROVIDERS"] = str(tmp / "fake_providers.py")
    for k, v in kv.items():
        os.environ[k] = str(v)


def write_pull_file(tmp: Path, records: dict) -> str:
    path = tmp / "remote.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return str(path)


def seed_linked(db_path: Path, entity_id: int, external_id: str, title: str,
                base: dict | None = None, system: str = "fake",
                dedup_key: str | None = None, due_ts=None) -> None:
    with DB(db_path) as db:
        db.conn.execute(
            "INSERT INTO entities(id, pack, chat_id, topic_id, kind, title, due_ts, "
            "dedup_key, created_ts, updated_ts) "
            "VALUES(?, 'services', -100999, 7, 'task', ?, ?, ?, 1, 1)",
            (entity_id, title, due_ts, dedup_key))
        db.conn.execute(
            "INSERT INTO integrations(entity_id, system, external_id, state_json, synced_ts) "
            "VALUES(?,?,?,?,0)", (entity_id, system, external_id,
                                  json.dumps({"base": base or {}})))
        db.conn.commit()


def state_of(db_path: Path, entity_id: int, system: str = "fake") -> dict:
    with DB(db_path) as db:
        row = db.conn.execute(
            "SELECT state_json FROM integrations WHERE entity_id=? AND system=?",
            (entity_id, system)).fetchone()
        return json.loads(row["state_json"] or "{}") if row else {}


def title_of(db_path: Path, entity_id: int) -> str:
    with DB(db_path) as db:
        return db.conn.execute(
            "SELECT title FROM entities WHERE id=?", (entity_id,)).fetchone()["title"]


def events_of(db_path: Path, entity_id: int) -> list[dict]:
    with DB(db_path) as db:
        return [dict(r) for r in db.conn.execute(
            "SELECT * FROM entity_events WHERE entity_id=? ORDER BY id", (entity_id,))]


def test_no_puller(tmp: Path) -> None:
    print("=== pull: система без PULLERS — структурная ошибка ===")
    db_path = tmp / "nop.sqlite"
    res = export_mod.run(str(db_path), system="file", direction="pull")
    check(res.get("ok") is False and "нет puller" in res.get("error", ""),
          "pull: нет puller — структурированная ошибка", str(res))


def test_preview_and_gate(tmp: Path) -> None:
    print("=== pull: превью по умолчанию, гейт TG_ALLOW_PULL + confirm ===")
    db_path = tmp / "preview.sqlite"
    seed_linked(db_path, 1, "e1", "Задача", base={"title": "Задача"})
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r1", "snapshot": "Задача в работе\nНовая формулировка",
               "fields": {"title": "Новая задача"}, "quote": "Новая формулировка"}})

    res = export_mod.run(str(db_path), system="fake", direction="pull")
    check(res.get("ok") is True and res.get("preview") is True and res.get("would") == 1
          and res.get("imported") == 0,
          "pull превью: would=1, imported=0, без записи", str(res))
    check(title_of(db_path, 1) == "Задача", "pull превью: сущность не тронута")
    check(len(events_of(db_path, 1)) == 0, "pull превью: событий нет",
          str(events_of(db_path, 1)))
    with DB(db_path) as db:
        ev = db.conn.execute("SELECT COUNT(*) FROM external_evidence").fetchone()[0]
    check(ev == 0, "pull превью: external_evidence пуст", str(ev))

    blocked = export_mod.run(str(db_path), system="fake", direction="pull",
                             apply=True, confirm=True)
    check(blocked.get("ok") is False and "TG_ALLOW_PULL" in blocked.get("error", ""),
          "pull без гейта: применение отклонено", str(blocked))
    check(title_of(db_path, 1) == "Задача", "pull без гейта: сущность не тронута")

    blocked2 = export_mod.run(str(db_path), system="fake", direction="pull",
                              apply=True, confirm=False)
    os.environ["TG_ALLOW_PULL"] = "1"
    blocked3 = export_mod.run(str(db_path), system="fake", direction="pull",
                              apply=True, confirm=False)
    check(blocked2.get("ok") is False and blocked3.get("ok") is False,
          "pull: apply без confirm отклонён даже с гейтом", f"{blocked2} {blocked3}")

    res2 = export_mod.run(str(db_path), system="fake", direction="pull",
                          apply=True, confirm=True)
    check(res2.get("ok") is True and res2.get("imported") == 1,
          "pull с гейтом и confirm: применено", str(res2))
    check(title_of(db_path, 1) == "Новая задача", "pull: удалённое поле применено",
          title_of(db_path, 1))

    evs = events_of(db_path, 1)
    ext = [e for e in evs if e["origin"] == "external"]
    check(len(ext) == 1 and ext[0]["op"] == "update" and ext[0]["field"] == "title"
          and ext[0]["chat_id"] is None and ext[0]["msg_id"] is None
          and ext[0]["from_val"] == "Задача" and ext[0]["to_val"] == "Новая задача",
          "pull: событие origin='external' с null chat/msg", str(ext))

    with DB(db_path) as db:
        row = db.conn.execute(
            "SELECT snapshot, content_hash FROM external_evidence "
            "WHERE system='fake' AND external_id='e1' AND revision='r1'").fetchone()
    check(row is not None and row["snapshot"] == "Задача в работе\nНовая формулировка"
          and row["content_hash"],
          "pull: строка external_evidence со снимком и хэшем",
          str(dict(row) if row else None))
    check(ext[0]["external_evidence_id"] is not None
          and ext[0]["quote"] in row["snapshot"],
          "pull: событие ссылается на external_evidence, цитата — подстрока снимка",
          f"{ext[0]['external_evidence_id']} {ext[0]['quote']!r}")

    replay = export_mod.run(str(db_path), system="fake", direction="pull",
                            apply=True, confirm=True)
    check(replay.get("ok") is True and replay.get("imported") == 0,
          "pull: реплей той же ревизии — без изменений", str(replay))
    check(len(events_of(db_path, 1)) == len(evs),
          "pull: реплей не плодит событий", str(len(events_of(db_path, 1))))


def test_merge_cases(tmp: Path) -> None:
    print("=== pull: 3-way merge remote-only / local-only / converged / conflict ===")
    db_path = tmp / "merge.sqlite"
    seed_linked(db_path, 1, "e1", "Remote", base={"title": "Remote"})
    seed_linked(db_path, 2, "e2", "LocalEdit", base={"title": "Keep"})
    seed_linked(db_path, 3, "e3", "Both", base={"title": "Same"})
    seed_linked(db_path, 4, "e4", "LocalC", base={"title": "Base"})
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r-e1", "snapshot": "Remote2", "fields": {"title": "Remote2"}},
        "e2": {"revision": "r-e2", "snapshot": "Keep", "fields": {"title": "Keep"}},
        "e3": {"revision": "r-e3", "snapshot": "Both", "fields": {"title": "Both"}},
        "e4": {"revision": "r-e4", "snapshot": "RemoteC", "fields": {"title": "RemoteC"}},
    })
    os.environ["TG_ALLOW_PULL"] = "1"
    res = export_mod.run(str(db_path), system="fake", direction="pull",
                         apply=True, confirm=True)
    check(res.get("ok") is True and res.get("imported") == 1,
          "merge: применено ровно одно (remote-only)", str(res))
    check(title_of(db_path, 1) == "Remote2", "merge remote-only: удалённое применено",
          title_of(db_path, 1))
    check(title_of(db_path, 2) == "LocalEdit", "merge local-only: локальное сохранено",
          title_of(db_path, 2))
    check(title_of(db_path, 3) == "Both", "merge converged: значение сошлось",
          title_of(db_path, 3))
    check(title_of(db_path, 4) == "LocalC", "merge conflict: ни одна сторона не перезаписана",
          title_of(db_path, 4))
    check(len([e for e in events_of(db_path, 2)]) == 0
          and len([e for e in events_of(db_path, 3)]) == 0
          and len([e for e in events_of(db_path, 4)]) == 0,
          "merge: local-only/converged/conflict событий не пишут")
    conflicts = state_of(db_path, 4).get("conflicts") or []
    check(len(conflicts) == 1 and conflicts[0]["field"] == "title"
          and conflicts[0]["local"] == "LocalC" and conflicts[0]["remote"] == "RemoteC",
          "merge conflict: записан в state_json", str(conflicts))
    check(state_of(db_path, 1).get("revision") == "r-e1",
          "merge: revision записан в state_json", str(state_of(db_path, 1)))


def test_failed_entity(tmp: Path) -> None:
    print("=== pull: сбой одной сущности не роняет остальные ===")
    db_path = tmp / "failed.sqlite"
    seed_linked(db_path, 1, "e1", "Bad", base={"title": "Bad"})
    seed_linked(db_path, 2, "e2", "Good", base={"title": "Good"})
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r1", "fail": True},
        "e2": {"revision": "r2", "snapshot": "Good2", "fields": {"title": "Good2"}}})
    os.environ["TG_ALLOW_PULL"] = "1"
    res = export_mod.run(str(db_path), system="fake", direction="pull",
                         apply=True, confirm=True)
    check(res.get("ok") is False and res.get("failed") == [1]
          and res.get("imported") == 1,
          "pull: упавшая сущность в failed, здоровая применена", str(res))
    check(title_of(db_path, 2) == "Good2", "pull: здоровая сущность выгружена")


def test_preserve_dedup_key(tmp: Path) -> None:
    print("=== pull: внешняя правка title не трогает dedup_key ===")
    db_path = tmp / "dedup.sqlite"
    seed_linked(db_path, 1, "e1", "Сервис", base={"title": "Сервис"}, dedup_key="stable-key")
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r1", "snapshot": "Сервис v2", "fields": {"title": "Сервис v2"}}})
    os.environ["TG_ALLOW_PULL"] = "1"
    export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    with DB(db_path) as db:
        key = db.conn.execute("SELECT dedup_key FROM entities WHERE id=1").fetchone()["dedup_key"]
    check(key == "stable-key", "pull: dedup_key сохранён при внешней правке title", str(key))


def test_allowlist(tmp: Path) -> None:
    print("=== pull: allowlist — identity/system-поля не перезаписываются ===")
    db_path = tmp / "allow.sqlite"
    seed_linked(db_path, 1, "e1", "Сервис", base={"title": "Сервис"}, dedup_key="stable-key")
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r1", "snapshot": "Сервис v2",
               "fields": {"title": "Сервис v2", "id": 999, "dedup_key": "hacked",
                          "pack": "hacked", "chat_id": -1, "created_ts": 42,
                          "closed_ts": 43, "updated_ts": 44}}})
    os.environ["TG_ALLOW_PULL"] = "1"
    res = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(res.get("ok") is True and res.get("imported") == 1,
          "allowlist: применение без ошибок (updated_ts не даёт дубль колонки)", str(res))
    with DB(db_path) as db:
        row = dict(db.conn.execute(
            "SELECT id, pack, chat_id, dedup_key, created_ts, closed_ts, updated_ts, title "
            "FROM entities WHERE id=1").fetchone())
    check(row["title"] == "Сервис v2", "allowlist: разрешённое поле применено", str(row))
    check(row["id"] == 1 and row["pack"] == "services" and row["chat_id"] == -100999
          and row["dedup_key"] == "stable-key" and row["created_ts"] == 1
          and row["closed_ts"] is None and row["updated_ts"] != 44,
          "allowlist: identity/system-поля не тронуты", str(row))


def test_empty_revision(tmp: Path) -> None:
    print("=== pull: пустая ревизия -> sha1(snapshot), повтор не пропускается ===")
    db_path = tmp / "norev.sqlite"
    seed_linked(db_path, 1, "e1", "T1", base={"title": "T1"})
    os.environ["TG_ALLOW_PULL"] = "1"
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"snapshot": "S1", "fields": {"title": "T2"}}})
    first = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(first.get("ok") is True and first.get("imported") == 1,
          "пустая ревизия: первое применение", str(first))
    check(title_of(db_path, 1) == "T2", "пустая ревизия: title применён", title_of(db_path, 1))
    check(bool(state_of(db_path, 1).get("revision")),
          "пустая ревизия: в state_json записан sha1, не пусто", str(state_of(db_path, 1)))

    replay = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(replay.get("ok") is True and replay.get("imported") == 0,
          "пустая ревизия: реплей того же снимка — без изменений", str(replay))

    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"snapshot": "S2", "fields": {"title": "T3"}}})
    second = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(second.get("ok") is True and second.get("imported") == 1,
          "пустая ревизия: изменённый снимок применяется повторно", str(second))
    check(title_of(db_path, 1) == "T3", "пустая ревизия: новое значение применено",
          title_of(db_path, 1))


def test_per_entity_error(tmp: Path) -> None:
    print("=== pull: не-_EntityFailed сбой одной сущности не роняет остальные ===")
    db_path = tmp / "pererr.sqlite"
    seed_linked(db_path, 1, "e1", "Bad", base={"title": "Bad"})
    seed_linked(db_path, 2, "e2", "Good", base={"title": "Good"})
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r1", "snapshot": "Bad2",
               "fields": {"title": {"nested": "dict"}}},
        "e2": {"revision": "r2", "snapshot": "Good2", "fields": {"title": "Good2"}}})
    os.environ["TG_ALLOW_PULL"] = "1"
    res = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(res.get("ok") is False and res.get("failed") == [1] and res.get("imported") == 1,
          "pull: не-_EntityFailed сбой в failed, здоровая применена", str(res))
    check(title_of(db_path, 2) == "Good2", "pull: здоровая сущность применена после сбоя")
    check(title_of(db_path, 1) == "Bad", "pull: упавшая сущность не изменена")


def test_echo_suppression(tmp: Path) -> None:
    print("=== pull: эхо-гашение — не шлём обратно в ту же систему, шлём в другую ===")
    db_path = tmp / "echo.sqlite"
    seed_linked(db_path, 1, "e1", "Задача", base={"title": "Задача"})
    now = int(time.time())
    with DB(db_path) as db:
        db.conn.execute(
            "INSERT INTO entity_events(entity_id, ts, op, chat_id, msg_id, quote) "
            "VALUES(1, ?, 'new', -100999, 1, 'цитата')", (now - 100,))
        db.conn.commit()
    push_file = str(tmp / "push_fake.jsonl")
    push2_file = str(tmp / "push_fake2.jsonl")
    os.environ["FAKE_PUSH_FILE"] = push_file
    os.environ["FAKE_PUSH2_FILE"] = push2_file

    first = export_mod.run(str(db_path), system="fake")
    check(first.get("ok") is True and first.get("exported") == 1,
          "echo: первичный push в fake выгрузил событие", str(first))

    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r1", "snapshot": "Задача v2", "fields": {"title": "Задача v2"}}})
    os.environ["TG_ALLOW_PULL"] = "1"
    export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    time.sleep(1.1)

    again = export_mod.run(str(db_path), system="fake")
    check(again.get("ok") is True and again.get("exported") == 0,
          "echo: внешнее изменение не уходит обратно в ту же систему", str(again))

    other = export_mod.run(str(db_path), system="fake2")
    recs = [json.loads(x) for x in Path(push2_file).read_text(encoding="utf-8").splitlines()]
    ext_ops = [r for r in recs if "external" in r["origins"]]
    check(other.get("ok") is True and len(ext_ops) == 1,
          "echo: внешнее изменение уходит в другую систему", str(recs))


def test_sanitize_pull(tmp: Path) -> None:
    print("=== pull: секреты и bidi не попадают в entities/entity_events/quote ===")
    db_path = tmp / "sanitize.sqlite"
    seed_linked(db_path, 1, "e1", "Задача", base={"title": "Задача"})
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    bidi = chr(0x202e)
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r1",
               "snapshot": f"Задача{bidi} {secret}\nНовая формулировка",
               "fields": {"title": f"Новая{bidi} задача {secret}",
                          "body": f"тело {secret}"},
               "quote": "Новая формулировка"}})
    os.environ["TG_ALLOW_PULL"] = "1"
    res = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(res.get("ok") is True and res.get("imported") == 1, "sanitize: применение", str(res))
    with DB(db_path) as db:
        row = dict(db.conn.execute("SELECT title, body FROM entities WHERE id=1").fetchone())
    check(secret not in (row["title"] or "") and bidi not in (row["title"] or ""),
          "sanitize: title без секрета и bidi", str(row))
    check(secret not in (row["body"] or "") and bidi not in (row["body"] or ""),
          "sanitize: body без секрета и bidi", str(row))
    evs = events_of(db_path, 1)
    blob = json.dumps(evs, ensure_ascii=False)
    check(secret not in blob and bidi not in blob,
          "sanitize: entity_events без секрета и bidi", blob)
    with DB(db_path) as db:
        ev = db.conn.execute(
            "SELECT snapshot FROM external_evidence WHERE system='fake' "
            "AND external_id='e1' AND revision='r1'").fetchone()
    check(ev is not None and secret not in (ev["snapshot"] or "")
          and bidi not in (ev["snapshot"] or ""),
          "sanitize: снимок external_evidence замаскирован", str(dict(ev) if ev else None))
    ext = [e for e in evs if e["origin"] == "external"]
    check(bool(ext) and ext[0]["quote"] and secret not in ext[0]["quote"]
          and ext[0]["quote"] in ev["snapshot"],
          "sanitize: цитата — подстрока замаскированного снимка", str(ext))


def test_conflict_persistence(tmp: Path) -> None:
    print("=== pull: конфликт переживает no-op pull, снимается при схождении ===")
    db_path = tmp / "conflict.sqlite"
    seed_linked(db_path, 1, "e1", "LocalC", base={"title": "Base"})
    os.environ["TG_ALLOW_PULL"] = "1"
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r1", "snapshot": "RemoteC", "fields": {"title": "RemoteC"}}})
    first = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(first.get("ok") is True and first.get("imported") == 0,
          "conflict: первое применение — конфликт, ничего не применено", str(first))
    c1 = state_of(db_path, 1).get("conflicts") or []
    check(len(c1) == 1 and c1[0]["field"] == "title" and c1[0]["local"] == "LocalC"
          and c1[0]["remote"] == "RemoteC", "conflict: записан", str(c1))

    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r2", "snapshot": "RemoteC", "fields": {}}})
    second = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(second.get("ok") is True and second.get("imported") == 0,
          "conflict: no-op pull ничего не применяет", str(second))
    c2 = state_of(db_path, 1).get("conflicts") or []
    check(len(c2) == 1 and c2[0]["field"] == "title" and c2[0]["local"] == "LocalC"
          and c2[0]["remote"] == "RemoteC",
          "conflict: пережил no-op pull", str(c2))

    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r3", "snapshot": "LocalC", "fields": {"title": "LocalC"}}})
    third = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(third.get("ok") is True, "conflict: схождение применилось", str(third))
    check(not (state_of(db_path, 1).get("conflicts") or []),
          "conflict: снят при схождении", str(state_of(db_path, 1).get("conflicts")))


def test_evidence_bounded(tmp: Path) -> None:
    print("=== pull: external_evidence ограничен 20 ревизиями ===")
    db_path = tmp / "bounded.sqlite"
    seed_linked(db_path, 1, "e1", "T0", base={"title": "T0"})
    os.environ["TG_ALLOW_PULL"] = "1"
    for i in range(1, 26):
        os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
            "e1": {"revision": f"r{i}", "snapshot": f"T{i}", "fields": {"title": f"T{i}"}}})
        export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    with DB(db_path) as db:
        n = db.conn.execute(
            "SELECT COUNT(*) FROM external_evidence WHERE system='fake' "
            "AND external_id='e1'").fetchone()[0]
    check(n == 20, "evidence: не больше 20 ревизий на (system, external_id)", str(n))
    check(title_of(db_path, 1) == "T25", "evidence: последнее значение применено",
          title_of(db_path, 1))


def test_evidence_only_on_apply(tmp: Path) -> None:
    print("=== pull: без применённых полей строка external_evidence не пишется ===")
    db_path = tmp / "noapply.sqlite"
    seed_linked(db_path, 1, "e1", "Same", base={"title": "Same"})
    os.environ["TG_ALLOW_PULL"] = "1"
    os.environ["FAKE_PULL_FILE"] = write_pull_file(tmp, {
        "e1": {"revision": "r1", "snapshot": "Same", "fields": {"title": "Same"}}})
    res = export_mod.run(str(db_path), system="fake", direction="pull", apply=True, confirm=True)
    check(res.get("ok") is True and res.get("imported") == 0,
          "no-apply: ничего не применено", str(res))
    with DB(db_path) as db:
        n = db.conn.execute("SELECT COUNT(*) FROM external_evidence").fetchone()[0]
    check(n == 0, "no-apply: external_evidence пуст", str(n))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_pull_"))
    (tmp / "fake_providers.py").write_text(FAKE_MODULE, encoding="utf-8")
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    try:
        set_env(tmp)
        test_no_puller(tmp)
        set_env(tmp)
        test_preview_and_gate(tmp)
        set_env(tmp)
        test_merge_cases(tmp)
        set_env(tmp)
        test_failed_entity(tmp)
        set_env(tmp)
        test_preserve_dedup_key(tmp)
        set_env(tmp)
        test_allowlist(tmp)
        set_env(tmp)
        test_empty_revision(tmp)
        set_env(tmp)
        test_per_entity_error(tmp)
        set_env(tmp)
        test_echo_suppression(tmp)
        set_env(tmp)
        test_sanitize_pull(tmp)
        set_env(tmp)
        test_conflict_persistence(tmp)
        set_env(tmp)
        test_evidence_bounded(tmp)
        set_env(tmp)
        test_evidence_only_on_apply(tmp)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
