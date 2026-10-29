import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

module = None
if Path("var/private/tiktok-dm-continuation/continuation-local-api.py").is_file():
    spec = importlib.util.spec_from_file_location(
        "mira_continuation", Path("var/private/tiktok-dm-continuation/continuation-local-api.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


@unittest.skipUnless(
    module is not None, "Optional private operations fixture is not in this checkout"
)
class TikTokContinuationControlTests(unittest.TestCase):
    def setUp(self):
        base = Path.cwd() / "tmp" / "tiktok-continuation-tests"
        base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = datetime(2026, 10, 28, 23, 30, tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.db = self.root / "test.db"
        self.entry = {
            "sequence": 1,
            "candidate_id": "candidate-one",
            "task_name": "one task",
            "recipient_id": "peer",
            "conversation_id": "0:1:9:10",
            "parent_message_id": "101",
            "text": "Test reply",
            "task_id": None,
            "requires_acceptance": False,
            "request_acceptance_state": "not_required",
            "manifest_version": 2,
        }
        self.entry["manifest_sha256"] = module.manifest(self.entry)
        state = {
            "account_id": module.ACCOUNT,
            "execution_channel": "local",
            "phase": "candidate_ready",
            "next_scan_at": self.now - 10,
            "entries": [self.entry],
        }
        (self.root / "continuation-plan.json").write_text(json.dumps(state), encoding="utf-8")
        proof = {
            "ready": True,
            "identity": "mira339019",
            "recipient_id": "peer",
            "recipient_verified": True,
            "conversation_verified": True,
            "parent_id_verified": True,
            "unanswered_verified": True,
            "no_duplicate": True,
            "conversation_id": "0:1:9:10",
            "latest_inbound_id": "101",
            "editor_empty": True,
            "observed_at": self.now,
        }
        (self.root / "candidate-001-preflight-proof.json").write_text(
            json.dumps(proof), encoding="utf-8"
        )
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("""
                CREATE TABLE tiktok_dm_messages(account_id TEXT, conversation_id TEXT, message_id TEXT,
                                                direction TEXT, sender_username TEXT, id_verified INTEGER, last_seen REAL);
                CREATE TABLE items(id TEXT, account_id TEXT, payload TEXT);
                CREATE TABLE tasks(id TEXT, name TEXT, account_ids TEXT);
            """)
            db.execute(
                "INSERT INTO tiktok_dm_messages VALUES(?,?,?,?,?,?,?)",
                (module.ACCOUNT, "0:1:9:10", "101", "inbound", "peer", 1, self.now),
            )
        for patcher in (
            patch.object(module, "ROOT", self.root),
            patch.object(module, "STATE", self.root / "continuation-plan.json"),
            patch.object(module.schedule, "DB", self.db),
            patch.object(module.time, "time", return_value=self.now),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_create_set_start_uses_one_checkpoint_and_exact_local_target(self):
        calls = []

        def request(path, method="GET", body=None):
            calls.append((path, method, body))
            return {"id": "task-1", "state": "draft" if path == "/api/tasks" else "active"}

        with (
            patch.object(module.control, "load_environment"),
            patch.object(module.control, "gate"),
            patch.object(module.control, "app_request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            module.main("create", 1)
            with self.assertRaises(ValueError):
                module.main("create", 1)
            module.main("set", 1)
            module.main("start", 1)
        cp = json.loads((self.root / "candidate-001-api-state.json").read_text(encoding="utf-8"))
        self.assertEqual(cp["status"], "started")
        self.assertEqual([x[1] for x in calls], ["POST", "PUT", "POST"])
        self.assertEqual(calls[0][2]["policy"]["execution_channel"], "local")
        self.assertEqual(calls[1][2][0]["tiktok_message_id"], "101")
        self.assertEqual(calls[1][2][0]["text"], "Test reply")

    def test_duplicate_parent_and_stale_context_are_rejected(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO items VALUES(?,?,?)",
                (
                    "existing",
                    module.ACCOUNT,
                    json.dumps({"tiktok_conversation_id": "0:1:9:10", "tiktok_message_id": "101"}),
                ),
            )
        with self.assertRaisesRegex(ValueError, "Dedupe"):
            module.verify_candidate(self.entry, self.now)
        proof_path = self.root / "candidate-001-preflight-proof.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["observed_at"] = self.now - 121
        proof_path.write_text(json.dumps(proof), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "FreshContext"):
            module.verify_candidate(self.entry, self.now)

    def test_incomplete_context_proof_cannot_authorize_a_new_reply(self):
        proof_path = self.root / "candidate-001-preflight-proof.json"
        original = json.loads(proof_path.read_text(encoding="utf-8"))
        for key in (
            "recipient_verified",
            "conversation_verified",
            "parent_id_verified",
            "unanswered_verified",
            "no_duplicate",
        ):
            with self.subTest(key=key):
                proof = {**original, key: False}
                proof_path.write_text(json.dumps(proof), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "FreshContext"):
                    module.verify_candidate(self.entry, self.now)

    def test_mutated_manifest_and_unaccepted_request_are_rejected(self):
        p = self.root / "continuation-plan.json"
        state = json.loads(p.read_text(encoding="utf-8"))
        state["entries"][0]["text"] = "changed"
        p.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ManifestChanged"):
            module.read_entry(1)
        state["entries"][0] = self.entry.copy()
        state["entries"][0]["requires_acceptance"] = True
        state["entries"][0]["manifest_sha256"] = module.manifest(state["entries"][0])
        state["entries"][0]["request_acceptance_state"] = "pending"
        p.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "RequestNotVerifiedAccepted"):
            module.read_entry(1)


if __name__ == "__main__":
    unittest.main()
