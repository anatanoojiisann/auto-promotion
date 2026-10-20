"""Read-only collector contracts using synthetic databases/HTTP/Windows metadata."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_windows.py"
spec = importlib.util.spec_from_file_location("verify_windows", SCRIPT)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
SECRET = "must-not-export-token-or-body"


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="验证 空格 # ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = (
            self.root / "队列 # ?.sqlite" if os.name != "nt" else self.root / "队列 #.sqlite"
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript("""
CREATE TABLE meta(key TEXT,value TEXT);
INSERT INTO meta VALUES('global_stop','0'),('store_schema_revision','1');
CREATE TABLE tasks(id TEXT,state TEXT,phase TEXT,policy TEXT);
CREATE TABLE execution_jobs(state TEXT,task_id TEXT,available_at REAL,expires_at REAL,updated_at REAL,lease_until REAL);
CREATE TABLE pool_workers(heartbeat_at REAL,state TEXT);
CREATE TABLE audit(at REAL,kind TEXT,details TEXT);
CREATE TABLE items(id TEXT,task_id TEXT,status TEXT,attempt_id TEXT,external_reference TEXT);
""")
            policy = {
                "starts_at": "2026-01-01T00:00:00Z",
                "ends_at": "2027-01-01T00:00:00Z",
                "text": SECRET,
            }
            db.execute("INSERT INTO tasks VALUES('t','active','ready',?)", (json.dumps(policy),))
            db.execute("INSERT INTO execution_jobs VALUES('pending','t',0,2000000000,1,0)")

    def test_readonly_unicode_uri_never_exports_policy_or_changes_database(self):
        before = self.db_path.read_bytes()
        result = tool.database_evidence(self.db_path, "t", now=1800000000)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["due_pending"], 1)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.db_path.read_bytes(), before)
        with tool.read_only_database(self.db_path) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM tasks")

    def test_missing_legacy_locked_and_credential_databases_are_not_created_or_migrated(self):
        missing = self.root / "missing.db"
        self.assertEqual(tool.database_evidence(missing)["code"], "DatabaseMissing")
        self.assertFalse(missing.exists())
        legacy = self.root / "legacy.sqlite"
        with contextlib.closing(sqlite3.connect(legacy)) as db, db:
            db.execute("CREATE TABLE accounts(secret TEXT)")
        before = legacy.read_bytes()
        self.assertEqual(tool.database_evidence(legacy)["code"], "LegacyOrIncompleteSchema")
        self.assertEqual(legacy.read_bytes(), before)
        with contextlib.closing(sqlite3.connect(self.db_path)) as writer, writer:
            writer.execute("BEGIN EXCLUSIVE")
            result = tool.database_evidence(self.db_path)
            self.assertEqual(result["status"], "unverified")
        with patch.object(Path, "open", side_effect=AssertionError("credential file read")):
            self.assertEqual(
                tool.database_evidence(self.root / "login.session")["code"],
                "CredentialDatabaseForbidden",
            )

    def test_live_wal_includes_committed_rows_and_closed_wal_without_sidecars_is_unverified(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as writer, writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO execution_jobs VALUES('unknown','t',0,2000000000,2,0)")
            writer.commit()
            result = tool.database_evidence(self.db_path, "t")
            self.assertEqual(result["counts"]["unknown"], 1)
        self.assertEqual(
            tool.database_evidence(self.db_path)["code"], "WALSidecarsRequiredForSafeRead"
        )

    def test_private_confirmed_queue_unknown_is_only_a_candidate_without_leaking_reference(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO items VALUES('i','t','unknown','attempt',NULL)")
        private = self.root / "receipts.sqlite"
        with contextlib.closing(sqlite3.connect(private)) as db, db:
            db.execute("CREATE TABLE receipts(id TEXT,payload_hash TEXT,result TEXT)")
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?)",
                (
                    "execute:i",
                    "a" * 64,
                    json.dumps(
                        {
                            "status": "confirmed",
                            "verified": True,
                            "external_reference": "https://example.org/" + SECRET,
                            "text": SECRET,
                        }
                    ),
                ),
            )
        before = self.db_path.read_bytes(), private.read_bytes()
        result = tool.receipt_evidence(self.db_path, private, "t", "i")
        self.assertEqual(result["code"], "ReadOnlyReconciliationCandidate")
        self.assertEqual(result["status"], "unverified")
        self.assertTrue(result["private_has_external_reference"])
        self.assertFalse(result["strict_binding_verified"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("https://", json.dumps(result))
        self.assertEqual(before, (self.db_path.read_bytes(), private.read_bytes()))
        self.assertEqual(
            tool.receipt_evidence(self.db_path, private, "wrong-task", "i")["code"],
            "SelectedItemMissing",
        )

    def test_health_blocks_credentials_redirects_and_non_loopback_without_connection(self):
        with patch.object(
            tool.http.client, "HTTPConnection", side_effect=AssertionError("connected")
        ):
            for url in (
                "https://127.0.0.1/health",
                "http://localhost/health",
                "http://127.0.0.1/health?token=x",
                "http://user:pass@127.0.0.1/health",
                "http://example.org/health",
                "http://127.0.0.1/api/start",
            ):
                self.assertEqual(tool.loopback_health(url, {})["status"], "failed")

    def test_actual_loopback_health_whitelist_and_older_running_service(self):
        disk = {"commit": "a" * 40, "source_fingerprint": "b" * 64, "dirty": False}
        body = {
            "runtime": {
                "git_commit_at_start": disk["commit"],
                "source_fingerprint": disk["source_fingerprint"],
                "started_at": time.time(),
            },
            "background": {"sender": {"enabled": True, "alive": True}},
            "token": SECRET,
        }
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                seen.append(handler.path)
                data = json.dumps(body).encode()
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/health"
            good = tool.loopback_health(url, disk)
            self.assertEqual(good["status"], "passed")
            self.assertNotIn(SECRET, json.dumps(good))
            body.pop("background")
            self.assertEqual(tool.loopback_health(url, disk)["status"], "failed")
            self.assertEqual(seen, ["/health", "/health"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_health_redirect_large_response_and_untrusted_fields_are_not_exported(self):
        response = unittest.mock.Mock(status=302)
        conn = unittest.mock.Mock()
        conn.getresponse.return_value = response
        with patch.object(tool.http.client, "HTTPConnection", return_value=conn):
            self.assertEqual(
                tool.loopback_health("http://127.0.0.1/health", {})["code"], "HealthHTTPStatus"
            )
            response.status = 200
            response.read.return_value = b"x" * 65537
            self.assertEqual(
                tool.loopback_health("http://127.0.0.1/health", {})["code"], "HealthTooLarge"
            )

    def test_windows_metadata_filters_raw_identifiers_and_never_requests_command_line(self):
        data = {
            "processes": [
                {
                    "Name": SECRET,
                    "ProcessId": 7,
                    "CommandLine": SECRET,
                    "Started": "2026-10-20T00:00:00Z",
                }
            ],
            "services": [],
            "scheduled_tasks": [],
        }
        with (
            patch.object(tool.sys, "platform", "win32"),
            patch.object(
                tool.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(data).encode()),
            ) as run,
        ):
            result = tool.windows_evidence()
        self.assertEqual(result["status"], "passed")
        self.assertNotIn(SECRET, json.dumps(result))
        command = run.call_args.args[0][-1]
        self.assertNotIn("CommandLine", command)
        self.assertNotIn("Actions", command)
        self.assertIn("-NoProfile", run.call_args.args[0])

    def test_git_avoids_optional_locks_fsmonitor_raw_names_and_stderr(self):
        answers = [
            subprocess.CompletedProcess([], 0, stdout=b"a" * 40),
            subprocess.CompletedProcess([], 0, stdout=("?? " + SECRET).encode()),
        ]
        with (
            patch.object(tool.subprocess, "run", side_effect=answers) as run,
            patch.object(tool, "fingerprint", return_value="b" * 64),
        ):
            result = tool.git_evidence()
        self.assertTrue(result["dirty"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIn("core.fsmonitor=false", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_bounded_observation_distinguishes_future_dependency_unknown_expiry_and_suspected_stall(
        self,
    ):
        now = [100.0]
        sample = {
            "status": "passed",
            "counts": {"pending": 1},
            "future": 0,
            "due_pending": 1,
            "active_leases": 0,
            "global_stop": False,
            "last_queue_update": 1,
            "task": {"starts_at": 90, "ends_at": 200, "phase": "ready", "state": "active"},
        }

        def run():
            now[0] = 100
            return tool.observe(
                self.db_path,
                "t",
                90,
                200,
                3,
                1,
                110,
                reader=lambda *a, **k: sample.copy(),
                clock=lambda: now[0],
                sleep=lambda amount: now.__setitem__(0, now[0] + amount),
            )

        self.assertEqual(run()["code"], "SuspectedStallNeedsHostAndDependencyEvidence")
        sample["future"] = 1
        sample["due_pending"] = 0
        self.assertEqual(run()["code"], "FutureWork")
        sample["future"] = 0
        sample["due_pending"] = 1
        sample["counts"] = {"unknown": 1}
        self.assertEqual(run()["code"], "UnknownReadOnlyReconciliationRequired")
        sample["counts"] = {}
        sample["task"]["phase"] = "review"
        self.assertEqual(run()["code"], "WaitingApprovalOrDependency")
        sample["task"]["phase"] = "ready"
        sample["task"]["ends_at"] = 99
        self.assertEqual(run()["code"], "WindowExpiredNoAutomaticResume")
        sample["status"] = "unverified"
        self.assertEqual(run()["code"], "IncompleteObservation")
        self.assertLessEqual(now[0], 103)
        self.assertEqual(tool.observe(self.db_path, "", 1, 2, 9999, 0, 100)["status"], "failed")

    def test_future_item_never_hides_an_unchanged_due_backlog(self):
        now = [1790000000.0]
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO execution_jobs VALUES('pending','t',1790001000,2000000000,1,0)")
        result = tool.observe(
            self.db_path,
            "t",
            1789999000,
            1790002000,
            1,
            1,
            1790000010,
            clock=lambda: now[0],
            sleep=lambda amount: now.__setitem__(0, now[0] + amount),
        )
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["code"], "SuspectedStallNeedsHostAndDependencyEvidence")
        self.assertEqual(result["final"]["due_pending"], 1)
        self.assertEqual(result["final"]["future"], 1)

    def test_changed_queue_never_overrides_unknown_missing_or_expired_window(self):
        for mode, expected in (
            ("unknown", "UnknownReadOnlyReconciliationRequired"),
            ("missing", "TaskWindowUnverified"),
            ("expired", "WindowExpiredNoAutomaticResume"),
        ):
            with self.subTest(mode=mode):
                now = [100.0]
                first = {
                    "status": "passed",
                    "counts": {"pending": 1},
                    "last_queue_update": 1,
                    "task": {"starts_at": 90, "ends_at": 200, "phase": "ready", "state": "active"},
                }
                second = json.loads(json.dumps(first))
                second["last_queue_update"] = 2
                if mode == "unknown":
                    second["counts"] = {"unknown": 1}
                elif mode == "missing":
                    second["task"]["ends_at"] = None
                else:
                    second["task"]["ends_at"] = 99
                result = tool.observe(
                    self.db_path,
                    "t",
                    90,
                    200,
                    1,
                    1,
                    110,
                    reader=lambda *a, **k: first if now[0] == 100 else second,
                    clock=lambda: now[0],
                    sleep=lambda amount: now.__setitem__(0, now[0] + amount),
                )
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(result["code"], expected)
                self.assertTrue(result["queue_changed"])

    def test_log_tail_only_exports_allowlisted_codes_with_bounded_time(self):
        log = self.root / "synthetic.log"
        log.write_text(
            "2026-10-20T00:00:00Z OperationalError "
            + SECRET
            + "\nunknown time LeaseExpired "
            + SECRET,
            encoding="utf-8",
        )
        result = tool.log_evidence(
            log, tool.parse_time("2026-10-19T00:00:00Z"), tool.parse_time("2026-10-21T00:00:00Z")
        )
        self.assertEqual(result["window_counts"], {"OperationalError": 1})
        self.assertEqual(result["unknown_time_lines"], 1)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_observation_indicator_matrix_never_overrides_uncertain_or_failed_evidence(self):
        for changed in (False, True):
            for future in (0, 1):
                for issue in ("unknown", "missing", "expired", "due", "failed", "missed"):
                    with self.subTest(changed=changed, future=future, issue=issue):
                        now = [100.0]
                        sample = {
                            "status": "passed",
                            "counts": {"pending": 1},
                            "last_queue_update": 1,
                            "future": future,
                            "due_pending": 1,
                            "global_stop": False,
                            "active_leases": 0,
                            "task": {
                                "starts_at": 90,
                                "ends_at": 200,
                                "phase": "ready",
                                "state": "active",
                            },
                        }
                        if issue == "unknown":
                            sample["counts"]["unknown"] = 1
                        if issue == "missing":
                            sample["task"]["ends_at"] = None
                        if issue == "expired":
                            sample["task"]["ends_at"] = 99
                        if issue in {"failed", "missed"}:
                            sample["counts"][issue] = 1

                        def read(*args, **kwargs):
                            value = json.loads(json.dumps(sample))
                            if changed and now[0] > 100:
                                value["last_queue_update"] = 2
                            return value

                        result = tool.observe(
                            self.db_path,
                            "t",
                            90,
                            200,
                            1,
                            1,
                            110,
                            reader=read,
                            clock=lambda: now[0],
                            sleep=lambda amount: now.__setitem__(0, now[0] + amount),
                        )
                        self.assertEqual(result["status"], "unverified")
                        self.assertNotEqual(result["code"], "FutureWork")
                        self.assertEqual(result["queue_changed"], changed)

    def test_output_utf8_never_overwrites_inputs_or_existing_report(self):
        output = self.root / "报告.json"
        with patch.object(tool, "faults", return_value=tool.check("passed", "Synthetic")):
            self.assertEqual(tool.main(["faults", "--output", str(output)]), 0)
            self.assertFalse(output.read_bytes().startswith(b"\xff\xfe"))
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["report_schema"], 1)
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tool.main(["faults", "--output", str(output)]), 1)
                self.assertEqual(tool.main(["faults", "--output", str(self.root / ".env.json")]), 1)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)

    def test_fault_launcher_copies_reviewed_tests_and_does_not_inherit_credentials(self):
        created = []

        def run(argv, *, cwd, env, **kwargs):
            created.append(Path(cwd))
            self.assertEqual(env["HOME"], env["USERPROFILE"])
            for key in ("AP_DB", "AP_ACCOUNTS_DIR", "APPDATA", "LOCALAPPDATA"):
                self.assertTrue(Path(env[key]).is_relative_to(cwd))
            self.assertNotIn("TELEGRAM_API_HASH", env)
            self.assertNotIn("CLOUDFLARE_API_TOKEN", env)
            self.assertEqual(env["AUTO_PROMOTION_LIVE_WRITES"], "0")
            self.assertTrue((Path(cwd) / "src" / "sitecustomize.py").is_file())
            self.assertFalse((Path(cwd) / ".env").exists())
            return subprocess.CompletedProcess(
                argv, 0, stdout=b'{"tests":39,"failed":0,"errors":0,"skipped":0}'
            )

        with (
            patch.dict(os.environ, {"TELEGRAM_API_HASH": SECRET, "CLOUDFLARE_API_TOKEN": SECRET}),
            patch.object(tool.subprocess, "run", side_effect=run),
        ):
            result = tool.faults()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(all(not path.exists() for path in created))
        with patch.object(tool.importlib.util, "find_spec", return_value=None):
            self.assertEqual(tool.faults()["code"], "SyntheticDependenciesMissing")

    @unittest.skipUnless(sys.platform == "win32", "actual Windows PowerShell/CIM required")
    def test_actual_windows_powershell_metadata_is_readable(self):
        result = tool.windows_evidence()
        self.assertEqual(result["status"], "passed", result)


if __name__ == "__main__":
    unittest.main()
