"""Synthetic readiness, imported coverage and durable enqueue pacing contracts."""

import unittest
from unittest.mock import patch

import test_interactions as fixtures

from auto_promotion.core import Conflict
from auto_promotion.interaction_models import SourceImport
from auto_promotion.interaction_simulation import drain_sender, source
from auto_promotion.interactions import Interactions


class InteractionReadinessTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.InteractionTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_live_enable_reports_missing_model_and_worker_but_drafts_can_save(self):
        f = self.f
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live'")
        f.io.capabilities = lambda account: {"transport": "x_api", "model": {"configured": False}}
        f.s.worker_available = lambda: False
        f.config(enabled=False)
        report = f.s.status(f.p["id"])["readiness"]
        self.assertIn("model_connection_unavailable", report["blockers"])
        self.assertIn("worker_unavailable", report["blockers"])
        with self.assertRaisesRegex(Conflict, "model_connection_unavailable"):
            f.config(enabled=True)
        self.assertFalse(f.s.policy(f.p["id"])["config"]["enabled"])

    def test_browser_source_is_not_misrepresented_as_api_and_import_is_partial(self):
        f = self.f
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live'")
        f.io.capabilities = lambda account: {
            "transport": "browser_or_unconfigured",
            "model": {"configured": True},
        }
        f.s.worker_available = lambda: True
        f.config(enabled=False)
        with self.assertRaisesRegex(Conflict, "scan_source_unavailable"):
            f.config(enabled=True)
        f.config(enabled=True, scan={"source": "imported_results"})
        readiness = f.s.status(f.p["id"])["readiness"]
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["coverage"], "partial_manual_import")
        self.assertEqual(readiness["publishing"], "requires_target_eligibility")

    def test_import_routes_candidates_but_never_earns_empty_rounds_or_watermarks(self):
        f = self.f
        f.config(scan={"source": "imported_results"})
        event = source(7001, "P1", f.now)
        public = {k: v for k, v in event.items() if not k.startswith("simulation_")}
        f.io.classify = lambda *args: event["simulation_classification"]
        with patch.object(f.io, "discover", side_effect=AssertionError("no browser/API scan")):
            result = f.s.import_sources(f.p["id"], SourceImport(sources=[public]))
            f.s.run_stage("classify")
            f.s.settle_rounds()
            self.assertEqual(f.candidate(7001)["priority"], "P1")
            self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 0)
            self.assertEqual(f.store.rows("SELECT * FROM hf_watermarks"), [])
            run = f.store.one("SELECT * FROM hf_scan_runs WHERE id=?", (result["run_id"],))
            self.assertEqual(run["coverage_incomplete"], 1)
            f.s.schedule()
            self.assertEqual(len(f.store.rows("SELECT * FROM hf_scan_runs")), 1)

    def test_import_rejects_missing_original_timestamp_and_simulation_override(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            SourceImport(sources=[{"id": "7", "url": "https://x.com/a/status/7", "text": "x"}])
        event = source(7002, "P1", self.f.now)
        with self.assertRaises(ValidationError):
            SourceImport(sources=[event])

    def test_delay_is_frozen_once_survives_restart_and_repeat_enqueue(self):
        f = self.f
        f.config(publishing={"enqueue_delay_min_seconds": 600, "enqueue_delay_max_seconds": 900})
        f.round([source(7003, "P1", f.now)])
        f.s.run_stage("generate")
        with patch.object(f.s, "draw_delay", return_value=721) as draw:
            f.s.run_stage("publish")
            self.assertEqual(draw.call_count, 1)
        row = f.candidate(7003)
        self.assertEqual(row["not_before"], f.now + 721)
        item = f.store.one("SELECT * FROM items WHERE id=?", (row["item_id"],))
        self.assertEqual(item["due_at"], row["not_before"])
        drain_sender(f.engine)
        self.assertEqual(len(f.store.rows("SELECT * FROM attempts")), 0)
        restarted = Interactions(f.engine, connectors=f.io)
        with patch.object(restarted, "draw_delay", side_effect=AssertionError("must not redraw")):
            restarted.schedule()
            restarted.run_stage("publish")
        self.assertEqual(f.candidate(7003)["not_before"], row["not_before"])
        f.now += 721
        drain_sender(f.engine)
        restarted.sync_results()
        self.assertEqual(len(f.store.rows("SELECT * FROM attempts")), 1)

    def test_short_ttl_closes_without_task_attempt_or_quota(self):
        f = self.f
        f.config(publishing={"enqueue_delay_min_seconds": 600, "enqueue_delay_max_seconds": 600})
        f.config(scan={"initial_lookback_seconds": 100000})
        f.round([source(7004, "P1", f.now - 86000)])
        f.s.run_stage("generate")
        f.s.run_stage("publish")
        row = f.candidate(7004)
        self.assertEqual(row["closed_reason"], "expires_before_enqueue")
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])
        self.assertEqual(f.store.rows("SELECT * FROM tasks"), [])
        self.assertEqual(f.store.rows("SELECT * FROM quota_reservations"), [])

    def test_result_summary_never_counts_missed_or_unknown_as_confirmed(self):
        f = self.f
        f.round([source(7005, "P1", f.now)])
        f.generate_publish()
        report = f.s.status(f.p["id"])["delivery_summary"]
        self.assertEqual(report["simulated"], 1)
        self.assertEqual(report["confirmed"], 0)
        self.assertEqual(report["unknown"], 0)
        self.assertEqual(report["missed"], 0)

    def test_missing_model_classification_never_becomes_empty_success(self):
        from auto_promotion.drafts import DraftService

        f = self.f
        f.io.classify = lambda *args: DraftService(endpoint="", key="", model="").structured(
            "interaction_classify", "synthetic", {}
        )
        run = f.round([source(7006, "P1", f.now)])
        self.assertEqual(run["state"], "classifying")
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 0)

        self.assertEqual(f.candidate(7006)["block_reason"], "model_connection_unavailable")
        f.now += 301
        f.s.schedule()
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 0)

    def test_delay_default_bounds_and_reconfiguration_never_redraw_existing_intent(self):
        from auto_promotion.interaction_models import InteractionConfig

        settings = InteractionConfig().publishing
        self.assertEqual(
            (settings.enqueue_delay_min_seconds, settings.enqueue_delay_max_seconds), (600, 900)
        )
        f = self.f
        for event, delay in ((7010, 600), (7011, 900)):
            f.config(
                publishing={"enqueue_delay_min_seconds": 600, "enqueue_delay_max_seconds": 900}
            )
            f.round([source(event, "P1", f.now)])
            f.s.run_stage("generate")
            with patch.object(f.s, "draw_delay", return_value=delay):
                f.s.run_stage("publish")
            row = f.candidate(event)
            self.assertEqual(row["not_before"], f.now + delay)
            original = row["not_before"]
            f.config(
                publishing={"enqueue_delay_min_seconds": 1000, "enqueue_delay_max_seconds": 1000}
            )
            self.assertEqual(f.candidate(event)["not_before"], original)

    def test_source_switch_resets_streak_and_old_complete_runs_cannot_enable_p3(self):
        f = self.f
        f.empty(5)
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 5)
        f.config(scan={"source": "imported_results"})
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 0)
        f.s.settle_rounds()
        f.s.assess_fallback()
        self.assertEqual(f.store.rows("SELECT * FROM hf_assessments"), [])

    def test_import_cannot_supply_permissions_future_time_or_cross_project_legacy(self):
        from pydantic import ValidationError

        from auto_promotion.models import CandidateInput, TaskInput

        f = self.f
        f.config(scan={"source": "imported_results"})
        event = {
            k: v for k, v in source(7012, "P1", f.now).items() if not k.startswith("simulation_")
        }
        for field, value in (
            ("eligibility", "eligible"),
            ("coverage_complete", True),
            ("verified_fact_ids", ["demo-fact"]),
            ("classification", {}),
        ):
            with self.assertRaises(ValidationError):
                SourceImport(sources=[{**event, field: value}])
        for timestamp in (True, float("nan"), float("inf")):
            with self.assertRaises(ValidationError):
                SourceImport(sources=[{**event, "source_event_at": timestamp}])
        with self.assertRaises(ValidationError):
            SourceImport(sources=[{**event, "id": 9007199254740993123}])
        with self.assertRaisesRegex(Conflict, "invalid_source_timestamp"):
            f.s.import_sources(
                f.p["id"], SourceImport(sources=[{**event, "source_event_at": f.now + 10}])
            )
        self.assertEqual(f.store.rows("SELECT * FROM hf_candidates"), [])
        task = f.engine.create_task(
            TaskInput(
                name="other project",
                account_ids=[f.account["id"]],
                policy={
                    "project_id": "other",
                    "starts_at": f.iso(f.now - 1),
                    "ends_at": f.iso(f.now + 3600),
                },
            )
        )
        f.engine.candidate(
            task["id"],
            CandidateInput(
                account_id=f.account["id"], target=event["url"], text=event["text"], is_public=True
            ),
        )
        candidate = f.engine.task(task["id"])["candidates"][0]
        with self.assertRaisesRegex(Conflict, "legacy_source_scope_mismatch"):
            f.s.import_sources(
                f.p["id"],
                SourceImport(
                    sources=[
                        {
                            "legacy_candidate_id": candidate["id"],
                            "author_id": "synthetic",
                            "source_event_at": f.now,
                        }
                    ]
                ),
            )

    def test_legacy_import_reuses_public_body_without_restarting_browser(self):
        from auto_promotion.models import CandidateInput, TaskInput

        f = self.f
        f.config(scan={"source": "imported_results"})
        task = f.engine.create_task(
            TaskInput(
                name="saved discovery",
                account_ids=[f.account["id"]],
                policy={"starts_at": f.iso(f.now - 1), "ends_at": f.iso(f.now + 3600)},
            )
        )
        event = source(7013, "P1", f.now)
        f.engine.candidate(
            task["id"],
            CandidateInput(
                account_id=f.account["id"], target=event["url"], text=event["text"], is_public=True
            ),
        )
        candidate = f.engine.task(task["id"])["candidates"][0]
        self.assertEqual(f.s.import_options(f.p["id"])[0]["id"], candidate["id"])
        f.s.import_sources(
            f.p["id"],
            SourceImport(
                sources=[
                    {
                        "legacy_candidate_id": candidate["id"],
                        "author_id": "synthetic",
                        "source_event_at": f.now,
                        "context_status": "complete",
                    }
                ]
            ),
        )
        self.assertEqual(f.candidate(7013)["source"]["text"], event["text"])

    def test_native_and_engine_guard_reject_early_submission_even_if_due_is_changed(self):
        from auto_promotion.interaction_intents import candidate_guard

        f = self.f
        f.config(publishing={"enqueue_delay_min_seconds": 600, "enqueue_delay_max_seconds": 600})
        f.round([source(7014, "P1", f.now)])
        f.s.run_stage("generate")
        f.s.run_stage("publish")
        row = f.candidate(7014)
        with f.store.connect(write=True) as db:
            self.assertEqual(
                candidate_guard(db, row["item_id"], f.now), "candidate_waiting_enqueue"
            )
            db.execute("UPDATE items SET due_at=? WHERE id=?", (f.now, row["item_id"]))
        self.assertIsNone(f.engine._claim(row["item_id"]))
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])

    def test_delayed_item_misses_only_after_the_new_due_window(self):
        f = self.f
        f.config(publishing={"enqueue_delay_min_seconds": 900, "enqueue_delay_max_seconds": 900})
        f.round([source(7015, "P1", f.now)])
        f.s.run_stage("generate")
        f.s.run_stage("publish")
        row = f.candidate(7015)
        f.now += 1801
        drain_sender(f.engine)
        f.s.sync_results()
        self.assertEqual(f.engine.task(row["task_id"])["items"][0]["status"], "missed")
        self.assertEqual(f.s.status(f.p["id"])["delivery_summary"]["missed"], 1)
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])

    def test_delayed_item_still_obeys_cooldown_and_quota(self):
        f = self.f
        f.config(publishing={"enqueue_delay_min_seconds": 600, "enqueue_delay_max_seconds": 600})
        f.round([source(7016, "P1", f.now)])
        f.s.run_stage("generate")
        f.s.run_stage("publish")
        f.now += 600
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET cooldown_until=?", (f.now + 120,))
        drain_sender(f.engine)
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])
        f.now += 121
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET hourly_limit=0")
        drain_sender(f.engine)
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])

    def test_delivery_summary_uses_frozen_mode_and_receipt_not_current_account(self):
        from auto_promotion.delivery_summary import summarize

        result = summarize(
            [
                {
                    "status": "confirmed",
                    "external_reference": "https://x.com/a/status/1",
                    "result": {"verified": True},
                },
                {"status": "unknown"},
                {"status": "missed"},
            ],
            "simulation",
        )
        self.assertEqual(result["confirmed"], 0)
        self.assertEqual(result["simulated"], 1)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["missed"], 1)
        unverified = summarize(
            [{"status": "confirmed", "external_reference": None, "result": {}}], "live"
        )
        self.assertEqual(unverified["unverified"], 1)

    def test_delayed_unknown_survives_restart_without_redraw_or_second_attempt(self):
        f = self.f
        f.config(publishing={"enqueue_delay_min_seconds": 600, "enqueue_delay_max_seconds": 600})
        row = f._unknown(7017)
        self.assertEqual(row["state"], "READY")
        self.assertEqual(len(f.store.rows("SELECT * FROM attempts")), 0)
        f.now += 600
        drain_sender(f.engine)
        f.s.sync_results()
        self.assertEqual(f.candidate(7017)["state"], "VERIFYING")
        f.s = Interactions(f.engine, connectors=f.io)
        with patch.object(
            f.s, "draw_delay", side_effect=AssertionError("unknown must never reroute")
        ):
            f.s.schedule()
            f.s.run_stage("publish")
            drain_sender(f.engine)
        self.assertEqual(len(f.store.rows("SELECT * FROM attempts")), 1)
        self.assertEqual(len(f.store.rows("SELECT * FROM interaction_intents")), 1)

    def test_runtime_missing_model_cannot_turn_empty_api_page_into_healthy_empty_round(self):
        f = self.f
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live'")
        f.io.capabilities = lambda account: {"transport": "x_api", "model": {"configured": False}}
        f.io.discover = lambda *args: {"candidates": [], "coverage_complete": True}
        f.s.worker_available = lambda: True
        run = f.round()
        self.assertEqual(run["state"], "failed")
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 0)

    def test_model_configuration_and_worker_errors_are_safe_readiness_blockers(self):
        from auto_promotion.drafts import DraftService

        for endpoint, model, reason in (
            ("invalid-url", "demo", "model_endpoint_invalid"),
            ("https://example.invalid/model", "", "model_identifier_missing"),
        ):
            service = DraftService(endpoint=endpoint, key="canary-secret", model=model)
            self.assertEqual(service.status()["configuration_error"], reason)
            with self.assertRaisesRegex(Conflict, reason):
                service.structured("interaction_classify", "test", {})
        f = self.f
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live'")
        f.io.capabilities = lambda account: {"transport": "x_api", "model": {"configured": True}}

        def unavailable():
            raise RuntimeError("canary-secret")

        f.s.worker_available = unavailable
        report = f.s.status(f.p["id"])["readiness"]
        self.assertIn("worker_unverified", report["blockers"])
        self.assertNotIn("canary-secret", str(report))

    def test_expired_orphan_handoff_never_rolls_due_or_reopens_delivery_window(self):
        f = self.f
        f.config(publishing={"enqueue_delay_min_seconds": 600, "enqueue_delay_max_seconds": 600})
        for event, stage in ((7020, "before_items"), (7021, "after_items")):
            with self.subTest(stage=stage):
                f.round([source(event, "P1", f.now)])
                f.s.run_stage("generate")
                job = f.s.queue.claim("publish")
                operation = "create_task" if stage == "before_items" else "set_items"
                original = getattr(f.engine, operation)

                def crash(*args, **kwargs):
                    original(*args, **kwargs)
                    raise RuntimeError("synthetic_process_exit")

                with patch.object(f.engine, operation, side_effect=crash):
                    with self.assertRaisesRegex(RuntimeError, "synthetic_process_exit"):
                        f.s._publish(job)
                before = f.store.rows("SELECT id,payload,payload_hash,due_at FROM items")
                fixed = f.candidate(event)["not_before"]
                f.now = fixed + 901
                restarted = Interactions(f.engine, connectors=f.io)
                recovered = restarted.queue.claim("publish")
                with patch.object(
                    restarted, "draw_delay", side_effect=AssertionError("must not redraw")
                ):
                    restarted._publish(recovered)
                row = f.candidate(event)
                self.assertEqual(row["not_before"], fixed)
                self.assertEqual(row["state"], "CLOSED")
                self.assertEqual(row["closed_reason"], "enqueue_window_elapsed")
                self.assertEqual(
                    f.store.rows("SELECT id,payload,payload_hash,due_at FROM items"), before
                )
                self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])
                # Finish this synthetic stage lease so the next subcase is independent.
                with f.store.connect(write=True) as db:
                    restarted.queue.settle(db, recovered)

    def test_attempted_orphan_is_recovered_before_expiry_and_never_classified_as_missed(self):
        f = self.f
        event = 7022
        f.round([source(event, "P1", f.now)])
        f.s.run_stage("generate")
        job = f.s.queue.claim("publish")
        original = f.engine.set_items
        write_calls = []
        adapter = f.engine.adapters.simulation.call

        def unknown(account, operation, payload):
            if operation == "execute":
                write_calls.append(payload["item_id"])
                return {"status": "unknown", "verified": False, "submission_started": True}
            return adapter(account, operation, payload)

        f.engine.adapters.simulation.call = unknown

        def crash_after_external_attempt(*args, **kwargs):
            task = original(*args, **kwargs)
            with f.store.connect(write=True) as db:
                db.execute(
                    "UPDATE interaction_intents SET item_id=? WHERE candidate_id=?",
                    (task["items"][0]["id"], f.candidate(event)["id"]),
                )
            f.engine.start_task(task["id"])
            drain_sender(f.engine)
            raise RuntimeError("synthetic_exit_before_candidate_link")

        with patch.object(f.engine, "set_items", side_effect=crash_after_external_attempt):
            with self.assertRaisesRegex(RuntimeError, "synthetic_exit_before_candidate_link"):
                f.s._publish(job)
        attempt = f.store.rows("SELECT * FROM attempts")
        self.assertEqual(len(attempt), 1)
        f.now = f.candidate(event)["expires_at"] + 1
        restarted = Interactions(f.engine, connectors=f.io)
        restarted.schedule()
        row = f.candidate(event)
        self.assertEqual(row["state"], "VERIFYING")
        self.assertEqual(f.engine.task(row["task_id"])["items"][0]["status"], "unknown")
        restarted.run_stage("publish")
        drain_sender(f.engine)
        self.assertEqual(len(write_calls), 1)
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), attempt)
        self.assertEqual(len(f.store.rows("SELECT * FROM interaction_intents")), 1)

    def test_repeat_import_enable_and_task_start_preserve_one_frozen_handoff(self):
        f = self.f
        f.config(
            scan={"source": "imported_results"},
            publishing={"enqueue_delay_min_seconds": 600, "enqueue_delay_max_seconds": 600},
        )
        event = source(7023, "P1", f.now)
        public = {k: v for k, v in event.items() if not k.startswith("simulation_")}
        f.io.classify = lambda *args: event["simulation_classification"]
        for _ in range(2):
            f.s.import_sources(f.p["id"], SourceImport(sources=[public]))
            f.s.run_stage("classify")
        f.s.run_stage("generate")
        f.s.run_stage("publish")
        row = f.candidate(7023)
        original = f.engine.task(row["task_id"])
        for _ in range(2):
            f.engine.start_task(row["task_id"])
        f.config(enabled=True)
        with patch.object(f.s, "draw_delay", side_effect=AssertionError("no repeat sampling")):
            f.s.run_stage("classify")
            f.s.run_stage("generate")
            f.s.run_stage("publish")
        current = f.engine.task(row["task_id"])
        self.assertEqual(current["manifest_hash"], original["manifest_hash"])
        self.assertEqual(current["manifest"], original["manifest"])
        self.assertEqual(f.candidate(7023)["not_before"], row["not_before"])
        self.assertEqual(len(f.s.candidates(f.p["id"])), 1)
        self.assertEqual(len(f.store.rows("SELECT * FROM items")), 1)
        self.assertEqual(len(f.store.rows("SELECT * FROM interaction_intents")), 1)
        self.assertEqual(len(f.store.rows("SELECT * FROM hf_admissions")), 1)
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])

    def test_existing_five_empty_rounds_cannot_assess_p3_after_model_disappears(self):
        f = self.f
        f.empty(4)
        with patch.object(f.s, "assess_fallback"):
            f.round([source(7024, "P3", f.now)])
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 5)
        self.assertGreater(f.s.policy(f.p["id"])["next_scan_at"], f.now)
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live'")
        f.io.capabilities = lambda account: {"transport": "x_api", "model": {"configured": False}}
        f.s.worker_available = lambda: True
        f.s.schedule()
        self.assertFalse(f.s.status(f.p["id"])["readiness"]["ready"])
        self.assertEqual(f.store.rows("SELECT * FROM hf_assessments"), [])
        self.assertEqual(f.candidate(7024)["state"], "DISCOVERED")

    def test_explicit_one_shot_consumes_stages_without_claiming_daemon_health(self):
        f = self.f
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live'")
        f.io.capabilities = lambda account: {"transport": "x_api", "model": {"configured": True}}
        f.io.discover = lambda *args: {"candidates": [], "coverage_complete": True}
        with f.s.one_shot_execution():
            self.assertEqual(f.s.readiness(f.cfg)["worker"], "one_shot")
            self.assertEqual(f.round()["state"], "complete")
        self.assertEqual(f.s.readiness(f.cfg)["worker"], "unavailable")
