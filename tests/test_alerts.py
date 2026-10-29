import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from auto_promotion.alert_relay import ENDPOINT, AlertRelay, AlertSettings
from auto_promotion.alert_rules import AlertRules
from auto_promotion.alert_service import AlertService
from auto_promotion.alert_sources import AlertSources, read_database
from auto_promotion.alert_store import AlertStore
from auto_promotion.models import canonical
from auto_promotion.statistics import Statistics
from auto_promotion.store import Store


class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.business = Store(self.root / "business.db")
        self.now = datetime.fromisoformat("2026-10-25T12:00:00+08:00").timestamp()
        self.settings = AlertSettings(key="fake-private-key", enabled=True, progress_seconds=60)
        self.relay = Mock()
        self.relay.send.return_value = {
            "state": "accepted",
            "reason": "delivery_unverified",
            "http": 200,
        }
        self.service = self.new_service()
        self.account()

    def new_service(self):
        service = AlertService(
            self.business.path,
            self.root / "alerts.db",
            settings=self.settings,
            relay=self.relay,
            clock=lambda: self.now,
        )
        # Isolated regression tests for the previous detailed rule engine.
        # Production AlertService uses only ThreeHourSummaryRules.
        service.sources = AlertSources(self.business.path, clock=lambda: self.now)
        service.rules = AlertRules(service.store, progress_seconds=self.settings.progress_seconds)
        return service

    def account(self, key="a", platform="telegram", mode="live"):
        with self.business.connect(write=True) as db:
            db.execute(
                "INSERT INTO accounts(id,platform,label,backend_label,mode,status,health,checked_at,daily_limit,hourly_limit,min_interval) VALUES(?,?,?,?,?,'healthy',?,?,5,3,480)",
                (
                    key,
                    platform,
                    "账号 " + key,
                    key,
                    mode,
                    canonical(
                        {
                            "healthy": True,
                            "identity_verified": True,
                            "reason": "account_read_verified",
                        }
                    ),
                    self.now,
                ),
            )

    def task(self, statuses=("not_attempted",), *, key="t", mode="live", account="a"):
        start = datetime.fromtimestamp(self.now - 3600).astimezone().isoformat()
        end = datetime.fromtimestamp(self.now + 3600).astimezone().isoformat()
        with self.business.connect(write=True) as db:
            db.execute(
                "INSERT INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,state,phase,manifest_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,'hash','active','ready','manifest',?,?)",
                (
                    key,
                    "任务 " + key,
                    mode,
                    canonical([account]),
                    "[]",
                    canonical({"starts_at": start, "ends_at": end}),
                    self.now,
                    self.now,
                ),
            )
            for index, status in enumerate(statuses):
                db.execute(
                    "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,external_reference,result) VALUES(?,?,?,?,?,'hash',?,?,?,?)",
                    (
                        f"{key}-{index}",
                        key,
                        account,
                        index,
                        canonical({"action": "reply", "text": "private outbound text"}),
                        self.now + 480,
                        status,
                        "https://example.com/receipt" if status == "confirmed" else None,
                        canonical({"verified": status == "confirmed"}),
                    ),
                )

    def health(self, reason, *, healthy=False, checked=None, **extra):
        with self.business.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET health=?,checked_at=? WHERE id='a'",
                (
                    canonical(
                        {
                            "healthy": healthy,
                            "identity_verified": healthy,
                            "reason": reason,
                            **extra,
                        }
                    ),
                    self.now if checked is None else checked,
                ),
            )

    def tick(self, seconds=1):
        self.now += seconds
        self.assertTrue(self.service.collect())

    def messages(self, kind=None):
        with self.service.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM alert_outbox"
                    + (" WHERE kind=?" if kind else "")
                    + " ORDER BY created_at",
                    (kind,) if kind else (),
                )
            ]

    def test_source_is_read_only_and_excludes_simulation_and_unverified_success(self):
        self.task(("confirmed", "scheduled", "unknown", "manual_attention", "confirmed"))
        self.account("sim", mode="simulation")
        self.task(("confirmed",), key="sim", account="sim", mode="simulation")
        with self.business.connect(write=True) as db:
            db.execute("UPDATE items SET result='{}' WHERE id='t-4'")
        sample = self.service.sources.collect()
        self.assertEqual(len(sample["accounts"]), 1)
        self.assertEqual(
            sample["tasks"][0]["counts"],
            {"confirmed": 1, "scheduled": 1, "unknown": 1, "manual_attention": 1, "unverified": 1},
        )
        self.assertNotIn("private outbound text", canonical(sample))
        with read_database(self.business.path) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
        with self.business.connect() as db:
            self.assertEqual(
                db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0], "1"
            )

    def test_baseline_does_not_replay_task_history(self):
        self.task(("confirmed", "unknown"))
        self.tick()
        self.assertEqual([r["kind"] for r in self.messages()], ["startup"])
        self.tick(1801)
        self.assertEqual(self.messages("task"), [])

    def test_critical_risk_overrides_disabled_and_cooldown_then_requires_fresh_recovery(self):
        self.tick()
        self.health("PeerFloodError", risk_warning=True)
        with self.business.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET disabled=1,cooldown_until=? WHERE id='a'", (self.now + 3600,)
            )
        self.tick()
        text = self.messages("account")[0]["text"]
        self.assertIn("严重", text)
        self.tick(100)
        self.assertEqual(len(self.messages("account")), 1)
        with self.business.connect(write=True) as db:
            db.execute("UPDATE accounts SET disabled=0,cooldown_until=0 WHERE id='a'")
        self.health("account_read_verified", healthy=True, checked=self.now - 1000)
        self.tick()
        self.assertEqual(self.messages("recovery"), [])
        self.health("account_read_verified", healthy=True)
        self.tick()
        self.assertEqual(len(self.messages("recovery")), 1)
        self.tick()
        self.assertEqual(len(self.messages("recovery")), 1)

    def test_network_debounce_and_restart_preserve_incidents(self):
        self.tick()
        self.health("TimeoutError", consecutive_failures=3)
        self.tick()
        self.tick(119)
        self.assertEqual(self.messages("account"), [])
        self.tick(1)
        self.assertEqual(len(self.messages("account")), 1)
        self.service = self.new_service()
        self.tick()
        self.assertEqual(len(self.messages("account")), 1)

    def test_account_platform_error_mapping(self):
        for platform, reason, expected in (
            ("discord", "DiscordInvalidToken", "authorization_required"),
            ("tiktok", "TikTokPermissionDenied", "permission_denied"),
            ("instagram", "InstagramRequestRejected", "request_rejected"),
            ("x", "BrowserVerificationRequired", "verification_required"),
            ("reddit", "BrowserAccountRestricted", "restricted"),
        ):
            with self.subTest(platform=platform):
                with self.business.connect(write=True) as db:
                    db.execute("UPDATE accounts SET platform=? WHERE id='a'", (platform,))
                self.health(reason)
                self.assertEqual(self.service.sources.collect()["accounts"][0]["issue"], expected)

    def test_progress_is_deferred_not_lost_and_unknown_is_not_success(self):
        self.task(("not_attempted", "unknown", "scheduled"))
        self.tick()
        with self.business.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='confirmed',result=?,external_reference='https://example.com/1' WHERE id='t-0'",
                (canonical({"verified": True}),),
            )
        self.tick()
        self.assertEqual(self.messages("task"), [])
        self.tick(60)
        text = self.messages("task")[0]["text"]
        self.assertIn("33%", text)
        self.assertIn("结果未知 1", text)
        self.assertIn("已排程待核验 1", text)
        self.tick(60)
        self.assertEqual(len(self.messages("task")), 1)

    def test_new_start_and_completion_are_broadcast_once(self):
        self.tick()
        self.task(("not_attempted",))
        self.tick()
        self.assertIn("任务已启动", self.messages("task")[0]["text"])
        with self.business.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='confirmed',result=?,external_reference='https://example.com/1'",
                (canonical({"verified": True}),),
            )
        self.tick()
        self.assertIn("全部核验成功", self.messages("task")[-1]["text"])
        self.tick()
        self.assertEqual(len(self.messages("task")), 2)

    def test_expired_tasks_do_not_create_stale_account_warnings(self):
        self.task(("unknown", "missed"))
        with self.business.connect(write=True) as db:
            policy = {"ends_at": datetime.fromtimestamp(self.now - 1).astimezone().isoformat()}
            db.execute("UPDATE tasks SET policy=?", (canonical(policy),))
        self.now += 1000
        self.assertEqual(self.service.sources.collect()["accounts"][0]["issue"], "healthy")

    def test_report_boundary_and_late_correction(self):
        self.task(("confirmed",))
        stats = Statistics(self.business, clock=lambda: self.now)
        stats.run_due()
        self.tick()
        self.now += 3 * 3600
        stats.run_due()
        self.tick()
        report = self.messages("report")
        self.assertEqual(len(report), 1)
        self.assertIn("覆盖不完整", report[0]["text"])
        self.assertNotIn("private outbound text", report[0]["text"])

    def test_single_claim_under_concurrency_and_crash_is_unknown(self):
        with self.service.store.connect(write=True) as db:
            self.service.store.enqueue(db, "one-event", "test", "safe text")

        def claim(_):
            return AlertStore(self.root / "alerts.db", clock=lambda: self.now).claim()

        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(claim, range(4)))
        self.assertEqual(sum(c is not None for c in claims), 1)
        self.now += 91
        self.assertIsNone(self.service.store.claim())
        self.assertEqual(self.messages()[0]["state"], "unknown")

    def test_timeout_does_not_resubmit_or_create_reminders(self):
        self.tick()
        self.health("PeerFloodError", risk_warning=True)
        self.tick()
        self.relay.send.return_value = {"state": "unknown", "reason": "relay_outcome_unknown"}
        self.service.drain()
        self.tick(3601)
        self.service.drain()
        self.assertEqual(len(self.messages("account")), 1)
        self.assertEqual(self.relay.send.call_count, 2)  # startup and account, once each

    def test_auth_failure_pauses_queue_and_does_not_expose_key(self):
        self.tick()
        self.relay.send.return_value = {
            "state": "failed",
            "reason": "relay_authorization_failed",
            "http": 401,
        }
        self.service.drain()
        self.assertTrue(self.service.status()["authorization_blocked"])
        self.assertNotIn(self.settings.key, repr(self.settings))
        self.assertNotIn(self.settings.key, canonical(self.service.status()))
        self.tick()
        self.service.drain()
        self.assertEqual(self.relay.send.call_count, 1)

    def test_collection_failure_preserves_baseline(self):
        self.tick()
        last = self.service.status()["last_observed_at"]
        self.service.sources.collect = Mock(side_effect=OSError("private token"))
        self.assertFalse(self.service.collect())
        self.assertEqual(self.service.status()["last_observed_at"], last)
        self.assertEqual(self.service.status()["last_collection_error"], "source_collection_failed")

    def test_rate_limit_retry_is_bounded_and_preserves_notice_id(self):
        self.tick()
        self.relay.send.return_value = {
            "state": "retry",
            "reason": "relay_rate_limited",
            "retry_after": 30,
        }
        for delay in (0, 30, 120, 600):
            self.now += delay
            self.service.drain()
        rows = self.messages()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "failed")
        self.assertEqual(rows[0]["attempts"], 4)
        self.service.drain()
        self.assertEqual(self.relay.send.call_count, 4)

    def test_recovered_issue_expires_unsent_old_alarm(self):
        self.tick()
        self.health("PeerFloodError", risk_warning=True)
        self.tick()
        self.health("account_read_verified", healthy=True)
        self.tick()
        self.assertEqual(self.messages("account")[0]["state"], "expired")
        self.assertEqual(self.messages("recovery")[0]["state"], "pending")

    def test_accepted_notice_is_not_sent_again(self):
        self.tick()
        self.service.drain()
        self.service.drain()
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(self.messages()[0]["state"], "accepted")

    def test_midnight_report_waits_five_minutes_and_drops_duplicate_interval(self):
        self.now = datetime.fromisoformat("2026-10-25T23:59:00+08:00").timestamp()
        stats = Statistics(self.business, clock=lambda: self.now)
        stats.run_due()
        self.tick()
        self.now += 60
        stats.run_due()
        self.tick()
        reports = self.messages("report")
        self.assertEqual(len(reports), 1)
        self.assertIn("全天日报", reports[0]["text"])
        self.assertGreater(reports[0]["available_at"], self.now)
        self.service.drain()
        self.assertEqual(self.messages("report")[0]["state"], "pending")
        self.now += 300
        self.service.drain()
        self.assertEqual(self.messages("report")[0]["state"], "accepted")

    def test_late_report_correction_is_coalesced_and_old_pending_copy_expires(self):
        stats = Statistics(self.business, clock=lambda: self.now)
        stats.run_due()
        self.tick()
        self.now += 3 * 3600
        stats.run_due()
        self.tick()
        self.assertEqual(len(self.messages("report")), 1)
        with self.business.connect(write=True) as db:
            row = db.execute(
                "SELECT kind,cutoff,content FROM statistical_reports WHERE kind='interval' ORDER BY cutoff DESC LIMIT 1"
            ).fetchone()
            content = json.loads(row["content"])
            content["period"]["totals"]["observed_replies"] = 2
            db.execute(
                "UPDATE statistical_reports SET content=?,revision=revision+1 WHERE kind=? AND cutoff=?",
                (canonical(content), row["kind"], row["cutoff"]),
            )
        self.tick(30)
        self.assertEqual(len(self.messages("report")), 1)
        self.tick(1800)
        self.assertEqual(len(self.messages("report")), 2)
        self.assertEqual(self.messages("report")[0]["state"], "expired")
        self.assertIn("数据更正", self.messages("report")[1]["text"])

    def test_independent_accounts_use_earliest_eligible_account(self):
        self.account("b")
        self.task()
        with self.business.connect(write=True) as db:
            policy = json.loads(db.execute("SELECT policy FROM tasks WHERE id='t'").fetchone()[0])
            policy["pacing_scope"] = "account"
            db.execute(
                "UPDATE tasks SET account_ids=?,policy=?",
                (canonical(["a", "b"]), canonical(policy)),
            )
            db.execute("UPDATE accounts SET next_at=? WHERE id='a'", (self.now + 3600,))
            db.execute("UPDATE items SET account_id='b'")
        self.assertEqual(self.service.sources.collect()["tasks"][0]["next_at"], self.now + 480)

    def test_short_task_completed_between_polls_reports_final_result(self):
        self.tick()
        self.task(("confirmed",))
        self.tick()
        self.assertIn("全部核验成功", self.messages("task")[0]["text"])
        self.assertNotIn("任务已启动", self.messages("task")[0]["text"])

    def test_status_reports_active_service_settings_instead_of_observer_defaults(self):
        self.service.heartbeat()
        observer = AlertService(
            self.business.path,
            self.root / "alerts.db",
            settings=AlertSettings(),
            clock=lambda: self.now,
        )
        self.assertTrue(observer.status()["enabled"])
        self.assertTrue(observer.status()["key_configured"])


