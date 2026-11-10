#!/usr/bin/env python3
"""Офлайн-тесты модуля downloads: источники, задания, дедуп, resume, гейты, MCP."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["TG_ENV_FILE"] = ""
os.environ["TG_DOWNLOADS_DIR"] = os.environ.get("TEST_TMP_DIR", "") or str(
    Path(tempfile.mkdtemp(prefix="tg_downloads_")))

import jobs
import modules
import modules.downloads as dl
import sources
from db import DB, SCHEMA_VERSION
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError
from workspace_api import dispatch

HERE = Path(__file__).parent
TMP = Path(os.environ["TG_DOWNLOADS_DIR"])
FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


class DocumentAttributeVideo:
    def __init__(self, round_message: bool = False):
        self.round_message = round_message


class DocumentAttributeAudio:
    def __init__(self, voice: bool = False):
        self.voice = voice


class DocumentAttributeFilename:
    def __init__(self, file_name: str):
        self.file_name = file_name


class FakeDocument:
    def __init__(self, id: int, size: int = 1000, mime: str = "video/mp4",
                 attributes: list | None = None):
        self.id = id
        self.size = size
        self.mime_type = mime
        self.attributes = attributes if attributes is not None else [DocumentAttributeVideo()]


class FakeMediaDocument:
    def __init__(self, document):
        self.document = document


class FakePhoto:
    def __init__(self, id: int):
        self.id = id


class FakeMediaPhoto:
    def __init__(self, photo):
        self.photo = photo


class FakeMessage:
    def __init__(self, id: int, media):
        self.id = id
        self.media = media
        self.message = None


class FakeEntity:
    def __init__(self, id: int):
        self.id = id


class FakeClient:
    def __init__(self, messages: dict, content, fail_downloads: int = 0,
                 restricted: frozenset = frozenset(), floods: list | None = None,
                 entity_map: dict | None = None):
        self.messages = messages
        self.content = content
        self.download_calls = 0
        self.forward_calls = []
        self._fail_left = fail_downloads
        self._restricted = set(restricted)
        self._floods = list(floods or [])
        self._entity_map = dict(entity_map or {})

    async def get_messages(self, chat_id, ids=None):
        return [self.messages.get(chat_id, {}).get(i) for i in (ids or [])]

    async def download_media(self, msg, file=None):
        self.download_calls += 1
        if self._fail_left > 0:
            self._fail_left -= 1
            raise RuntimeError("скачивание сорвалось")
        Path(file).write_bytes(self.content(msg))
        return file

    async def get_entity(self, peer):
        if isinstance(peer, str) and peer.lower() in ("me", "saved", "self"):
            return FakeEntity(777)
        if peer in self._entity_map:
            return self._entity_map[peer]
        if isinstance(peer, int):
            return FakeEntity(peer)
        raise ValueError(f"Could not find the input entity for {peer!r}")

    async def forward_messages(self, target, msg_id, from_peer=None):
        if self._floods:
            raise self._floods.pop(0)
        if from_peer in self._restricted:
            raise ChatForwardsRestrictedError(None)
        self.forward_calls.append((target, msg_id, from_peer))
        return FakeMessage(5000 + len(self.forward_calls), None)

    async def connect(self):
        pass

    async def disconnect(self):
        pass


def _seed(db_path) -> None:
    with DB(db_path) as db:
        db.upsert_chat(chat_id=-100111, title="Канал A", username="chan_a",
                       kind="channel", is_forum=False, members=None)
        db.upsert_chat(chat_id=-100222, title="Канал B", username="chan_b",
                       kind="channel", is_forum=False, members=None)
        db.conn.execute(
            "INSERT INTO watch(chat_id, topic_id, alias) VALUES(-100111, 1, 'A')")
        db.conn.execute(
            "INSERT INTO folders(folder_id, title, synced_at) VALUES(7, 'F', 0)")
        db.conn.execute(
            "INSERT INTO folder_chats(folder_id, chat_id) VALUES(7, -100222)")
        db.conn.commit()


def _seed_messages(db: DB, rows: list) -> None:
    db.conn.executemany(
        "INSERT INTO messages(chat_id, msg_id, topic_id, date_iso, media, "
        "deleted, ts) VALUES(?,?,1,'2026-10-01',?,?,?)",
        rows)
    db.conn.commit()


def _doc_content(table: dict):
    def content(msg):
        doc = getattr(msg.media, "document", None)
        return table[doc.id if doc else msg.media.photo.id]
    return content


def test_sources() -> None:
    print("=== sources: подборки и резолв спек ===")
    db_path = TMP / "sources.sqlite"
    _seed(db_path)
    with DB(db_path) as db:
        res = sources.collection_save(db, "Видео", [-100111, -100222, -100111])
        check(res["chats"] == [-100222, -100111], "sources: save дедупит и сортирует",
              str(res))
        lst = sources.collection_list(db)
        check(lst and lst[0]["name"] == "Видео" and len(lst[0]["chats"]) == 2,
              "sources: list с чатами и названиями", str(lst)[:120])
        check([c["chat_id"] for c in sources.resolve(db, "watch")] == [-100111],
              "sources: watch")
        check([c["chat_id"] for c in sources.resolve(db, "all")]
              == [-100222, -100111], "sources: all")
        check([c["chat_id"] for c in sources.resolve(db, "folder:7")] == [-100222],
              "sources: folder:<id>")
        check(len(sources.resolve(db, "collection:Видео")) == 2,
              "sources: collection:<имя>")
        check([c["chat_id"] for c in sources.resolve(db, "chat:-100111")] == [-100111],
              "sources: chat по числовому id")
        check([c["chat_id"] for c in sources.resolve(db, "chat:@chan_b")] == [-100222],
              "sources: chat по @username")
        check(len(sources.resolve(db, "watch,folder:7")) == 2,
              "sources: объединение через запятую")
        for bad in ("nope", "collection:Нет", "chat:-999", "folder:abc", "folder:"):
            try:
                sources.resolve(db, bad)
                check(False, f"sources: {bad!r} отклонён", "не упал")
            except ValueError:
                check(True, f"sources: {bad!r} отклонён")
        check(sources.collection_delete(db, "Видео") is True, "sources: delete")
        check(sources.collection_delete(db, "Видео") is False,
              "sources: повторный delete False")
        try:
            sources.collection_save(db, "", [1])
            check(False, "sources: пустое имя отклонено", "не упал")
        except ValueError:
            check(True, "sources: пустое имя отклонено")


def test_jobs_core() -> None:
    print("=== jobs: состояния, claim, retry, отчёт ===")
    db_path = TMP / "jobs_core.sqlite"
    with DB(db_path) as db:
        jid = jobs.create_job(db, "downloads", {"sources": "watch"},
                              [{"chat_id": -1, "msg_id": 3},
                               {"chat_id": -1, "msg_id": 1},
                               {"chat_id": -2, "msg_id": 5}])
        job = jobs.job_get(db, jid)
        check(job["state"] == "planned" and job["counts"]["total"] == 3
              and job["params"] == {"sources": "watch"},
              "jobs: planned с 3 пунктами и params", str(job)[:140])
        check(jobs.claim(db, jid) is True, "jobs: claim planned -> running")
        check(jobs.claim(db, jid) is False, "jobs: повторный claim отклонён")
        item = jobs.item_next(db, jid)
        check(item == {"chat_id": -2, "msg_id": 5},
              "jobs: item_next сортирует по chat,msg", str(item))
        jobs.mark_item(db, jid, -2, 5, "done")
        jobs.mark_item(db, jid, -1, 1, "skipped", "duplicate")
        jobs.mark_item(db, jid, -1, 3, "failed", "boom")
        rep = jobs.report(db, jid)
        check(rep["counts"] == {"total": 3, "done": 1, "skipped": 1, "failed": 1},
              "jobs: отчёт по пунктам", str(rep["counts"]))
        check(rep["skipped"] == {"duplicate": 1}, "jobs: причины пропусков",
              str(rep["skipped"]))
        check(rep["failed_total"] == 1 and rep["failed"][0]["detail"] == "boom",
              "jobs: failed с деталью")
        check(jobs.is_active(db, jid) is True, "jobs: running активен")
        check(jobs.pause(db, jid) is True, "jobs: pause")
        check(jobs.is_active(db, jid) is False, "jobs: paused не активен")
        check(jobs.resume(db, jid) is True, "jobs: resume")
        check(jobs.retry_failed(db, jid) == 1, "jobs: retry_failed сбрасывает упавшие")
        check(jobs.item_next(db, jid) == {"chat_id": -1, "msg_id": 3},
              "jobs: сброшенный пункт снова planned")
        jobs.finish(db, jid, "done", result=rep)
        job = jobs.job_get(db, jid)
        check(job["state"] == "done" and job["result"]["counts"]["total"] == 3,
              "jobs: finish + result roundtrip", str(job)[:140])
        check(jobs.claim(db, jid, force=True) is True, "jobs: force забирает done")
        check(jobs.is_active(db, jid) is True, "jobs: force вернул в running")
        jobs.finish(db, jid, "done", result=rep)
        check(jobs.cancel(db, jid) is False, "jobs: cancel после done не работает")
        jobs.create_job(db, "other", {}, [])
        lst = jobs.job_list(db, kind="downloads")
        check(len(lst) == 1 and lst[0]["kind"] == "downloads",
              "jobs: список фильтруется по kind")


def test_plan() -> None:
    print("=== plan: отбор сообщений по источникам/kinds/окну ===")
    db_path = TMP / "plan.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [
            (-100111, 1, "document", 0, now),
            (-100111, 2, "photo", 0, now),
            (-100111, 3, "document", 1, now),
            (-100111, 4, "document", 0, now - 40 * 86400),
            (-100222, 5, "document", 0, now),
        ])
        res = dl.plan_job(db, "watch", kinds="video")
        check(res["items"] == 2 and res["job_id"] > 0
              and [c["chat_id"] for c in res["chats"]] == [-100111],
              "plan: watch + video = 2 документа, без удалённых", str(res["items"]))
        check(res["params"]["kinds"] == ["video"] and res["params"]["max_mb"] == 2000,
              "plan: params зафиксированы", str(res["params"]))
        res = dl.plan_job(db, "watch", kinds="video,photo", days=30)
        check(res["items"] == 2, "plan: days=30 отсекает старое, photo включается",
              str(res["items"]))
        res = dl.plan_job(db, "all", kinds="video")
        check(res["items"] == 3, "plan: all добавляет второй чат", str(res["items"]))
        for bad_kinds in ("", "bogus", "video,smth"):
            try:
                dl.plan_job(db, "watch", kinds=bad_kinds)
                check(False, f"plan: kinds={bad_kinds!r} отклонён", "не упал")
            except ValueError:
                check(True, f"plan: kinds={bad_kinds!r} отклонён")


def test_run_dedup() -> None:
    print("=== downloads: дедуп по doc-id и sha256, источники файла ===")
    db_path = TMP / "dedup.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [
            (-100111, 1, "document", 0, now),
            (-100111, 2, "document", 0, now),
            (-100111, 3, "document", 0, now),
            (-100111, 4, "photo", 0, now),
            (-100111, 5, None, 0, now),
            (-100111, 6, "document", 1, now),
            (-100222, 7, "document", 0, now),
            (-100222, 8, "document", 0, now),
            (-100222, 9, "document", 0, now - 40 * 86400),
        ])
    messages = {
        -100111: {
            1: FakeMessage(1, FakeMediaDocument(FakeDocument(101))),
            2: FakeMessage(2, FakeMediaDocument(FakeDocument(101))),
            3: FakeMessage(3, FakeMediaDocument(FakeDocument(102))),
            4: FakeMessage(4, FakeMediaPhoto(FakePhoto(201))),
            5: FakeMessage(5, None),
            6: FakeMessage(6, FakeMediaDocument(FakeDocument(999))),
        },
        -100222: {
            7: FakeMessage(7, FakeMediaDocument(FakeDocument(103))),
            8: FakeMessage(8, FakeMediaDocument(FakeDocument(102))),
            9: FakeMessage(9, FakeMediaDocument(FakeDocument(110))),
        },
    }
    content = _doc_content({101: b"AAA", 102: b"AAA", 103: b"CCC",
                            110: b"DDD", 201: b"PPP", 999: b"EEE"})
    root = TMP / "dedup_root"

    client = FakeClient(messages, content)
    rep = asyncio.run(dl.run_async(str(db_path), kinds="video,photo", root=root,
                                   client=client))
    check(rep["counts"] == {"total": 4, "done": 2, "skipped": 2},
          "dedup: 4 пункта — 2 скачано, 2 дубля", str(rep["counts"]))
    check(rep["skipped"] == {"duplicate": 1, "duplicate-content": 1},
          "dedup: причины — тот же doc-id и тот же sha", str(rep["skipped"]))

    with DB(db_path) as db:
        rows = dl.files(db)
        check(len(rows) == 2, "dedup: одна строка на уникальное содержимое",
              str(len(rows)))
        video_row = next(r for r in rows if r["kind"] == "video")
        photo_row = next(r for r in rows if r["kind"] == "photo")
        check(video_row["sources"] == 3,
              "dedup: у файла 3 источника (оригинал + дубль doc + дубль sha)",
              str(video_row["sources"]))
        check(photo_row["sources"] == 1, "dedup: фото один источник")
        check(video_row["filename"] is None and video_row["size"] == 3,
              "dedup: атрибуты файла", str(video_row)[:120])
        alias = db.conn.execute(
            "SELECT file_id FROM media_doc_alias WHERE doc_id='doc:102'").fetchone()
        check(alias is not None and alias["file_id"] == video_row["id"],
              "dedup: повторная загрузка того же ролика алиасится на файл")
        check((root / "files" / video_row["sha256"][:2]
               / (video_row["sha256"] + ".mp4")).exists(),
              "dedup: контентно-адресный путь на диске")
        check((root / "files" / photo_row["sha256"][:2]
               / (photo_row["sha256"] + ".jpg")).exists(),
              "dedup: фото с .jpg")
        check(not list((root / "tmp").glob("*.part")), "dedup: временных файлов не осталось")
        check(dl.plan_job(db, "watch", kinds="video,photo")["items"] == 0,
              "dedup: повторный план исключает скачанные сообщения")
        res = dl.plan_job(db, "all", kinds="video,photo", days=30)
        check(res["items"] == 2, "dedup: план all+days — только новые чаты",
              str(res["items"]))
        jid2 = res["job_id"]

    client2 = FakeClient(messages, content)
    rep2 = asyncio.run(dl.run_async(str(db_path), job_id=jid2, root=root,
                                    client=client2))
    check(client2.download_calls == 1,
          "dedup: алиас doc:102 не качается повторно (1 скачивание на 2 пункта)",
          str(client2.download_calls))
    check(rep2["counts"]["done"] == 1 and rep2["counts"]["skipped"] == 1,
          "dedup: второй чат — новый файл + дубль по алиасу", str(rep2["counts"]))

    with DB(db_path) as db:
        check(len(dl.files(db)) == 3, "dedup: в библиотеке 3 файла")
        check(len(dl.files(db, chat_id=-100222)) == 2,
              "dedup: фильтр файлов по чату")

    client3 = FakeClient(messages, content)
    rep3 = asyncio.run(dl.run_async(str(db_path), job_id=jid2, root=root,
                                    client=client3, force=True))
    check(client3.download_calls == 0, "dedup: повторный прогон ничего не качает")
    with DB(db_path) as db:
        check(len(dl.files(db)) == 3, "dedup: файлы не размножаются перезапуском")
        check((root / "files").exists(), "dedup: каталог цел")


def test_run_resume() -> None:
    print("=== downloads: resume упавших пунктов ===")
    db_path = TMP / "resume.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [
            (-100111, 1, "document", 0, now),
            (-100111, 2, "document", 0, now),
            (-100111, 3, "document", 0, now),
        ])
    messages = {-100111: {i: FakeMessage(i, FakeMediaDocument(FakeDocument(200 + i)))
                          for i in (1, 2, 3)}}
    content = _doc_content({201: b"R1", 202: b"R2", 203: b"R3"})
    flaky = FakeClient(messages, content, fail_downloads=2)
    rep = asyncio.run(dl.run_async(str(db_path), kinds="video",
                                   root=TMP / "resume_root", client=flaky))
    check(rep["counts"]["failed"] == 2 and rep["counts"]["done"] == 1,
          "resume: сбойные пункты падают, остальные идут", str(rep["counts"]))
    check(rep["failed"][0]["detail"] == "скачивание сорвалось",
          "resume: причина сбоя записана")
    jid = rep["job_id"]
    good = FakeClient(messages, content)
    rep2 = asyncio.run(dl.run_async(str(db_path), job_id=jid, root=TMP / "resume_root",
                                    client=good, force=True, retry=True))
    check(rep2["counts"]["done"] == 3 and rep2["counts"].get("failed", 0) == 0,
          "resume: retry дожимает упавшие пункты", str(rep2["counts"]))
    with DB(db_path) as db:
        check(len(dl.files(db)) == 3, "resume: все 3 файла на диске")
        job = jobs.job_get(db, jid)
        check(job["state"] == "done" and job["result"]["counts"]["done"] == 3,
              "resume: результат сохранён в задании")


def test_pause_cancel() -> None:
    print("=== downloads: пауза и отмена задания ===")
    db_path = TMP / "pause.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [
            (-100111, 1, "document", 0, now),
            (-100111, 2, "document", 0, now),
        ])
    messages = {-100111: {i: FakeMessage(i, FakeMediaDocument(FakeDocument(300 + i)))
                          for i in (1, 2)}}
    content = _doc_content({301: b"P1", 302: b"P2"})
    root = TMP / "pause_root"
    with DB(db_path) as db:
        jid = dl.plan_job(db, "watch", kinds="video")["job_id"]

    class PausingClient(FakeClient):
        async def download_media(self, msg, file=None):
            Path(file).write_bytes(self.content(msg))
            with DB(db_path) as db:
                jobs.pause(db, jid)
            return file

    rep = asyncio.run(dl.run_async(str(db_path), job_id=jid, root=root,
                                   client=PausingClient(messages, content)))
    check(rep["counts"]["done"] == 1 and rep["counts"].get("planned", 0) == 1,
          "pause: первый пункт скачан, второй остался planned", str(rep["counts"]))
    with DB(db_path) as db:
        check(jobs.job_get(db, jid)["state"] == "paused", "pause: состояние paused")
    good = FakeClient(messages, content)
    rep2 = asyncio.run(dl.run_async(str(db_path), job_id=jid, root=root,
                                    client=good, force=True))
    check(rep2["counts"]["done"] == 2, "pause: resume дожимает остаток",
          str(rep2["counts"]))

    with DB(db_path) as db:
        jid2 = dl.plan_job(db, "watch", kinds="video", include_downloaded=True)["job_id"]
        jobs.cancel(db, jid2)
    try:
        asyncio.run(dl.run_async(str(db_path), job_id=jid2, root=root,
                                 client=FakeClient(messages, content), force=True))
        check(False, "cancel: отменённое задание не берётся в работу", "не упал")
    except RuntimeError as exc:
        check("не взято" in str(exc), "cancel: отменённое задание не берётся в работу",
              str(exc)[:80])
    with DB(db_path) as db:
        check(jobs.job_get(db, jid2)["state"] == "cancelled",
              "cancel: состояние не изменилось")


def test_skip_reasons() -> None:
    print("=== downloads: причины пропуска пунктов ===")
    db_path = TMP / "skip.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [
            (-100111, 1, "document", 0, now),
            (-100111, 2, "document", 0, now),
            (-100111, 3, "document", 0, now),
            (-100111, 4, "document", 0, now),
            (-100222, 6, "document", 0, now),
        ])
    messages = {
        -100111: {
            1: FakeMessage(1, FakeMediaDocument(FakeDocument(401, size=3_000_000))),
            3: FakeMessage(3, FakeMediaDocument(
                FakeDocument(402, attributes=[DocumentAttributeAudio(voice=True)]))),
            4: FakeMessage(4, FakeMediaDocument(FakeDocument(
                403, attributes=[DocumentAttributeVideo(round_message=True)]))),
        },
        -100222: {
            6: FakeMessage(6, FakeMediaDocument(FakeDocument(404))),
        },
    }

    class UnavailableChatClient(FakeClient):
        async def get_messages(self, chat_id, ids=None):
            if chat_id == -100222:
                raise ValueError("Could not find the input entity")
            return await super().get_messages(chat_id, ids)

    content = _doc_content({401: b"B1", 402: b"B2", 403: b"B3", 404: b"B4"})
    with DB(db_path) as db:
        jid = dl.plan_job(db, "all", kinds="video,videonote", max_mb=1)["job_id"]
    rep = asyncio.run(dl.run_async(str(db_path), job_id=jid, root=TMP / "skip_root",
                                   client=UnavailableChatClient(messages, content)))
    check(rep["skipped"] == {"chat-unavailable": 1, "kind": 1, "missing": 1,
                             "too-big": 1},
          "skip: чат недоступен, голосовое мимо kinds, сообщение удалено, лимит",
          str(rep["skipped"]))
    check(rep["counts"]["done"] == 1, "skip: videonote скачивается при kinds=videonote",
          str(rep["counts"]))


def test_forward_run() -> None:
    print("=== forward: пересылка без скачивания ===")
    db_path = TMP / "fwd_run.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [
            (-100111, 1, "document", 0, now),
            (-100111, 2, "photo", 0, now),
        ])
    messages = {
        -100111: {
            1: FakeMessage(1, FakeMediaDocument(FakeDocument(501))),
            2: FakeMessage(2, FakeMediaPhoto(FakePhoto(502))),
        },
    }
    content = _doc_content({501: b"F1", 502: b"F2"})
    root = TMP / "fwd_run_root"
    with DB(db_path) as db:
        res = dl.plan_job(db, "watch", kinds="video,photo", mode="forward", to="saved")
        check(res["items"] == 2 and res["params"]["mode"] == "forward"
              and res["params"]["to"] == "saved",
              "forward: план с mode/to", str(res["params"]))
        try:
            dl.plan_job(db, "watch", mode="forward")
            check(False, "forward: без to план отклонён", "не упал")
        except ValueError:
            check(True, "forward: без to план отклонён")
    client = FakeClient(messages, content)
    rep = asyncio.run(dl.run_async(str(db_path), job_id=res["job_id"], root=root,
                                   client=client))
    check(rep["mode"] == "forward" and rep["counts"] == {"total": 2, "done": 2},
          "forward: оба пункта пересланы", str(rep["counts"]))
    check(client.download_calls == 0, "forward: скачивания нет — байты не идут")
    check(len(client.forward_calls) == 2, "forward: по одной пересылке на сообщение",
          str(len(client.forward_calls)))
    check(not (root / "files").exists(), "forward: файловой библиотеки не появилось")
    with DB(db_path) as db:
        rows = dl.forwards(db)
        check(len(rows) == 2 and all(r["target_chat_id"] == 777 for r in rows),
              "forward: реестр пересылок с адресатом saved", str(rows)[:140])
        check(all(r["sources"] == 1 for r in rows), "forward: у каждого по 1 источнику")
        check(dl.plan_job(db, "watch", kinds="video,photo", mode="forward",
                          to="saved")["items"] == 0,
              "forward: повторный план исключает пересланные сообщения")


def test_forward_dedup() -> None:
    print("=== forward: дедуп по id файла и адресату ===")
    db_path = TMP / "fwd_dedup.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [
            (-100111, 1, "document", 0, now),
            (-100111, 2, "document", 0, now),
            (-100222, 8, "document", 0, now),
        ])
    messages = {
        -100111: {
            1: FakeMessage(1, FakeMediaDocument(FakeDocument(601))),
            2: FakeMessage(2, FakeMediaDocument(FakeDocument(602))),
        },
        -100222: {
            8: FakeMessage(8, FakeMediaDocument(FakeDocument(602))),
        },
    }
    content = _doc_content({601: b"D1", 602: b"D2"})
    root = TMP / "fwd_dedup_root"
    client = FakeClient(messages, content)
    rep = asyncio.run(dl.run_async(str(db_path), sources_spec="all", kinds="video",
                                   mode="forward", to="saved", root=root,
                                   client=client))
    check(rep["counts"] == {"total": 3, "done": 2, "skipped": 1},
          "forward: дубль по doc-id пропущен", str(rep["counts"]))
    check(rep["skipped"] == {"duplicate": 1}, "forward: причина пропуска duplicate",
          str(rep["skipped"]))
    check(len(client.forward_calls) == 2, "forward: пересылок ровно 2")
    with DB(db_path) as db:
        rows = dl.forwards(db)
        check(len(rows) == 2, "forward: в реестре 2 медиа", str(len(rows)))
        dup = next(r for r in rows if r["doc_id"] == "doc:602")
        check(dup["sources"] == 2, "forward: у дубля 2 источника-сообщения", str(dup))
        jid = dl.plan_job(db, "all", kinds="video", mode="forward", to="saved",
                          include_downloaded=True)["job_id"]
    client2 = FakeClient(messages, content)
    rep2 = asyncio.run(dl.run_async(str(db_path), job_id=jid, force=True, root=root,
                                    client=client2))
    check(rep2["counts"].get("skipped", 0) == 3 and len(client2.forward_calls) == 0,
          "forward: повторный прогон того же адресата ничего не пересылает",
          str(rep2["counts"]))
    client3 = FakeClient(messages, content,
                         entity_map={"@chan_b": FakeEntity(-100222)})
    rep3 = asyncio.run(dl.run_async(str(db_path), sources_spec="all", kinds="video",
                                    mode="forward", to="@chan_b",
                                    include_downloaded=True, root=root,
                                    client=client3))
    check(rep3["counts"] == {"total": 3, "done": 2, "skipped": 1}
          and len(client3.forward_calls) == 2,
          "forward: другой адресат — свои пересылки (ключ doc_id+target)",
          str(rep3["counts"]))
    with DB(db_path) as db:
        rows = dl.forwards(db)
        check(len(rows) == 4 and {r["target_chat_id"] for r in rows} == {777, -100222},
              "forward: реестр по обоим адресатам", str(len(rows)))


def test_forward_skip() -> None:
    print("=== forward: причины пропуска и ошибки адресата ===")
    db_path = TMP / "fwd_skip.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [
            (-100111, 1, "document", 0, now),
            (-100111, 2, "document", 0, now),
            (-100111, 3, "document", 0, now),
            (-100222, 4, "document", 0, now),
        ])
    messages = {
        -100111: {
            1: FakeMessage(1, FakeMediaDocument(FakeDocument(701, size=3_000_000))),
            2: FakeMessage(2, FakeMediaDocument(FakeDocument(
                702, attributes=[DocumentAttributeAudio(voice=True)]))),
            3: FakeMessage(3, FakeMediaDocument(FakeDocument(703))),
        },
        -100222: {
            4: FakeMessage(4, FakeMediaDocument(FakeDocument(704))),
        },
    }
    content = _doc_content({701: b"S1", 702: b"S2", 703: b"S3", 704: b"S4"})
    client = FakeClient(messages, content, restricted={-100222})
    rep = asyncio.run(dl.run_async(str(db_path), sources_spec="all", kinds="video",
                                   max_mb=1, mode="forward", to="saved",
                                   root=TMP / "fwd_skip_root", client=client))
    check(rep["skipped"] == {"forwards-restricted": 1, "kind": 1, "too-big": 1},
          "forward: запрет пересылки, kinds и лимит", str(rep["skipped"]))
    check(rep["counts"]["done"] == 1 and len(client.forward_calls) == 1,
          "forward: годное видео переслано", str(rep["counts"]))
    with DB(db_path) as db:
        jid = dl.plan_job(db, "watch", kinds="video", mode="forward", to="@nope",
                          include_downloaded=True)["job_id"]
    try:
        asyncio.run(dl.run_async(str(db_path), job_id=jid, force=True,
                                 root=TMP / "fwd_skip_root",
                                 client=FakeClient(messages, content)))
        check(False, "forward: неизвестный адресат валит задание", "не упал")
    except ValueError:
        check(True, "forward: неизвестный адресат валит задание")
    with DB(db_path) as db:
        job = jobs.job_get(db, jid)
        check(job["state"] == "failed" and job.get("error"),
              "forward: ошибка адресата записана в задание",
              str(job.get("error"))[:80])


def test_forward_flood() -> None:
    print("=== forward: FloodWait — длинный паузит, короткий пережидается ===")
    db_path = TMP / "fwd_flood.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [
            (-100111, 1, "document", 0, now),
            (-100111, 2, "document", 0, now),
        ])
    messages = {-100111: {i: FakeMessage(i, FakeMediaDocument(FakeDocument(800 + i)))
                          for i in (1, 2)}}
    content = _doc_content({801: b"W1", 802: b"W2"})
    root = TMP / "fwd_flood_root"

    flooded = FakeClient(messages, content, floods=[FloodWaitError(None, 3600)])
    rep = asyncio.run(dl.run_async(str(db_path), sources_spec="watch", kinds="video",
                                   mode="forward", to="saved", root=root,
                                   client=flooded))
    check(rep["counts"].get("planned", 0) == 2 and rep["counts"].get("done", 0) == 0,
          "flood: длинный FloodWait оставляет пункты planned", str(rep["counts"]))
    with DB(db_path) as db:
        check(jobs.job_get(db, rep["job_id"])["state"] == "paused",
              "flood: задание ушло в pause")
    short = FakeClient(messages, content, floods=[FloodWaitError(None, 1)])
    rep2 = asyncio.run(dl.run_async(str(db_path), job_id=rep["job_id"], force=True,
                                    root=root, client=short))
    check(rep2["counts"] == {"total": 2, "done": 2} and len(short.forward_calls) == 2,
          "flood: короткий FloodWait пережидается, пункты доходят",
          str(rep2["counts"]))


def test_ws_routes() -> None:
    print("=== workspace API: маршруты модуля ===")
    db_path = TMP / "ws.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [(-100111, 1, "document", 0, now)])
        res, code = dispatch(db, "GET", "downloads/jobs", {})
        check(code == 200 and res["jobs"] == [], "ws: пустой список заданий")
        res, code = dispatch(db, "POST", "downloads/job",
                             {"sources": "watch", "kinds": "video"})
        check(code == 200 and res["job_id"] > 0 and res["items"] == 1,
              "ws: создание задания", str(res)[:120])
        jid = res["job_id"]
        res, code = dispatch(db, "GET", "downloads/job", {"id": str(jid)})
        check(code == 200 and res["job"]["id"] == jid
              and res["job"]["state"] == "planned",
              "ws: детали задания")
        res, code = dispatch(db, "GET", "downloads/job", {"id": "999"})
        check(code == 404, "ws: несуществующее задание 404", str(code))
        res, code = dispatch(db, "GET", "downloads/nope", {})
        check(code == 404, "ws: неизвестный маршрут 404", str(code))
        res, code = dispatch(db, "POST", "downloads/job", {"sources": "watch",
                                                           "kinds": "bogus"})
        check(code == 400, "ws: плохие kinds 400", str(code))

        saved = dl._runner
        dl._runner = lambda j: {"ok": True, "pid": -1}
        try:
            res, code = dispatch(db, "POST", "downloads/run", {"job_id": jid})
            check(code == 200 and res["ok"] is True and res["pid"] == -1,
                  "ws: запуск исполнителя", str(res)[:100])
        finally:
            dl._runner = saved
        jobs.claim(db, jid)
        res, _ = dispatch(db, "POST", "downloads/job/pause", {"job_id": jid})
        check(res["ok"] is True, "ws: pause")
        res, _ = dispatch(db, "POST", "downloads/job/resume", {"job_id": jid})
        check(res["ok"] is True, "ws: resume")
        res, _ = dispatch(db, "POST", "downloads/job/cancel", {"job_id": jid})
        check(res["ok"] is True, "ws: cancel")
        res, code = dispatch(db, "POST", "downloads/run", {"job_id": jid})
        check(code == 409, "ws: терминальное задание не запускается (409)", str(code))

        res, code = dispatch(db, "POST", "downloads/collections",
                             {"name": "X", "chat_ids": [-100111]})
        check(code == 200 and res["chats"] == [-100111], "ws: сохранение подборки")
        res, code = dispatch(db, "GET", "downloads/collections", {})
        check(code == 200 and res["collections"][0]["name"] == "X", "ws: список подборок")
        res, code = dispatch(db, "POST", "downloads/collections",
                             {"name": "X", "chat_ids": ["a"]})
        check(code == 400, "ws: chat_ids не числа 400", str(code))
        res, code = dispatch(db, "POST", "downloads/collections/delete", {"name": "X"})
        check(code == 200, "ws: удаление подборки")
        res, code = dispatch(db, "POST", "downloads/collections/delete", {"name": "X"})
        check(code == 404, "ws: повторное удаление 404", str(code))
        res, code = dispatch(db, "GET", "downloads/files", {})
        check(code == 200 and res["files"] == [], "ws: список файлов")
        res, code = dispatch(db, "GET", "downloads/forwards", {})
        check(code == 200 and res["forwards"] == [], "ws: список пересылок")
        res, code = dispatch(db, "POST", "downloads/forward", {"sources": "watch"})
        check(code == 400, "ws: forward без to -> 400", str(code))
        res, code = dispatch(db, "POST", "downloads/forward",
                             {"to": "saved", "kinds": "bogus"})
        check(code == 400, "ws: forward с плохими kinds -> 400", str(code))
        saved_runner = dl._runner
        dl._runner = lambda j: {"ok": True, "pid": -1}
        try:
            res, code = dispatch(db, "POST", "downloads/forward",
                                 {"sources": "watch", "to": "saved"})
            check(code == 200 and res["ok"] is True and res["items"] == 1
                  and res["pid"] == -1,
                  "ws: forward создаёт задание и запускает исполнителя",
                  str(res)[:120])
            fwd_job = res["job_id"]
            job = jobs.job_get(db, fwd_job)
            check(job["params"]["mode"] == "forward"
                  and job["params"]["to"] == "saved",
                  "ws: параметры пересылки в задании", str(job["params"]))
        finally:
            dl._runner = saved_runner


def test_migration() -> None:
    print("=== миграции v6/v7 -> v8: задания, таблицы модуля, пересылки ===")
    p = TMP / "v6.sqlite"
    raw = sqlite3.connect(str(p))
    raw.execute("PRAGMA user_version = 6")
    raw.close()
    with DB(p) as db:
        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        uv = db.conn.execute("PRAGMA user_version").fetchone()[0]
    need = {"jobs", "job_items", "collections", "collection_chats",
            "media_files", "media_doc_alias", "media_file_sources",
            "forward_files", "forward_sources"}
    check(need <= tables, "v6->v8: таблицы заданий и модуля созданы",
          str(sorted(need - tables)))
    check(uv == SCHEMA_VERSION, "v6->v11: user_version поднят", str(uv))
    with DB(p) as db:
        uv2 = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check(uv2 == SCHEMA_VERSION, "v11: повторное открытие без миграций", str(uv2))

    p7 = TMP / "v7.sqlite"
    raw = sqlite3.connect(str(p7))
    raw.execute("PRAGMA user_version = 7")
    raw.close()
    with DB(p7) as db:
        t7 = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        uv7 = db.conn.execute("PRAGMA user_version").fetchone()[0]
    check({"forward_files", "forward_sources"} <= t7,
          "v7->v8: таблицы пересылок добавлены в существующую базу",
          str(sorted({"forward_files", "forward_sources"} - t7)))
    check(uv7 == SCHEMA_VERSION, "v7->v11: user_version поднят", str(uv7))


def test_registry() -> None:
    print("=== реестр модулей и TG_MODULES_OFF ===")
    base_env = {k: v for k, v in os.environ.items()
                if k not in ("TG_MODULES_OFF", "TG_DOWNLOADS_DIR")}
    base_env["TG_ENV_FILE"] = ""
    base_env["TG_DOWNLOADS_DIR"] = str(TMP)

    env = {**base_env, "TG_MODULES_OFF": "downloads"}
    res = subprocess.run(
        [sys.executable, "-c",
         "import modules\n"
         "assert not modules.MODULES, modules.MODULES\n"
         "assert not modules.mcp_specs()\n"
         "assert not modules.workspace_routes()\n"
         "assert modules.spawn_runner('downloads', 1).get('ok') is False\n"
         "print('OK')"],
        capture_output=True, text=True, env=env, cwd=str(HERE), timeout=60)
    check(res.returncode == 0 and "OK" in res.stdout,
          "registry: TG_MODULES_OFF убирает модуль целиком",
          (res.stderr or res.stdout)[-200:])

    res = subprocess.run(
        [sys.executable, "-c",
         "import modules\n"
         "assert set(modules.MODULES) == {'downloads'}\n"
         "assert len(modules.mcp_specs()) == 4\n"
         "assert len(modules.workspace_routes()) == 13\n"
         "print('OK')"],
        capture_output=True, text=True, env=base_env, cwd=str(HERE), timeout=60)
    check(res.returncode == 0 and "OK" in res.stdout,
          "registry: по умолчанию модуль зарегистрирован (4 MCP, 13 маршрутов)",
          (res.stderr or res.stdout)[-200:])

    env = {**base_env, "TG_MODULES_OFF": "downloads"}
    p = str(TMP / "off_schema.sqlite")
    code = (
        "from db import DB\n"
        f"DB({p!r})\n"
        "import sqlite3\n"
        f"con = sqlite3.connect({p!r})\n"
        't = {r[0] for r in con.execute("SELECT name FROM sqlite_master '
        "WHERE type='table'\")}\n"
        "assert 'jobs' in t, t\n"
        "assert 'media_files' not in t, t\n"
        "print('OK')\n"
    )
    res = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, env=env, cwd=str(HERE),
                         timeout=60)
    check(res.returncode == 0 and "OK" in res.stdout,
          "registry: выключенный модуль не создаёт свою схему",
          (res.stderr or res.stdout)[-200:])

    env = {**base_env, "TG_MODULES_OFF": "downloads"}
    res = subprocess.run(
        [sys.executable, "-c",
         "import sys\n"
         "import modules\n"
         "assert 'modules.downloads' not in sys.modules\n"
         "assert 'telethon' not in sys.modules\n"
         "assert 'jobs' not in sys.modules\n"
         "print('OK')"],
        capture_output=True, text=True, env=env, cwd=str(HERE), timeout=60)
    check(res.returncode == 0 and "OK" in res.stdout,
          "registry: выключенный модуль не импортируется вовсе (telethon/jobs не тянутся)",
          (res.stderr or res.stdout)[-200:])

    extra = TMP / "extra_modules.py"
    extra.write_text(
        "def _ping() -> str:\n"
        "    return 'pong'\n"
        "MODULES = {'fake': {'id': 'fake', 'version': 1,\n"
        "                    'mcp': [{'name': 'tg_fake_ping', 'description': 'пинг',\n"
        "                             'fn': _ping, 'gate': 'TG_ALLOW_FAKE'}]}}\n",
        encoding="utf-8")
    env = {**base_env, "TG_EXTRA_MODULES": str(extra)}
    res = subprocess.run(
        [sys.executable, "-c",
         "import modules\n"
         "assert set(modules.MODULES) == {'downloads', 'fake'}, modules.MODULES\n"
         "assert modules.EXTRA_ERROR is None\n"
         "print('OK')"],
        capture_output=True, text=True, env=env, cwd=str(HERE), timeout=60)
    check(res.returncode == 0 and "OK" in res.stdout,
          "registry: TG_EXTRA_MODULES добавляет сторонний модуль поверх встроенных",
          (res.stderr or res.stdout)[-200:])

    extra_list = TMP / "extra_modules_list.py"
    extra_list.write_text(
        "MODULES = [{'id': 'fake', 'version': 1, 'mcp': []}]\n",
        encoding="utf-8")
    env = {**base_env, "TG_EXTRA_MODULES": str(extra_list),
           "TG_MODULES_OFF": "downloads"}
    res = subprocess.run(
        [sys.executable, "-c",
         "import modules\n"
         "assert set(modules.MODULES) == {'fake'}, modules.MODULES\n"
         "print('OK')"],
        capture_output=True, text=True, env=env, cwd=str(HERE), timeout=60)
    check(res.returncode == 0 and "OK" in res.stdout,
          "registry: MODULES списком + TG_MODULES_OFF действует и на сторонние",
          (res.stderr or res.stdout)[-200:])

    env = {**base_env, "TG_EXTRA_MODULES": str(TMP / "no_such_module.py")}
    res = subprocess.run(
        [sys.executable, "-c",
         "import modules\n"
         "assert set(modules.MODULES) == {'downloads'}\n"
         "assert modules.EXTRA_ERROR and modules.EXTRA_ERROR['ok'] is False\n"
         "assert 'TG_EXTRA_MODULES' in modules.EXTRA_ERROR['error']\n"
         "print('OK')"],
        capture_output=True, text=True, env=env, cwd=str(HERE), timeout=60)
    check(res.returncode == 0 and "OK" in res.stdout,
          "registry: битый путь TG_EXTRA_MODULES — EXTRA_ERROR, реестр не падает",
          (res.stderr or res.stdout)[-200:])

    collide = TMP / "collide_module.py"
    collide.write_text(
        "MODULES = {'downloads': {'id': 'downloads', 'version': 1, 'mcp': []}}\n",
        encoding="utf-8")
    env = {**base_env, "TG_EXTRA_MODULES": str(collide)}
    res = subprocess.run(
        [sys.executable, "-c",
         "import modules\n"
         "assert set(modules.MODULES) == {'downloads'}, modules.MODULES\n"
         "assert len(modules.mcp_specs()) == 4, modules.mcp_specs()\n"
         "assert modules.EXTRA_ERROR and modules.EXTRA_ERROR['ok'] is False\n"
         "assert 'downloads' in modules.EXTRA_ERROR['error']\n"
         "print('OK')"],
        capture_output=True, text=True, env=env, cwd=str(HERE), timeout=60)
    check(res.returncode == 0 and "OK" in res.stdout,
          "registry: сторонний модуль с id встроенного -> EXTRA_ERROR, встроенный цел",
          (res.stderr or res.stdout)[-200:])

    env_file = TMP / "off.env"
    env_file.write_text("TG_MODULES_OFF=downloads\n", encoding="utf-8")
    env = {**base_env, "TG_ENV_FILE": str(env_file)}
    res = subprocess.run(
        [sys.executable, "-c",
         "import ui\n"
         "import modules\n"
         "assert 'downloads' not in modules.MODULES, modules.MODULES\n"
         "assert not modules.mcp_specs()\n"
         "print('OK')"],
        capture_output=True, text=True, env=env, cwd=str(HERE), timeout=120)
    check(res.returncode == 0 and "OK" in res.stdout,
          "registry: TG_MODULES_OFF из .env действует до импорта реестра в ui",
          (res.stderr or res.stdout)[-300:])


def _rpc(proc, payload: dict) -> None:
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _wait_for(proc, pred, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if pred(msg):
            return msg
    raise TimeoutError("не дождались ответа MCP-сервера")


def _mcp_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("TG_DB", "TG_ALLOW_MANAGE", "TG_ALLOW_LIVE", "TG_EXPOSE_SQL",
                        "TG_STRICT_WATCH", "TG_ALLOW_DOWNLOADS", "TG_ALLOW_FORWARDS",
                        "TG_MODULES_OFF", "TG_EXTRA_MODULES", "TG_ALLOW_FAKE",
                        "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
                        "TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME")}
    # на Windows пустое значение env-переменной = её удаление, поэтому несуществующий путь
    env["TG_ENV_FILE"] = str(TMP / "absent.env")
    env["TG_DOWNLOADS_DIR"] = str(TMP)
    env.update(extra)
    return env


def _start(env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(HERE / "server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env)


def _init(proc: subprocess.Popen) -> set:
    _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "downloads-test", "version": "0"}}})
    _wait_for(proc, lambda m: m.get("id") == 1)
    _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = _wait_for(proc, lambda m: m.get("id") == 2)
    return {t["name"] for t in tools["result"]["tools"]}


def _call(proc, tool: str, args: dict, rid: int) -> dict:
    _rpc(proc, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": tool, "arguments": args}})
    msg = _wait_for(proc, lambda m: m.get("id") == rid)
    return json.loads(msg["result"]["content"][0]["text"])


def _stop(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_mcp_gate() -> None:
    print("=== MCP: tg_downloads_* за TG_ALLOW_DOWNLOADS / TG_ALLOW_FORWARDS ===")
    db_path = TMP / "mcp.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [(-100111, 1, "document", 0, now)])
    dl_names = {"tg_downloads_run", "tg_downloads_status", "tg_downloads_collections"}

    proc = _start(_mcp_env(TG_DB=str(db_path), TG_ALLOW_DOWNLOADS="1"))
    try:
        names = _init(proc)
        check(dl_names <= names and len(names) == 22,
              "MCP downloads: TG_ALLOW_DOWNLOADS=1 -> 19 базовых + 3 = 22",
              str(sorted(n for n in names if n.startswith("tg_downloads_"))))
        no_confirm = _call(proc, "tg_downloads_run", {"sources": "watch"}, 3)
        check(no_confirm.get("error") and "confirm" in no_confirm["error"],
              "MCP downloads: confirm=False -> JSON-ошибка", str(no_confirm)[:90])
        created = _call(proc, "tg_downloads_run",
                        {"sources": "watch", "confirm": True, "spawn": False}, 4)
        check(created.get("job_id", 0) > 0 and created.get("items") == 1,
              "MCP downloads: задание создано без запуска исполнителя",
              str(created)[:150])
        st = _call(proc, "tg_downloads_status", {"job_id": created["job_id"]}, 5)
        check(st.get("job", {}).get("state") == "planned"
              and st["job"]["counts"]["total"] == 1,
              "MCP downloads: статус видит задание", str(st)[:150])
        cols = _call(proc, "tg_downloads_collections", {}, 6)
        check("collections" in cols, "MCP downloads: коллекции отдаются")
        bad = _call(proc, "tg_downloads_run",
                    {"sources": "nope", "confirm": True, "spawn": False}, 7)
        check(bad.get("error", "").startswith("downloads:"),
              "MCP downloads: плохая спека -> JSON-ошибка, не зависание",
              str(bad)[:90])
        _rpc(proc, {"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
        alive = _wait_for(proc, lambda m: m.get("id") == 8)
        check(len(alive.get("result", {}).get("tools", [])) == 22,
              "MCP downloads: сервер жив после вызовов")
    finally:
        _stop(proc)

    proc2 = _start(_mcp_env(TG_DB=str(db_path)))
    try:
        names2 = _init(proc2)
        check(len(names2) == 19 and not any(n.startswith("tg_downloads_") for n in names2),
              "MCP downloads: без флага 19 инструментов, tg_downloads_* нет",
              str(sorted(n for n in names2 if n.startswith("tg_")))[:120])
    finally:
        _stop(proc2)

    proc3 = _start(_mcp_env(TG_DB=str(db_path), TG_ALLOW_DOWNLOADS="1",
                            TG_MODULES_OFF="downloads"))
    try:
        names3 = _init(proc3)
        check(len(names3) == 19 and not any(n.startswith("tg_downloads_") for n in names3),
              "MCP downloads: TG_MODULES_OFF глушит модуль даже при гейте")
    finally:
        _stop(proc3)

    proc4 = _start(_mcp_env(TG_DB=str(db_path), TG_ALLOW_FORWARDS="1"))
    try:
        names4 = _init(proc4)
        fwd_names = {"tg_downloads_forward", "tg_downloads_status",
                     "tg_downloads_collections"}
        check(fwd_names <= names4 and len(names4) == 22
              and "tg_downloads_run" not in names4,
              "MCP forwards: TG_ALLOW_FORWARDS=1 -> 19 + forward/status/collections",
              str(sorted(n for n in names4 if n.startswith("tg_downloads_"))))
        no_confirm = _call(proc4, "tg_downloads_forward",
                           {"sources": "watch", "to": "saved"}, 9)
        check(no_confirm.get("error") and "confirm" in no_confirm["error"],
              "MCP forwards: confirm=False -> JSON-ошибка", str(no_confirm)[:90])
        created = _call(proc4, "tg_downloads_forward",
                        {"sources": "watch", "to": "saved", "confirm": True,
                         "spawn": False}, 10)
        check(created.get("job_id", 0) > 0
              and created.get("params", {}).get("mode") == "forward"
              and created.get("params", {}).get("to") == "saved",
              "MCP forwards: задание пересылки создано", str(created)[:150])
        st = _call(proc4, "tg_downloads_status", {"job_id": created["job_id"]}, 11)
        check(st.get("job", {}).get("params", {}).get("to") == "saved",
              "MCP forwards: статус видит параметры пересылки", str(st)[:150])
        bad = _call(proc4, "tg_downloads_forward",
                    {"sources": "watch", "to": "nope", "confirm": True,
                     "spawn": False}, 12)
        check(bad.get("error", "").startswith("downloads:"),
              "MCP forwards: плохой адресат -> JSON-ошибка", str(bad)[:90])
    finally:
        _stop(proc4)

    proc5 = _start(_mcp_env(TG_DB=str(db_path), TG_ALLOW_FORWARDS="1",
                            TG_ALLOW_DOWNLOADS="1"))
    try:
        names5 = _init(proc5)
        check(len(names5) == 23,
              "MCP: оба гейта -> 19 базовых + 4 tg_downloads_* = 23",
              str(len(names5)))
    finally:
        _stop(proc5)

    extra = TMP / "gate_module.py"
    extra.write_text(
        "def _ping() -> str:\n"
        "    return 'pong'\n"
        "MODULES = {'fake': {'id': 'fake', 'version': 1,\n"
        "                    'mcp': [{'name': 'tg_fake_ping', 'description': 'пинг',\n"
        "                             'fn': _ping, 'gate': 'TG_ALLOW_FAKE'}]}}\n",
        encoding="utf-8")
    proc6 = _start(_mcp_env(TG_DB=str(db_path), TG_EXTRA_MODULES=str(extra)))
    try:
        names6 = _init(proc6)
        check(len(names6) == 19 and "tg_fake_ping" not in names6,
              "MCP: произвольный гейт модуля выключен — инструмента нет (19)",
              str(len(names6)))
    finally:
        _stop(proc6)

    proc7 = _start(_mcp_env(TG_DB=str(db_path), TG_EXTRA_MODULES=str(extra),
                            TG_ALLOW_FAKE="1"))
    try:
        names7 = _init(proc7)
        check(len(names7) == 20 and "tg_fake_ping" in names7,
              "MCP: произвольный гейт TG_ALLOW_FAKE=1 регистрирует инструмент модуля",
              str(sorted(n for n in names7 if n.startswith("tg_fake"))))
    finally:
        _stop(proc7)

    extra_space = TMP / "gate_module_space.py"
    extra_space.write_text(
        "def _ping() -> str:\n"
        "    return 'pong'\n"
        "MODULES = {'fake': {'id': 'fake', 'version': 1,\n"
        "                    'mcp': [{'name': 'tg_fake_ping', 'description': 'пинг',\n"
        "                             'fn': _ping, 'gate': 'TG_ALLOW_FAKE '}]}}\n",
        encoding="utf-8")
    proc8 = _start(_mcp_env(TG_DB=str(db_path), TG_EXTRA_MODULES=str(extra_space),
                            TG_ALLOW_FAKE="1"))
    try:
        names8 = _init(proc8)
        check(len(names8) == 20 and "tg_fake_ping" in names8,
              "MCP: gate с хвостовым пробелом всё равно регистрирует инструмент",
              str(sorted(n for n in names8 if n.startswith("tg_fake"))))
    finally:
        _stop(proc8)


def test_runner_spawn() -> None:
    print("=== runner: фоновый загрузчик и отказ без кредов ===")
    db_path = TMP / "spawn.sqlite"
    _seed(db_path)
    now = int(time.time())
    with DB(db_path) as db:
        _seed_messages(db, [(-100111, 1, "document", 0, now)])
        jid = dl.plan_job(db, "watch", kinds="video")["job_id"]
    keys = ("TG_DB", "TG_DOWNLOADS_DIR", "TG_ENV_FILE", "TG_MODULES_OFF",
            "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
            "TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["TG_DB"] = str(db_path)
    os.environ["TG_DOWNLOADS_DIR"] = str(TMP)
    os.environ["TG_ENV_FILE"] = str(TMP / "absent.env")
    os.environ.pop("TG_MODULES_OFF", None)
    for k in keys[4:]:
        os.environ.pop(k, None)
    try:
        res = modules.spawn_runner("downloads", jid)
        check(res.get("ok") is True and res.get("pid", 0) > 0,
              "runner: фоновый загрузчик запущен", str(res))
        job = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with DB(db_path) as db:
                job = jobs.job_get(db, jid)
            if job and job["state"] in ("failed", "done", "cancelled"):
                break
            time.sleep(0.5)
        check(job is not None and job["state"] == "failed" and job.get("error"),
              "runner: без кредов задание помечено failed, не зависает",
              f"state={job and job['state']} error={job and job.get('error')}")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    test_sources()
    test_jobs_core()
    test_plan()
    test_run_dedup()
    test_run_resume()
    test_pause_cancel()
    test_skip_reasons()
    test_forward_run()
    test_forward_dedup()
    test_forward_skip()
    test_forward_flood()
    test_ws_routes()
    test_migration()
    test_registry()
    test_mcp_gate()
    test_runner_spawn()
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
