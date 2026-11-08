from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["TG_ENV_FILE"] = ""

import extract
from db import DB
from packs import PACKS


class QuestionSignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="question_signals_")
        self.db = DB(Path(self.tmp.name) / "index.sqlite")
        self.db.insert_message({"chat_id": -1001, "msg_id": 10, "topic_id": 1,
                                "sender_id": 1, "text": "Когда будет доставка?", "ts": 100})
        self.db.conn.execute("INSERT INTO entities(id,pack,chat_id,topic_id,kind,title,status,created_ts,updated_ts) "
                             "VALUES(1,'construction',-1001,1,'question','Доставка','open',100,100)")
        self.db.conn.execute("INSERT INTO entity_events(entity_id,ts,op,chat_id,msg_id,quote) "
                             "VALUES(1,100,'new',-1001,10,'Когда будет доставка?')")
        self.db.conn.execute("INSERT INTO entity_sources(entity_id,chat_id,topic_id,first_seen_ts,last_seen_ts) "
                             "VALUES(1,-1001,1,100,100)")
        self.db.conn.commit()
        self.scope = {"chat_id": -1001, "topic_id": 1}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_unknown_answer_does_not_resolve_question(self):
        rows = [{"msg_id": 11, "sender_id": 2, "reply_to": 10, "text": "Не знаю, уточню", "topic_id": 1}]
        extract._record_question_replies(self.db, PACKS["construction"], self.scope, rows, 1, 200)
        row = self.db.conn.execute("SELECT status,closed_ts FROM entities WHERE id=1").fetchone()
        self.assertEqual(tuple(row), ("open", None), "A reply is not proof that the question is resolved")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM entity_events WHERE op='reply_received'").fetchone()[0], 1)

    def test_aggregate_reactions_do_not_prove_resolution(self):
        self.db.conn.execute("UPDATE messages SET reactions=?", (json.dumps([{"emoji": "👍", "count": 2}]),))
        extract._record_question_reactions(self.db, PACKS["construction"], self.scope, 1, 200)
        row = self.db.conn.execute("SELECT status,closed_ts FROM entities WHERE id=1").fetchone()
        self.assertEqual(tuple(row), ("open", None), "Aggregate reactions do not identify an authorized approver")


if __name__ == "__main__":
    unittest.main()
