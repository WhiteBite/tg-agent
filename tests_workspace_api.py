from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ["TG_ENV_FILE"] = ""

from db import DB
import ui


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
