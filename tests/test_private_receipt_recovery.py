"""Synthetic final receipts survive diagnostics/central persistence failures; no platform I/O."""

import copy
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_native_backends as native_fixtures

from auto_promotion import final_receipts, native_bridge
from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.backends.common import Receipts, save_profile
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import AccountInput, ItemInput, TaskInput
from auto_promotion.queue_store import JobQueue
from auto_promotion.routing import route_for
from auto_promotion.store import Store


class PrivateReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = native_fixtures.ClaimTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.engine, self.store, self.account, self.request = (
            fixture.engine,
            fixture.store,
            fixture.account,
            fixture.request,
        )
        self.claim = self.request["payload"]
        self.item_id, self.task_id = self.claim["item_id"], self.claim["task_id"]
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET identity_hash='synthetic-identity'")
        self.queue = JobQueue(self.engine)
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (self.task_id,))
        item = self.store.one("SELECT * FROM items WHERE id=?", (self.item_id,))
        self.queue._put(
            "send",
            self.item_id,
            task,
            self.account,
            route_for(self.account),
            item["payload_hash"],
            time.time() - 1,
            time.time() + 600,
        )
        self.job_id = "send:" + self.item_id
        self.context = {"id": self.job_id, "owner": "synthetic-owner"}
        self.request["execution_job"] = self.context
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE execution_jobs SET state='leased',owner=?,lease_until=?",
                (self.context["owner"], time.time() + 90),
            )
        self.sends = []
        self.quota_before = self.store.rows("SELECT * FROM quota_reservations")

    def submit(self, outcome="confirmed"):
        def provider(label, operation, payload, checkpoint, authorize):
            checkpoint.progress("submit_click", True)
            authorize()
            self.sends.append(True)
            result = {"status": outcome, "verified": True}
            if outcome == "confirmed":
                result["external_reference"] = "telegram:@public_group:123"
                result["message_ids"] = [123]
            checkpoint(result)
            return result

        return provider

    def call(self, outcome="confirmed"):
        with (
            patch("auto_promotion.backends.telegram.call", self.submit(outcome)),
            patch.object(
                self.engine.adapters,
                "call",
                side_effect=lambda *a: native_bridge.dispatch(self.request),
            ),
            self.engine.adapters.execution_job(self.context),
        ):
            return self.engine.call(self.account, "execute", self.claim, self.task_id)

    def expire(self, *, paused=False):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE execution_jobs SET lease_until=0")
            db.execute("UPDATE items SET claimed_at=?", (time.time() - 301,))
            if paused:
                db.execute("UPDATE tasks SET state='paused'")

    def assert_once(self, outcome, *, job_outcome=None):
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], outcome)
        self.queue.recover()
        self.assertEqual(
            self.store.one("SELECT state FROM execution_jobs")["state"], job_outcome or outcome
        )
        self.assertIsNone(self.queue.claim("telegram.mtproto", "replacement"))
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.rows("SELECT * FROM quota_reservations"), self.quota_before)

    def test_confirmed_result_is_not_discarded_when_call_logging_is_locked(self):
        with patch.object(
            self.store, "log_call", side_effect=sqlite3.OperationalError("database is locked")
        ):
            result = self.call()
        self.engine.finish(self.claim, result)
        self.assert_once("confirmed")

    def test_failed_known_result_is_not_discarded_when_call_logging_is_locked(self):
        with patch.object(
            self.store, "log_call", side_effect=sqlite3.OperationalError("database is locked")
        ):
            result = self.call("failed_known")
        self.engine.finish(self.claim, result)
        self.assert_once("failed_known")

    def test_final_receipt_recovers_after_central_finish_and_queue_settle_fail(self):
        result = self.call()
        with patch.object(
            self.engine, "finish", side_effect=sqlite3.OperationalError("database is locked")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.engine.finish(self.claim, result)
        job = self.store.one("SELECT * FROM execution_jobs")
        with patch.object(
            self.queue, "settle", side_effect=sqlite3.OperationalError("database is locked")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.queue.settle(job)
        self.expire(paused=True)
        self.engine.recover()
        self.assert_once("confirmed")
        self.assertEqual(self.store.one("SELECT state FROM tasks")["state"], "paused")

    def test_final_receipt_converges_an_unknown_job_without_reopening_it(self):
        self.call("failed_known")
        self.queue.settle(self.store.one("SELECT * FROM execution_jobs"))
        self.expire()
        self.engine.recover()
        self.assert_once("failed_known")

    def test_legacy_or_mismatched_receipt_stays_unknown_and_cannot_replay(self):
        self.call()
        receipts = Receipts("telegram", "default")
        key = "execute:" + self.item_id
        saved = receipts.read(key)
        saved.pop("receipt_binding", None)
        saved.pop("receipt_finalized_at", None)
        receipts.save(key, saved)
        self.expire()
        self.engine.recover()
        self.assert_once("unknown")
        self.assertEqual(self.store.one("SELECT outcome FROM attempts")["outcome"], "unknown")

    def test_durable_partial_verified_checkpoint_is_not_a_final_receipt(self):
        self.call()
        receipts = Receipts("telegram", "default")
        saved = receipts.read("execute:" + self.item_id)
        saved.pop("receipt_finalized_at", None)
        receipts.save("execute:" + self.item_id, saved)
        self.expire()
        self.engine.recover()
        self.assert_once("unknown")

    def test_every_binding_field_and_final_envelope_are_required(self):
        self.call()
        receipts = Receipts("telegram", "default")
        key = "execute:" + self.item_id
        original = receipts.read(key)
        mutations = [("binding", key) for key in original["receipt_binding"]]
        mutations += [
            ("final", key)
            for key in ("receipt_finalized_at", "receipt_final_hash", "receipt_final_result")
        ]
        for section, field in mutations:
            with self.subTest(section=section, field=field):
                changed = copy.deepcopy(original)
                if section == "binding":
                    changed["receipt_binding"][field] = "mismatched"
                else:
                    changed.pop(field)
                receipts.save(key, changed)
                self.expire()
                self.engine.recover()
                self.assert_once("unknown")
        # Even an otherwise complete old row with the wrong request scope is rejected.
        receipts.save(key, original)
        with receipts.connect() as db:
            db.execute("UPDATE receipts SET payload_hash='not-the-full-claim-hash'")
        self.engine.recover()
        self.assert_once("unknown")

    def test_finalized_timestamp_alone_cannot_promote_old_progress(self):
        self.call()
        receipts = Receipts("telegram", "default")
        receipts.save(
            "execute:" + self.item_id,
            {
                "status": "confirmed",
                "verified": True,
                "external_reference": "synthetic",
                "receipt_finalized_at": time.time(),
            },
        )
        self.expire()
        self.engine.recover()
        self.assert_once("unknown")

    def test_live_or_replaced_lease_is_not_recovered(self):
        self.call()
        self.expire()
        original = final_receipts.proof

        def renew(*args):
            evidence = original(*args)
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE execution_jobs SET owner='replacement',lease_until=?",
                    (time.time() + 90,),
                )
            return evidence

        with patch.object(final_receipts, "proof", side_effect=renew):
            self.engine.recover()
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "executing")
        self.assertEqual(self.store.one("SELECT owner FROM execution_jobs")["owner"], "replacement")
        self.assertEqual(len(self.sends), 1)
        self.expire()
        self.engine.recover()
        self.assert_once("confirmed")

    def test_changed_attempt_between_lookup_and_write_is_not_recovered(self):
        self.call()
        self.expire()
        original = final_receipts.proof

        def replace(*args):
            evidence = original(*args)
            with self.store.connect(write=True) as db:
                db.execute("UPDATE attempts SET id='replacement-attempt'")
                db.execute("UPDATE items SET attempt_id='replacement-attempt'")
            return evidence

        with patch.object(final_receipts, "proof", side_effect=replace):
            self.engine.recover()
        self.assert_once("unknown")

    def test_job_and_account_binding_changes_after_lookup_are_rejected(self):
        self.call()
        original = final_receipts.proof
        job = self.store.one("SELECT * FROM execution_jobs")
        for table, field in (
            ("execution_jobs", "version"),
            ("execution_jobs", "account_id"),
            ("execution_jobs", "task_id"),
            ("execution_jobs", "reference_id"),
            ("accounts", "identity_hash"),
        ):
            with self.subTest(table=table, field=field):
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE execution_jobs SET version=?,account_id=?,task_id=?,reference_id=?,state='leased',lease_until=0",
                        (job["version"], job["account_id"], job["task_id"], job["reference_id"]),
                    )
                    db.execute("UPDATE accounts SET identity_hash='synthetic-identity'")
                    db.execute(
                        "UPDATE items SET status='executing',claimed_at=?", (time.time() - 301,)
                    )

                def change(*args):
                    evidence = original(*args)
                    with self.store.connect(write=True) as db:
                        db.execute(f"UPDATE {table} SET {field}=?", ("changed-after-lookup",))
                    return evidence

                with patch.object(final_receipts, "proof", side_effect=change):
                    self.engine.recover()
                # Existing queue cleanup cancels an orphaned reference. It must
                # not change the item outcome or make that job pending again.
                self.assert_once(
                    "unknown", job_outcome="cancelled" if field == "reference_id" else "unknown"
                )

    def test_later_progress_cannot_replace_the_independent_final_result(self):
        self.call()
        receipts = Receipts("telegram", "default")
        key = "execute:" + self.item_id
        saved = receipts.read(key)
        receipts.save(
            key, {**saved, "status": "unknown", "verified": False, "execution_step": "later_lookup"}
        )
        self.expire()
        self.engine.recover()
        self.assert_once("confirmed")

    def test_final_reconciliation_rolls_back_all_rows_if_audit_fails_then_restart_converges(self):
        self.call()
        self.expire(paused=True)
        with patch(
            "auto_promotion.core.Store.audit", side_effect=sqlite3.OperationalError("locked")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.engine.recover()
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "executing")
        self.assertEqual(self.store.one("SELECT outcome FROM attempts")["outcome"], "claimed")
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "leased")
        restarted = Engine(self.store, self.engine.adapters, live_enabled=True)
        restarted.recover()
        self.assert_once("confirmed")
        self.assertEqual(self.store.one("SELECT state FROM tasks")["state"], "paused")

    def test_corrupt_private_row_keeps_unknown_and_does_not_create_another_attempt(self):
        self.call()
        receipts = Receipts("telegram", "default")
        with receipts.connect() as db:
            db.execute("UPDATE receipts SET result='not-json'")
        self.expire()
        self.engine.recover()
        self.assert_once("unknown")

    def test_final_private_save_failure_does_not_treat_checkpoint_as_final(self):
        save = Receipts.save

        def fail_final(receipts, key, result):
            if "receipt_finalized_at" in result:
                raise sqlite3.OperationalError("database is locked")
            return save(receipts, key, result)

        with patch.object(Receipts, "save", fail_final):
            with self.assertRaises(Conflict):
                self.call()
        self.expire()
        self.engine.recover()
        self.assert_once("unknown")

    def test_unknown_job_with_changed_version_cannot_follow_old_terminal_item(self):
        self.engine.finish(self.claim, self.call())
        with self.store.connect(write=True) as db:
            db.execute("UPDATE execution_jobs SET state='unknown',version='changed'")
        self.queue.recover()
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "unknown")
        self.assertEqual(len(self.sends), 1)

    def test_unchanged_unknown_job_is_not_rewritten_on_every_recovery(self):
        self.engine.finish(self.claim, {"status": "unknown"})
        self.queue.recover()
        before = self.store.one("SELECT * FROM execution_jobs")
        with patch.object(self.queue, "clock", return_value=time.time() + 60):
            self.queue.recover()
        self.assertEqual(self.store.one("SELECT * FROM execution_jobs"), before)

    def test_provider_exception_is_preserved_when_error_audit_also_fails(self):
        with (
            patch.object(self.engine.adapters, "call", side_effect=TimeoutError("private detail")),
            patch.object(self.store, "log_call", side_effect=sqlite3.OperationalError("locked")),
        ):
            with self.assertRaises(Conflict) as raised:
                self.engine.call(self.account, "execute", self.claim, self.task_id)
        self.assertEqual(raised.exception.safe_code, "TimeoutError")
        self.assertNotIn("private detail", str(raised.exception))
        self.assertEqual(self.sends, [])

    def test_call_audit_programming_error_is_not_suppressed(self):
        with patch.object(self.store, "log_call", side_effect=ValueError("programming error")):
            with self.assertRaises(ValueError):
                self.call()
        self.assertEqual(len(self.sends), 1)

    def test_invalid_types_or_nonfinite_final_times_are_not_proof(self):
        self.call()
        receipts = Receipts("telegram", "default")
        key = "execute:" + self.item_id
        original = receipts.read(key)
        for field, value in [
            ("version", True),
            ("version", "1"),
            ("time", True),
            ("time", float("inf")),
            ("time", float("nan")),
            ("time", None),
        ]:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(original)
                if field == "version":
                    changed["receipt_binding"]["version"] = value
                else:
                    changed["receipt_finalized_at"] = value
                # Simulate an old/corrupt on-disk row; the normal writer already
                # rejects non-finite JSON before persistence.
                with receipts.connect() as db:
                    db.execute(
                        "UPDATE receipts SET result=? WHERE id=?", (json.dumps(changed), key)
                    )
                self.expire()
                self.engine.recover()
                self.assert_once("unknown")

    def test_x_final_requires_target_and_true_verification_schedules_stay_unknown(self):
        item = {"action": "reply"}
        result = {
            "status": "confirmed",
            "verified": True,
            "target_verified": True,
            "external_reference": "https://x.com/synthetic/status/1",
        }
        self.assertTrue(final_receipts.valid_result(result, "x", item))
        for changes in (
            {"verified": 1},
            {"verified": False},
            {"target_verified": False},
            {"target_verified": 1},
            {"external_reference": ""},
            {"status": "unknown"},
            {"status": "scheduled", "scheduled": True, "active": True},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(final_receipts.valid_result({**result, **changes}, "x", item))
        self.assertTrue(final_receipts.valid_result({"status": "failed_known"}, "x", item))

    def test_x_confirmed_and_failed_known_recover_with_one_platform_call(self):
        class IdentityAdapter(SimulationAdapter):
            def call(self, account, operation, payload):
                result = super().call(account, operation, payload)
                if operation == "health":
                    result["account_identity"] = "synthetic"
                return result

        for outcome in ("confirmed", "failed_known"):
            with (
                self.subTest(outcome=outcome),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(
                    os.environ,
                    {
                        "AP_ACCOUNTS_DIR": str(Path(directory) / "accounts"),
                        "AP_DB": str(Path(directory) / "test.db"),
                        "AP_X_OWNER": "auto-promotion",
                        "AUTO_PROMOTION_LIVE_WRITES": "1",
                    },
                ),
            ):
                store = Store(Path(directory) / "test.db")
                registry = AdapterRegistry(Path(directory), overrides={"x": IdentityAdapter()})
                engine = Engine(store, registry, live_enabled=True)
                save_profile(
                    "x", "synthetic-x", {"identity": "synthetic", "automatic_actions": ["reply"]}
                )
                account = engine.add_account(
                    AccountInput(platform="x", label="synthetic", backend_label="synthetic-x")
                )
                engine.check_account(account["id"])
                now = time.time()
                task = engine.create_task(
                    TaskInput(
                        name="synthetic-x",
                        account_ids=[account["id"]],
                        policy={
                            "starts_at": native_fixtures.iso(now - 60),
                            "ends_at": native_fixtures.iso(now + 3600),
                        },
                    )
                )
                engine.start_task(task["id"], sending=False)
                engine.set_items(
                    task["id"],
                    [
                        ItemInput(
                            account_id=account["id"],
                            action="reply",
                            target="https://x.com/synthetic/status/456",
                            text="synthetic test only",
                            scheduled_at=native_fixtures.iso(now - 1),
                        )
                    ],
                )
                engine.prepare(task["id"])
                engine.start_task(task["id"])
                engine.set_stop(False)
                item = engine.task(task["id"])["items"][0]
                claim = engine._claim(item["id"], {"allowed": True})
                self.assertIsNotNone(claim)
                queue = JobQueue(engine)
                item = store.one("SELECT * FROM items")
                queue._put(
                    "send",
                    item["id"],
                    store.one("SELECT * FROM tasks"),
                    account,
                    route_for(account, claim["payload"]),
                    item["payload_hash"],
                    now - 1,
                    now + 600,
                )
                context = {"id": "send:" + item["id"], "owner": "synthetic-x-owner"}
                with store.connect(write=True) as db:
                    db.execute(
                        "UPDATE execution_jobs SET state='leased',owner=?,lease_until=?",
                        (context["owner"], now + 90),
                    )
                quota = store.rows("SELECT * FROM quota_reservations")
                calls = []

                def provider(platform, label, operation, payload, checkpoint, authorize):
                    checkpoint.progress("submit_click", True)
                    authorize()
                    calls.append(operation)
                    result = {"status": outcome, "verified": True, "target_verified": True}
                    if outcome == "confirmed":
                        result["external_reference"] = "https://x.com/synthetic/status/123"
                    checkpoint(result)
                    return result

                request = {
                    "platform": "x",
                    "account": "synthetic-x",
                    "account_id": account["id"],
                    "database": store.path,
                    "operation": "execute",
                    "payload": claim,
                    "execution_job": context,
                }
                with patch("auto_promotion.backends.browser.call", provider):
                    native_bridge.dispatch(request)
                with store.connect(write=True) as db:
                    db.execute("UPDATE execution_jobs SET lease_until=0")
                    db.execute("UPDATE items SET claimed_at=?", (now - 301,))
                restarted = Engine(store, registry, live_enabled=True)
                restarted.recover()
                restarted.recover()
                self.assertEqual(store.one("SELECT status FROM items")["status"], outcome)
                self.assertEqual(store.one("SELECT state FROM execution_jobs")["state"], outcome)
                self.assertEqual(calls, ["execute"])
                self.assertEqual(store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)
                self.assertEqual(store.rows("SELECT * FROM quota_reservations"), quota)
                self.assertIsNone(queue.claim("x.browser", "replacement"))
