from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

os.environ["TG_ENV_FILE"] = ""

import ui
from db import DB


class WorkspaceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="workspace_api_")
        self.db_path = Path(self.tmp.name) / "index.sqlite"
        self.now = int(time.time())
        with DB(self.db_path) as db:
            db.upsert_chat(-100111, "Объект", None, "supergroup", True, 3)
            db.upsert_topic(-100111, 1, "Работы")
            db.upsert_topic(-100111, 2, "Другой проект")
            db.watch_add(-100111, 1, project="А", alias="Работы")
            db.watch_add(-100111, 2, project="Б")
            for mid, topic, text in ((10, 1, "смета поставки"), (11, 1, "удалённая смета"),
                                     (12, 2, "смета другого проекта")):
                db.insert_message({"chat_id": -100111, "msg_id": mid, "topic_id": topic,
                                   "ts": self.now - 100, "sender_id": 5, "sender_name": "Иван", "text": text})
            db.mark_deleted(-100111, 11)
            for eid, kind, project, topic, due in ((1, "task", "А", 1, self.now - 1000),
                                                  (2, "question", "А", 1, None),
                                                  (3, "promise", "А", 1, None),
                                                  (4, "task", "Б", 2, None)):
                db.conn.execute(
                    "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,body,status,owner,due_ts,project,created_ts,updated_ts) "
                    "VALUES(?,'construction',-100111,?,?,?,'Описание','open','Иван',?,?,?,?)",
                    (eid, topic, kind, "Смета " + str(eid), due, project, self.now - 500, self.now - 100))
                db.conn.execute(
                    "INSERT INTO entity_events(entity_id,ts,op,chat_id,msg_id,quote) VALUES(?,?,'new',-100111,?,'смета')",
                    (eid, self.now - 100, 12 if eid == 4 else 10))
            db.conn.commit()
        self.old = (ui.DB_PATH, ui.TOKEN, ui.ALLOWED_HOSTS)
        ui.DB_PATH = str(self.db_path)
        ui.TOKEN = "workspace-test"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
        self.port = self.server.server_address[1]
        ui.ALLOWED_HOSTS = {f"127.0.0.1:{self.port}"}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        ui.DB_PATH, ui.TOKEN, ui.ALLOWED_HOSTS = self.old
        self.tmp.cleanup()

    def request(self, path, body=None, token=True, ctype="application/json"):
        url = f"http://127.0.0.1:{self.port}/api/workspace/{quote(path, safe='/?=&')}"
        if token:
            url += ("&" if "?" in path else "?") + "token=workspace-test"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def request_api(self, path, body=None, token=True, ctype="application/json"):
        url = f"http://127.0.0.1:{self.port}/api/{path}"
        if token:
            url += ("&" if "?" in path else "?") + "token=workspace-test"
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def request_api_get(self, path, token=True):
        url = f"http://127.0.0.1:{self.port}/api/{path}"
        if token:
            url += ("&" if "?" in path else "?") + "token=workspace-test"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_attention_has_real_counts(self):
        status, d = self.request("home")
        self.assertEqual(status, 200)
        self.assertEqual(d["counts"]["overdue"], 1)
        self.assertEqual(d["counts"]["open_questions"], 1)

    def test_entities_filter_and_pagination_total(self):
        status, d = self.request("entities?project=А&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(d["total"], 3)
        self.assertEqual(len(d["items"]), 1)
        self.assertEqual(d["items"][0]["project"], "А")

    def test_invalid_filters_rejected(self):
        self.assertEqual(self.request("entities?sort=DROP&limit=1")[0], 400)
        self.assertEqual(self.request("entities?offset=-1")[0], 400)

    def test_evidence_is_source_grounded(self):
        status, d = self.request("entity?id=1")
        self.assertEqual(status, 200)
        self.assertEqual(d["evidence"][0]["text"], "смета поставки")
        self.assertEqual(d["evidence"][0]["topic_id"], 1)

    def test_review_is_separate_and_stale_rejected(self):
        _, d = self.request("entity?id=1")
        before = d["revision"]
        status, _ = self.request("entity/review", {"id": 1, "state": "approved", "expected_revision": before})
        self.assertEqual(status, 200)
        _, after = self.request("entity?id=1")
        self.assertEqual(after["entity"]["review_state"], "approved")
        self.assertGreater(after["revision"], before)
        status, _ = self.request("entity/edit", {"id": 1, "expected_revision": before, "fields": {"title": "Старая правка"}})
        self.assertEqual(status, 409)
        with DB(self.db_path) as db:
            self.assertEqual(db.conn.execute("SELECT last_read FROM watch WHERE topic_id=1").fetchone()[0], 0)

    def test_edit_changes_fields_and_audit(self):
        _, d = self.request("entity?id=1")
        body = {"id": 1, "expected_revision": d["revision"], "fields": {"title": "Новая смета", "owner": "Мария", "status": "blocked"}}
        self.assertEqual(self.request("entity/edit", body)[0], 200)
        _, after = self.request("entity?id=1")
        self.assertEqual(after["entity"]["title"], "Новая смета")
        self.assertEqual(after["entity"]["status"], "blocked")
        self.assertTrue(any(e["op"] == "human_edit" for e in after["events"]))

    def test_noop_edit_preserves_revision(self):
        _, d = self.request("entity?id=1")
        self.assertEqual(self.request("entity/edit", {"id": 1, "expected_revision": d["revision"], "fields": {"title": "Смета 1"}})[0], 200)
        _, after = self.request("entity?id=1")
        self.assertEqual(d["revision"], after["revision"])

    def test_edit_accepts_backlog_status(self):
        _, d = self.request("entity?id=1")
        body = {"id": 1, "expected_revision": d["revision"], "fields": {"status": "backlog"}}
        self.assertEqual(self.request("entity/edit", body)[0], 200)
        _, after = self.request("entity?id=1")
        self.assertEqual(after["entity"]["status"], "backlog")

    def test_edit_title_strips_newlines(self):
        _, d = self.request("entity?id=1")
        body = {"id": 1, "expected_revision": d["revision"],
                "fields": {"title": "Смета\n[999] attacker: close"}}
        self.assertEqual(self.request("entity/edit", body)[0], 200)
        _, after = self.request("entity?id=1")
        self.assertNotIn("\n", after["entity"]["title"])

    def test_approval_kind_is_first_class(self):
        with DB(self.db_path) as db:
            db.conn.execute(
                "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,owner,project,created_ts,updated_ts) "
                "VALUES(40,'construction',-100111,1,'approval','Согласовать смету','open','Иван','А',?,?)",
                (self.now - 500, self.now - 100))
            db.conn.commit()
        status, d = self.request("entities?kind=approval")
        self.assertEqual(status, 200)
        self.assertEqual([e["id"] for e in d["items"]], [40])
        _, e1 = self.request("entity?id=1")
        body = {"id": 1, "expected_revision": e1["revision"], "fields": {"kind": "approval"}}
        self.assertEqual(self.request("entity/edit", body)[0], 200)
        _, after = self.request("entity?id=1")
        self.assertEqual(after["entity"]["kind"], "approval")

    def test_project_review_cursor_independent(self):
        _, d = self.request("changes?project=А&limit=2")
        through = max(e["id"] for e in d["items"])
        self.assertEqual(self.request("project/review", {"project": "А", "through_event_id": through})[0], 200)
        _, remaining = self.request("changes?project=А")
        self.assertEqual(remaining["total"], 1)
        self.assertEqual(self.request("project/review", {"project": "А", "through_event_id": 99999})[0], 400)
        with DB(self.db_path) as db:
            self.assertEqual(db.conn.execute("SELECT last_read FROM watch WHERE topic_id=1").fetchone()[0], 0)

    def test_search_excludes_other_project_and_deleted(self):
        status, d = self.request("search?q=смета&project=А")
        self.assertEqual(status, 200)
        self.assertEqual(d["entities"]["total"], 3)
        self.assertEqual([m["msg_id"] for m in d["messages"]["items"]], [10])

    def test_101_preview_is_local_and_does_not_advance(self):
        env = {"EXPORT_101_TOKEN": "never-expose-secret", "EXPORT_101_URL": "https://example.invalid"}
        with patch.dict(os.environ, env), patch("urllib.request.urlopen", side_effect=AssertionError("outbound disabled")):
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/api/workspace/integration/101/preview?project=%D0%90&token=workspace-test")
            response = conn.getresponse()
            d = json.loads(response.read())
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(d["preview_only"])
        self.assertFalse(d["contract_verified"])
        self.assertEqual(d["items"][0]["payload"]["title"], "Смета 1")
        self.assertNotIn("never-expose-secret", json.dumps(d))
        with DB(self.db_path) as db:
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM integrations").fetchone()[0], 0)

    def test_101_status_never_claims_verified(self):
        with patch.dict(os.environ, {"EXPORT_101_TOKEN": "hidden-token", "EXPORT_101_URL": "https://example.invalid"}):
            status, d = self.request("integration/101/status")
        self.assertEqual(status, 200)
        self.assertFalse(d["contract_verified"])
        self.assertFalse(d["sending_enabled"])
        self.assertNotIn("hidden-token", json.dumps(d))

    def test_people_mapping_survives_reload(self):
        _, d = self.request("people")
        p = next(p for p in d["items"] if p["owner"] == "Иван")
        self.assertEqual(self.request("people", {"key": p["key"], "display_name": "Иван Петров", "role": "Прораб", "telegram_sender_ids": [5], "crm_member_guid": "member-guid"})[0], 200)
        _, after = self.request("people")
        self.assertEqual(next(p for p in after["items"] if p["key"] == p["owner"])["display_name"], "Иван Петров")

    def test_security_gates(self):
        self.assertEqual(self.request("home", token=False)[0], 401)
        self.assertEqual(self.request("project/topics", {}, ctype="text/plain")[0], 415)

    def test_topics_assignment_validates_all_before_write(self):
        body = {"project": "Новый", "topics": [{"chat_id": -100111, "topic_id": 1}, {"chat_id": -100111, "topic_id": 999}]}
        self.assertEqual(self.request("project/topics", body)[0], 400)
        with DB(self.db_path) as db:
            self.assertEqual(db.conn.execute("SELECT project FROM watch WHERE topic_id=1").fetchone()[0], "А")

    def test_new_source_event_invalidates_previous_approval(self):
        _, d = self.request("entity?id=1")
        self.request("entity/review", {"id": 1, "state": "approved", "expected_revision": d["revision"]})
        with DB(self.db_path) as db:
            db.conn.execute("INSERT INTO entity_events(entity_id,ts,op,field,to_val) VALUES(1,?,'update','owner','Олег')", (self.now,))
            db.conn.commit()
        _, d = self.request("entity?id=1")
        self.assertEqual(d["entity"]["review_state"], "pending")

    def test_response_masks_secrets(self):
        with DB(self.db_path) as db:
            db.conn.execute("UPDATE entities SET body='password: cannot-leak-this' WHERE id=1")
            db.conn.commit()
        status, d = self.request("entity?id=1")
        self.assertEqual(status, 200)
        self.assertNotIn("cannot-leak-this", json.dumps(d))

    def test_deleted_evidence_marked_instead_of_lost(self):
        with DB(self.db_path) as db:
            db.conn.execute("INSERT INTO entity_events(entity_id,ts,op,chat_id,msg_id,quote) "
                            "VALUES(1,?,'update',-100111,11,'удалённая смета')", (self.now,))
            db.conn.commit()
        _, d = self.request("entity?id=1")
        evidence = next(ev for ev in d["evidence"] if ev["msg_id"] == 11)
        self.assertTrue(evidence["deleted"])
        self.assertEqual(evidence["quote"], "удалённая смета")

    def test_no_ui_send_route(self):
        self.assertEqual(self.request("integration/101/send", {})[0], 404)

    def test_fts_prefix_and_malformed_query(self):
        status, d = self.request("search?q=смет*&scope=messages&project=А")
        self.assertEqual(status, 200)
        self.assertEqual(d["messages"]["total"], 1)
        self.assertEqual(self.request('search?q="&scope=messages')[0], 200)

    def test_unknown_project_and_person_validation(self):
        self.assertEqual(self.request("project?project=Нет")[0], 404)
        self.assertEqual(self.request("people", {"key": "Иван", "telegram_sender_ids": ["wrong"]})[0], 400)

    def test_analysis_scopes_entities_and_hides_deleted(self):
        with DB(self.db_path) as db:
            db.conn.execute("INSERT INTO digests(chat_id,topic_id,ts,since_msg,upto_msg,summary) "
                            "VALUES(-100111,1,?,1,10,'Сводка темы')", (self.now,))
            db.conn.execute(
                "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,owner,project,created_ts,updated_ts) "
                "VALUES(5,'services',-100999,7,'trial','Триал','open',NULL,'А',?,?)",
                (self.now - 500, self.now - 100))
            db.conn.execute(
                "INSERT INTO entity_sources(entity_id,chat_id,topic_id,first_seen_ts,last_seen_ts) "
                "VALUES(5,-100111,1,?,?)", (self.now - 500, self.now - 100))
            db.conn.commit()
        status, d = self.request("analysis?chat_id=-100111&topic_id=1")
        self.assertEqual(status, 200)
        self.assertEqual(d["chat"]["title"], "Объект")
        self.assertEqual(d["watch"]["project"], "А")
        self.assertEqual(d["digests"][0]["summary"], "Сводка темы")
        ids = [e["id"] for e in d["entities"]["items"]]
        self.assertEqual(set(ids), {1, 2, 3, 5})
        self.assertTrue(all(e["review_state"] == "pending" for e in d["entities"]["items"]))
        self.assertEqual(d["counts"]["total"], 4)
        self.assertEqual(d["counts"]["open"], 4)
        self.assertEqual(d["counts"]["by_kind"], {"task": 1, "question": 1, "promise": 1, "trial": 1})
        self.assertEqual([g["id"] for g in d["gaps"]["items"]], [2, 3])
        self.assertEqual(d["gaps"]["items"][0]["reason"], "вопрос без ответа")
        self.assertEqual(d["gaps"]["items"][1]["reason"], "обещание без срока")
        self.assertEqual(d["gaps"]["total"], 2)
        self.assertEqual([m["msg_id"] for m in d["messages"]["items"]], [10])

    def test_analysis_validates_chat_and_generalizes_topic(self):
        self.assertEqual(self.request("analysis")[0], 400)
        self.assertEqual(self.request("analysis?chat_id=-100111&topic_id=abc")[0], 400)
        self.assertEqual(self.request("analysis?chat_id=-100222")[0], 404)
        status, d = self.request("analysis?chat_id=-100111&topic_id=0")
        self.assertEqual(status, 200)
        self.assertEqual(d["topic"]["topic_id"], 1)

    def test_bulk_close_stale_trials_only(self):
        with DB(self.db_path) as db:
            for eid, kind, due in ((7, "trial", self.now - 40 * 86400),
                                   (8, "trial", self.now - 5 * 86400),
                                   (9, "task", self.now - 40 * 86400)):
                db.conn.execute(
                    "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,owner,due_ts,project,created_ts,updated_ts) "
                    "VALUES(?, 'services', -100111, 1, ?, ?, 'open', 'Иван', ?, 'А', ?, ?)",
                    (eid, kind, f"Запись {eid}", due, self.now - 500, self.now - 100))
            db.conn.commit()
        status, d = self.request("entity/bulk_close", {"older_than_days": 30})
        self.assertEqual(status, 200)
        self.assertEqual(d["closed"], 1)
        with DB(self.db_path) as db:
            rows = {r["id"]: dict(r) for r in db.conn.execute(
                "SELECT id,status,closed_ts FROM entities WHERE id IN (7,8,9)")}
            self.assertEqual(rows[7]["status"], "done")
            self.assertIsNotNone(rows[7]["closed_ts"])
            self.assertEqual(rows[8]["status"], "open")
            self.assertEqual(rows[9]["status"], "open")
            ev = db.conn.execute(
                "SELECT op,quote FROM entity_events WHERE entity_id=7 ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(ev["op"], "human_edit")
            self.assertIn("протухших", ev["quote"])
        self.assertEqual(self.request("entity/bulk_close", {"older_than_days": 0})[0], 400)
        self.assertEqual(self.request("entity/bulk_close", {"kinds": ["nope"]})[0], 400)

    def test_visit_counters_count_new_since_your_visit(self):
        status, _ = self.request("visit", {"chat_id": -100111, "topic_id": 1})
        self.assertEqual(status, 200)
        with DB(self.db_path) as db:
            db.insert_message({"chat_id": -100111, "msg_id": 20, "topic_id": 1,
                               "ts": self.now + 10, "sender_id": 5, "sender_name": "Иван",
                               "text": "новое после визита"})
        _, d = self.request_api_get("state")
        chat = next(c for c in d["chats"] if c["id"] == -100111)
        w = next(w for w in chat["watched"] if w["topic_id"] == 1)
        self.assertEqual(w["new_since_visit"], 1)
        self.assertEqual(chat["new_total"], 2)
        self.assertEqual(self.request("visit", {"chat_id": -100111, "topic_id": 999})[0], 404)
        self.assertEqual(self.request("visit", {"chat_id": -100111, "topic_id": "x"})[0], 400)
        self.assertEqual(self.request("visit", {})[0], 400)

    def test_home_autocloses_stale_trials_and_drops_review_noise(self):
        with DB(self.db_path) as db:
            for eid, kind, due in ((7, "trial", self.now - 20 * 86400),
                                   (8, "trial", self.now - 3 * 86400),
                                   (9, "task", self.now - 20 * 86400)):
                db.conn.execute(
                    "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,owner,due_ts,project,created_ts,updated_ts) "
                    "VALUES(?, 'services', -100111, 1, ?, ?, 'open', 'Иван', ?, NULL, ?, ?)",
                    (eid, kind, f"Запись {eid}", due, self.now - 500, self.now - 100))
            db.conn.commit()
        status, d = self.request("home")
        self.assertEqual(status, 200)
        with DB(self.db_path) as db:
            e7 = db.conn.execute("SELECT status,closed_ts FROM entities WHERE id=7").fetchone()
            self.assertEqual(e7["status"], "done")
            self.assertEqual(e7["closed_ts"], self.now - 20 * 86400)
            e8 = db.conn.execute("SELECT status FROM entities WHERE id=8").fetchone()
            self.assertEqual(e8["status"], "open")
            ev = db.conn.execute("SELECT op FROM entity_events WHERE entity_id=7 ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(ev["op"], "auto_close")
        self.assertTrue(all("review_pending" not in e["reasons"] for e in d["attention"]))
        self.assertNotIn(7, [e["id"] for e in d["attention"]])
        self.assertIn(8, [e["id"] for e in d["attention"]])
        self.assertIn(9, [e["id"] for e in d["attention"]])
        self.assertGreaterEqual(d["counts"]["review_pending"], 4)

    def test_catalog_rollup_by_dedup_key(self):
        with DB(self.db_path) as db:
            db.upsert_chat(-100222, "Второй чат", None, "supergroup", False, 3)
            rows = [
                (20, "trial", "VPN Example", self.now + 5 * 86400, self.now - 100, "vpn.example", "open", None),
                (21, "trial", "Старый триал", self.now - 2 * 86400, self.now - 90, None, "done", self.now),
                (22, "trial", "Сосед", None, self.now - 80, None, "open", None),
            ]
            for eid, kind, title, due, upd, dk, status, closed in rows:
                db.conn.execute(
                    "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,owner,due_ts,dedup_key,created_ts,updated_ts,closed_ts) "
                    "VALUES(?, 'services', -100111, 1, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                    (eid, kind, title, status, due, dk, self.now - 500, upd, closed))
            for src_chat in (-100111, -100222):
                db.conn.execute(
                    "INSERT INTO entity_sources(entity_id,chat_id,topic_id,first_seen_ts,last_seen_ts) "
                    "VALUES(20, ?, 1, ?, ?)", (src_chat, self.now - 100, self.now - 50))
            db.conn.commit()
        status, d = self.request("catalog")
        self.assertEqual(status, 200)
        vpn = next(g for g in d["items"] if g["key"] == "vpn.example")
        self.assertEqual(vpn["count"], 2)
        self.assertEqual(vpn["open_count"], 1)
        self.assertEqual(vpn["status"], "active")
        self.assertEqual(vpn["next_due"], self.now + 5 * 86400)
        self.assertEqual(vpn["entity_id"], 20)
        self.assertEqual(vpn["title"], "VPN Example")
        self.assertEqual(vpn["chat_count"], 2)
        self.assertIn("Второй чат", vpn["chats"])
        self.assertIn("Объект", vpn["chats"])
        _, expired = self.request("catalog?status=expired")
        self.assertEqual([g["title"] for g in expired["items"]], ["Старый триал"])
        _, active = self.request("catalog?status=active")
        self.assertEqual({g["key"] for g in active["items"]}, {"vpn.example", "id:22"})
        _, q = self.request("catalog?q=vpn")
        self.assertEqual([g["key"] for g in q["items"]], ["vpn.example"])
        self.assertEqual(self.request("catalog?status=nope")[0], 400)

    def test_home_upcoming_and_catalog_week_filter(self):
        with DB(self.db_path) as db:
            for eid, title, dedup, due in ((10, "Триал недели", "week.example", self.now + 3 * 86400),
                                           (11, "Триал далёкий", "far.example", self.now + 30 * 86400)):
                db.conn.execute(
                    "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,owner,due_ts,dedup_key,created_ts,updated_ts) "
                    "VALUES(?, 'services', -100111, 1, 'trial', ?, 'open', NULL, ?, ?, ?, ?)",
                    (eid, title, due, dedup, self.now - 500, self.now - 100))
            db.conn.commit()
        status, d = self.request("home")
        self.assertEqual(status, 200)
        self.assertEqual(d["counts"]["upcoming"], 1)
        _, cat = self.request("catalog?due=upcoming")
        self.assertEqual([g["key"] for g in cat["items"]], ["week.example"])
        self.assertEqual(self.request("catalog?due=bogus")[0], 400)

    def test_alerts_settings_roundtrip(self):
        status, d = self.request("alerts")
        self.assertEqual(status, 200)
        self.assertEqual(d["source"], "env")
        status, saved = self.request("settings/alerts", {"words": ["раздача"], "mentions": ["MyName"]})
        self.assertEqual(status, 200)
        self.assertEqual(saved["mentions"], ["MyName"])
        status, d = self.request("alerts")
        self.assertEqual(d["source"], "ui")
        self.assertEqual(d["words"], ["раздача"])
        self.assertEqual(d["mentions"], ["myname"])
        self.assertEqual(self.request("settings/alerts", {"words": ["ok", "x" * 61]})[0], 400)
        self.assertEqual(self.request("settings/alerts", {})[0], 400)

    def test_catalog_star_roundtrip(self):
        with DB(self.db_path) as db:
            db.conn.execute(
                "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,due_ts,dedup_key,created_ts,updated_ts) "
                "VALUES(30,'services',-100111,1,'trial','Звёздный сервис','open',?, 'star.example', ?, ?)",
                (self.now + 5 * 86400, self.now - 100, self.now - 50))
            db.conn.commit()
        status, d = self.request("catalog?starred=1")
        self.assertEqual(status, 200)
        self.assertEqual(d["total"], 0)
        self.assertEqual(self.request("catalog/star", {"key": "star.example", "starred": True})[0], 200)
        status, d = self.request("catalog?starred=1")
        self.assertEqual([g["key"] for g in d["items"]], ["star.example"])
        self.assertTrue(d["items"][0]["starred"])
        self.assertEqual(self.request("catalog/star", {"key": "star.example", "starred": False})[0], 200)
        self.assertEqual(self.request("catalog?starred=1")[1]["total"], 0)
        self.assertEqual(self.request("catalog/star", {"key": "x" * 201, "starred": True})[0], 400)
        self.assertEqual(self.request("catalog/star", {"key": "star.example"})[0], 400)

    def test_engine_view_and_gate(self):
        status, d = self.request("engine")
        self.assertEqual(status, 200)
        self.assertIn("pipeline", d["engines"])
        self.assertIn("live", d["engines"])
        self.assertFalse(d["enabled"])
        status, err = self.request("engine/start", {"engine": "pipeline"})
        self.assertEqual(status, 403)
        self.assertFalse(err["ok"])
        status, err = self.request("engine/stop", {"engine": "live"})
        self.assertEqual(status, 403)

    def test_links_view_and_media(self):
        with DB(self.db_path) as db:
            db.conn.execute("INSERT INTO refs(chat_id,msg_id,kind,value) "
                            "VALUES(-100111,10,'invite','+AbCdEf')")
            db.conn.execute("INSERT INTO refs(chat_id,msg_id,kind,value) "
                            "VALUES(-100111,12,'url','https://mega.nz/file/AbC#k')")
            db.conn.execute("INSERT INTO media_assets(chat_id,msg_id,kind,status,ts) "
                            "VALUES(-100111,10,'voice','done',?)", (self.now,))
            db.conn.execute("INSERT INTO media_assets(chat_id,msg_id,kind,status,ts) "
                            "VALUES(-100111,11,'voice','pending',?)", (self.now,))
            db.conn.commit()
        status, d = self.request("links")
        self.assertEqual(status, 200)
        self.assertIn("invite", d["groups"])
        status, m = self.request("media")
        self.assertEqual(status, 200)
        self.assertEqual(m["counts"].get("done"), 1)
        self.assertEqual(m["counts"].get("pending"), 1)
        self.assertEqual(m["total"], 2)

    def test_account_lens_filters_read_surfaces(self):
        with DB(self.db_path) as db:
            db.upsert_chat(-100222, "Второй", None, "supergroup", False, 3)
            db.upsert_topic(-100222, 1, "Тема")
            db.watch_add(-100222, 1, project="А", alias="Второй")
            db.conn.execute("INSERT INTO chat_accounts(chat_id, account_id, is_primary) "
                            "VALUES(-100111, 'work', 1)")
            db.conn.execute("INSERT INTO chat_accounts(chat_id, account_id, is_primary) "
                            "VALUES(-100222, 'home', 1)")
            db.conn.execute(
                "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,owner,project,created_ts,updated_ts) "
                "VALUES(50,'construction',-100222,1,'task','Домашняя','open','Иван','А',?,?)",
                (self.now - 500, self.now - 100))
            db.conn.execute(
                "INSERT INTO entity_events(entity_id,ts,op,chat_id,msg_id,quote) "
                "VALUES(50,?,'new',-100222,1,'домашняя')", (self.now - 100,))
            db.conn.execute("INSERT INTO refs(chat_id,msg_id,kind,value) "
                            "VALUES(-100111,10,'invite','+WorkInvite')")
            db.conn.execute("INSERT INTO refs(chat_id,msg_id,kind,value) "
                            "VALUES(-100222,1,'invite','+HomeInvite')")
            db.conn.execute(
                "INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,dedup_key,created_ts,updated_ts) "
                "VALUES(60,'services',-100111,1,'trial','Общий сервис','open','shared.svc',?,?)",
                (self.now - 500, self.now - 100))
            db.conn.execute("INSERT INTO entity_sources(entity_id,chat_id,topic_id) VALUES(60,-100111,1)")
            db.conn.execute("INSERT INTO entity_sources(entity_id,chat_id,topic_id) VALUES(60,-100222,1)")
            db.insert_message({"chat_id": -100222, "msg_id": 1, "topic_id": 1,
                               "ts": self.now + 1000, "sender_id": 5, "sender_name": "Иван",
                               "text": "домашнее"})
            db.conn.execute("INSERT INTO extraction_runs(pack,chat_id,topic_id,ts) "
                            "VALUES('construction',-100111,1,?)", (self.now - 50,))
            db.conn.execute("INSERT INTO extraction_runs(pack,chat_id,topic_id,ts) "
                            "VALUES('construction',-100222,1,?)", (self.now + 500,))
            db.conn.commit()

        _, work = self.request("changes?account=work")
        _, home = self.request("changes?account=home")
        self.assertEqual({e["entity_id"] for e in work["items"]}, {1, 2, 3, 4})
        self.assertEqual({e["entity_id"] for e in home["items"]}, {50})

        _, wp = self.request("projects?account=work")
        _, hp = self.request("projects?account=home")
        wa = next(p for p in wp["items"] if p["name"] == "А")
        ha = next(p for p in hp["items"] if p["name"] == "А")
        self.assertEqual(wa["new_events"], 3)
        self.assertEqual(ha["new_events"], 1)
        self.assertEqual(wa["open"], 3)
        self.assertEqual(ha["open"], 1)
        self.assertEqual({t["chat_id"] for t in wa["topics"]}, {-100111})
        self.assertEqual({t["chat_id"] for t in ha["topics"]}, {-100222})

        _, wl = self.request("links?account=work")
        _, hl = self.request("links?account=home")
        self.assertEqual([c["id"] for c in wl["chats"]], [-100111])
        self.assertEqual([c["id"] for c in hl["chats"]], [-100222])
        self.assertEqual([i["value"] for i in wl["groups"]["invite"]], ["+WorkInvite"])
        self.assertEqual([i["value"] for i in hl["groups"]["invite"]], ["+HomeInvite"])

        _, wc = self.request("catalog?account=work")
        _, hc = self.request("catalog?account=home")
        wg = next(g for g in wc["items"] if g["key"] == "shared.svc")
        hg = next(g for g in hc["items"] if g["key"] == "shared.svc")
        self.assertEqual(wg["chat_count"], 1)
        self.assertEqual(hg["chat_count"], 1)
        self.assertEqual(wg["chats"], ["Объект"])
        self.assertEqual(hg["chats"], ["Второй"])
        _, ac = self.request("catalog")
        self.assertEqual(next(g for g in ac["items"] if g["key"] == "shared.svc")["chat_count"], 2)

        _, wh = self.request("home?account=work")
        _, hh = self.request("home?account=home")
        self.assertEqual(wh["freshness"]["messages_ts"], self.now - 100)
        self.assertEqual(hh["freshness"]["messages_ts"], self.now + 1000)
        self.assertEqual(wh["freshness"]["extraction_ts"], self.now - 50)
        self.assertEqual(hh["freshness"]["extraction_ts"], self.now + 500)

    def test_global_search_gate(self):
        status, d = self.request_api_get("search_global?q=test")
        self.assertEqual(status, 200)
        self.assertFalse(d["ok"])
        self.assertIn("TG_ALLOW_LIVE", d["error"])

    def test_sync_cancel_without_running_sync(self):
        status, d = self.request_api("sync/cancel", {})
        self.assertEqual(status, 409)
        self.assertEqual(d["ok"], False)
        self.assertTrue(isinstance(d["error"], str) and d["error"])

    def test_sync_cancel_security_gates(self):
        self.assertEqual(self.request_api("sync/cancel", token=False)[0], 401)
        self.assertEqual(self.request_api("sync/cancel", {}, ctype="text/plain")[0], 415)


if __name__ == "__main__":
    unittest.main()