class RelayTests(unittest.TestCase):
    def relay(self, *, response=None, error=None):
        opener = Mock()
        if error:
            opener.open.side_effect = error
        else:
            handle = Mock(status=202)
            handle.read.return_value = json.dumps(response or {"ok": True}).encode()
            handle.__enter__ = Mock(return_value=handle)
            handle.__exit__ = Mock(return_value=False)
            opener.open.return_value = handle
        return AlertRelay(AlertSettings(key="private-key", enabled=True), opener=opener), opener

    def test_exact_source_text_contract_and_accepted_is_not_delivery(self):
        relay, opener = self.relay()
        result = relay.send("中文预警")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(json.loads(request.data), {"source": "Auto Promotion", "text": "中文预警"})
        self.assertEqual(
            result, {"state": "accepted", "reason": "delivery_unverified", "http": 202}
        )

    def test_unknown_vs_definite_rejection(self):
        for code, state in (
            (401, "failed"),
            (403, "failed"),
            (429, "retry"),
            (500, "unknown"),
            (408, "unknown"),
            (302, "failed"),
        ):
            with self.subTest(code=code):
                exc = HTTPError(ENDPOINT, code, "secret details", {}, io.BytesIO(b"private body"))
                relay, _ = self.relay(error=exc)
                result = relay.send("safe")
                self.assertEqual(result["state"], state)
                self.assertNotIn("secret", canonical(result))
        relay, _ = self.relay(error=TimeoutError("private details"))
        self.assertEqual(relay.send("safe")["state"], "unknown")

    def test_business_failure_and_secret_redaction(self):
        relay, opener = self.relay(response={"success": False, "error": "private response"})
        self.assertEqual(relay.send("private-key")["state"], "failed")
        self.assertNotIn("private-key", opener.open.call_args.args[0].data.decode())

    def test_private_file_only_supplies_named_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "ALERT_RELAY_KEY='example-key'\nAUTO_PROMOTION_LIVE_WRITES=1\n", encoding="utf-8"
            )
            import os
            from unittest.mock import patch

            with patch.dict(os.environ, {}, clear=True):
                settings = AlertSettings.environment(path)
                self.assertEqual(settings.key, "example-key")
                self.assertFalse(settings.enabled)
                self.assertNotIn("AUTO_PROMOTION_LIVE_WRITES", os.environ)


if __name__ == "__main__":
    unittest.main()
