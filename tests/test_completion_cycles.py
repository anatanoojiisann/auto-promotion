import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.completion_cycles import CompletionCycles
from auto_promotion.completion_sources import external_evidence, refresh_sources
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import AccountInput, ItemInput, TaskInput
from auto_promotion.store import Store


class CompletionCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1800000000.0
        self.store = Store(Path(self.temp.name) / "cycle.db")
        self.cycles = CompletionCycles(self.store, clock=lambda: self.now)
        self.cycles.configure(
            enabled=True, source="human: all accounts; P0/P1 automatic, P2-P4 on explicit choice"
        )
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
        for account in ("a", "b"):
            self.cycles.enroll(account, "x", account)
            self.cycles.observe(
                account,
                "original",
                "complete",
                evidence="verified task closure",
                completed_at=self.now,
            )

    def refresh(self, account="a", state="complete"):
        old = self.store.one(
            "SELECT completed_at FROM completion_cycle_workflows WHERE account_id=? AND reference='original'",
            (account,),
        )
        self.cycles.observe(
            account,
            "original",
            state,
            evidence="fresh ledger",
            completed_at=(old["completed_at"] or self.now) if state == "complete" else None,
        )

    def ready_round(self, candidates):
        self.now += 7200
        self.refresh()
        run = self.cycles.claim_scan("a", "worker")
        self.cycles.finish_scan(
            run, "worker", candidates, evidence="current public sources", coverage="partial"
        )
        return run

    def candidate(self, priority="P1", event="123"):
        return {
            "event_id": event,
            "priority": priority,
            "target": "https://x.com/person/status/" + event,
            "source_quote": "How do I keep my video character consistent?",
            "reason": "Unresolved workflow request",
            "draft": "Use the same reference image for each shot; what changes between your shots?",
            "context_verified": True,
            "unresolved": priority in {"P0", "P1"},
            "direct_relation_evidence": "actual mention of this account"
            if priority == "P0"
            else "",
            "evidence": ["current source and reply context"],
            "expires_at": self.now + 86400,
        }

    def row(self, event="123"):
        return self.store.one(
            "SELECT * FROM completion_cycle_candidates WHERE event_id=?", (event,)
        )

    def confirm(self, c):
        self.cycles.begin(c["id"], c["payload_hash"])
        self.cycles.result(
            c["id"],
            "confirmed",
            evidence="independent receipt lookup",
            external_reference="https://x.com/me/status/900",
            verified=True,
            published_at=self.now,
        )

    def test_accounts_wait_independently_from_actual_completion(self):
        self.cycles.observe("b", "original", "busy", evidence="unfinished interactions")
        self.now += 7200
        self.refresh("a")
        self.refresh("b", "busy")
        accounts = {a["id"]: a for a in self.cycles.status()["accounts"]}
        self.assertTrue(accounts["a"]["due"])
        self.assertFalse(accounts["b"]["due"])
        self.assertIsNone(accounts["b"]["next_scan_at"])

    def test_restart_and_repeated_status_do_not_retime(self):
        original = self.cycles.status()["accounts"][0]["next_scan_at"]
        self.now += 60
        self.cycles = CompletionCycles(self.store, clock=lambda: self.now)
        self.assertEqual(self.cycles.status()["accounts"][0]["next_scan_at"], original)

    def test_early_or_stale_scan_is_rejected(self):
        with self.assertRaises(Conflict):
            self.cycles.claim_scan("a", "worker")
        self.now += 7200
        with self.assertRaisesRegex(Conflict, "fresh_evidence"):
            self.cycles.claim_scan("a", "worker")

    def test_two_consumers_cannot_start_two_scans(self):
        self.now += 7200
        self.refresh()

        def claim(owner):
            try:
                return self.cycles.claim_scan("a", owner)
            except Conflict:
                return None

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(claim, ("one", "two")))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_expired_readonly_scan_reclaims_same_round(self):
        self.now += 7200
        self.refresh()
        run = self.cycles.claim_scan("a", "one")
        self.now += 1801
        self.refresh()
        self.assertEqual(self.cycles.claim_scan("a", "two"), run)
        with self.assertRaisesRegex(Conflict, "lease_lost"):
            self.cycles.finish_scan(run, "one", [], evidence="late")

    def test_low_priorities_are_not_notified_or_authorized_until_high_finishes(self):
        run = self.ready_round(
            [
                self.candidate("P0"),
                self.candidate("P2", "124"),
                self.candidate("P3", "125"),
                self.candidate("P4", "126"),
            ]
        )
        low = self.row("124")
        self.assertEqual(self.cycles.status()["pending_notices"], [])
        with self.assertRaisesRegex(Conflict, "high_priority"):
            self.cycles.authorize(
                {low["id"]: low["payload_hash"]}, source="human chose this exact candidate"
            )
        self.confirm(self.row())
        notices = self.cycles.status()["pending_notices"]
        self.assertEqual(notices[0]["round_id"], run)
        self.assertEqual([c["priority"] for c in notices[0]["snapshot"]], ["P2", "P3", "P4"])
        with self.assertRaises(Conflict):
            self.cycles.begin(low["id"], low["payload_hash"])

    def test_authorized_low_resets_clock_after_its_completion(self):
        self.ready_round([self.candidate("P4")])
        c = self.row()
        self.now += 120
        self.refresh()
        self.cycles.authorize({c["id"]: c["payload_hash"]}, source="human: interact with this P4")
        self.confirm(c)
        a = next(a for a in self.cycles.status()["accounts"] if a["id"] == "a")
        self.assertEqual(a["next_scan_at"], self.now + 7200)

    def test_unknown_keeps_account_blocked_and_never_replays(self):
        self.ready_round([self.candidate(), self.candidate("P2", "124")])
        c = self.row()
        self.cycles.begin(c["id"], c["payload_hash"])
        self.cycles.result(c["id"], "unknown", evidence="submission response lost")
        self.now += 10000
        self.refresh()
        state = self.cycles.status()
        self.assertFalse(next(a for a in state["accounts"] if a["id"] == "a")["due"])
        self.assertEqual(state["pending_notices"], [])
        with self.assertRaises(Conflict):
            self.cycles.begin(c["id"], c["payload_hash"])
        with self.assertRaises(Conflict):
            self.cycles.result(c["id"], "skipped", evidence="not visible in a search")
        with self.assertRaises(Conflict):
            self.cycles.result(c["id"], "failed_known", evidence="not visible in a search")
        self.cycles.result(
            c["id"],
            "confirmed",
            evidence="actual read-only receipt",
            external_reference="https://x.com/me/status/900",
            verified=True,
            published_at=self.now - 10000,
        )
        self.assertEqual(len(self.cycles.status()["pending_notices"]), 1)

    def test_missing_receipt_cannot_be_counted_as_confirmed(self):
        self.ready_round([self.candidate()])
        c = self.row()
        self.cycles.begin(c["id"], c["payload_hash"])
        with self.assertRaises(Conflict):
            self.cycles.result(c["id"], "confirmed", evidence="button clicked", verified=True)

    def test_global_stop_still_prevents_external_handoff(self):
        self.ready_round([self.candidate()])
        c = self.row()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        with self.assertRaisesRegex(Conflict, "global_stop"):
            self.cycles.begin(c["id"], c["payload_hash"])

    def test_candidate_hash_change_and_cross_account_duplicate_rejected(self):
        self.ready_round([self.candidate("P2")])
        c = self.row()
        with self.assertRaises(Conflict):
            self.cycles.authorize({c["id"]: "modified"}, source="human")
        self.refresh("b")
        run = self.cycles.claim_scan("b", "other")
        self.cycles.finish_scan(run, "other", [self.candidate("P1")], evidence="same original post")
        self.assertEqual(len(self.store.rows("SELECT * FROM completion_cycle_candidates")), 1)

    def test_p0_requires_direct_relation_and_platform_target_is_bound(self):
        self.now += 7200
        self.refresh()
        run = self.cycles.claim_scan("a", "worker")
        c = self.candidate("P0")
        c["direct_relation_evidence"] = ""
        with self.assertRaises(Conflict):
            self.cycles.finish_scan(run, "worker", [c], evidence="source")
        c["target"] = "https://example.com/post"
        with self.assertRaises(Conflict):
            self.cycles.finish_scan(run, "worker", [c], evidence="source")

    def test_new_busy_work_prevents_prepared_reply_submission(self):
        self.ready_round([self.candidate()])
        self.refresh(state="busy")
        c = self.row()
        with self.assertRaisesRegex(Conflict, "fresh_evidence"):
            self.cycles.begin(c["id"], c["payload_hash"])

    def test_low_pending_authorization_does_not_deadlock_next_scan(self):
        self.ready_round([self.candidate("P4")])
        self.now += 7200
        self.refresh()
        self.assertTrue(next(a for a in self.cycles.status()["accounts"] if a["id"] == "a")["due"])
        self.cycles.status()
        self.assertEqual(len(self.cycles.status()["pending_notices"]), 1)

    def test_external_stopped_batch_retains_unknown_and_followup_observation(self):
        state, _ = external_evidence(
            {"status": "stopped_by_user", "items": [{"account": "Loo", "status": "unknown"}]},
            account_label="Loo",
            now=self.now,
        )
        self.assertEqual(state, "unknown")
        state, _ = external_evidence(
            {
                "status": "observing_replies",
                "items": [{"status": "sent", "followup_status": "awaiting_response"}],
            },
            account_label="IG",
            now=self.now,
        )
        self.assertEqual(state, "busy")
        state, completed = external_evidence(
            {
                "status": "stopped_by_user",
                "items": [
                    {"account": "Kore", "status": "sent"},
                    {"account": "Loo", "status": "unknown"},
                ],
            },
            account_label="Kore",
            now=self.now,
        )
        self.assertEqual((state, completed), ("stopped", self.now))

    def test_native_refresh_enrolls_real_accounts_only(self):
        engine = Engine(self.store, AdapterRegistry(Path(self.temp.name)))
        real = engine.add_account(
            AccountInput(platform="instagram", label="IG", backend_label="real", mode="live")
        )
        engine.add_account(
            AccountInput(
                platform="x", label="simulation", backend_label="fixture", mode="simulation"
            )
        )
        state = refresh_sources(self.cycles)
        self.assertIn(real["id"], [a["id"] for a in state["accounts"]])
        self.assertEqual(len(state["accounts"]), 3)

    def test_expiry_closes_only_unsubmitted_candidates(self):
        self.ready_round([self.candidate(), self.candidate("P4", "124")])
        c = self.row()
        self.cycles.begin(c["id"], c["payload_hash"])
        self.now += 90000
        self.cycles.status()
        self.assertEqual(self.row()["state"], "submitting")
        self.assertEqual(self.row("124")["state"], "skipped")

    def test_native_cooldown_prevents_scan_claim(self):
        engine = Engine(self.store, AdapterRegistry(Path(self.temp.name)))
        a = engine.add_account(
            AccountInput(platform="x", label="real", backend_label="fixture", mode="live")
        )
        self.cycles.enroll(a["id"], "x", "real", details={"native_account_id": a["id"]})
        self.cycles.observe(
            a["id"], "ledger", "complete", evidence="known completed", completed_at=self.now
        )
        self.now += 7200
        self.cycles.observe(
            a["id"], "ledger", "complete", evidence="fresh", completed_at=self.now - 7200
        )
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET cooldown_until=? WHERE id=?", (self.now + 600, a["id"]))
        with self.assertRaisesRegex(Conflict, "cooldown"):
            self.cycles.claim_scan(a["id"], "worker")

    def test_external_account_without_completion_evidence_never_scans(self):
        self.cycles.enroll("external:ig", "instagram", "real IG")
        self.now += 90000
        self.assertFalse(
            next(a for a in self.cycles.status()["accounts"] if a["id"] == "external:ig")["due"]
        )
        with self.assertRaisesRegex(Conflict, "fresh_evidence"):
            self.cycles.claim_scan("external:ig", "worker")

    def test_native_handoff_requires_same_exact_unattempted_task(self):
        from datetime import datetime, timezone

        engine = Engine(self.store, AdapterRegistry(Path(self.temp.name)), clock=lambda: self.now)
        a = engine.add_account(
            AccountInput(
                platform="x",
                label="synthetic native",
                backend_label="native",
                mode="simulation",
                min_interval=0,
            )
        )
        self.cycles.enroll(a["id"], "x", "native", details={"native_account_id": a["id"]})
        self.cycles.observe(a["id"], "ledger", "complete", evidence="closed", completed_at=self.now)
        self.now += 7200
        self.cycles.observe(
            a["id"], "ledger", "complete", evidence="fresh", completed_at=self.now - 7200
        )
        run = self.cycles.claim_scan(a["id"], "worker")
        value = self.candidate()
        self.cycles.finish_scan(run, "worker", [value], evidence="public source")
        c = self.row()
        with self.assertRaisesRegex(Conflict, "exact_native_task"):
            self.cycles.begin(c["id"], c["payload_hash"])

        def iso(at):
            return datetime.fromtimestamp(at, timezone.utc).isoformat()

        task = engine.create_task(
            TaskInput(
                name="completion native fixture",
                account_ids=[a["id"]],
                policy={
                    "starts_at": iso(self.now - 1),
                    "ends_at": iso(self.now + 3600),
                    "min_interval": 0,
                },
            )
        )
        engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=a["id"],
                    action="reply",
                    target=value["target"],
                    text="different unreviewed text",
                    scheduled_at=iso(self.now + 30),
                )
            ],
        )
        with self.assertRaisesRegex(Conflict, "manifest_mismatch"):
            self.cycles.begin(c["id"], c["payload_hash"], task_id=task["id"])
        engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=a["id"],
                    action="reply",
                    target=value["target"],
                    text=value["draft"],
                    scheduled_at=iso(self.now + 30),
                )
            ],
        )
        self.cycles.begin(c["id"], c["payload_hash"], task_id=task["id"])
        self.assertEqual(self.row()["state"], "submitting")


if __name__ == "__main__":
    unittest.main()
