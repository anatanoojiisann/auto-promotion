import asyncio
import copy
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_meta_browser as fixtures
from test_api import request

from auto_promotion.api import create_app
from auto_promotion.cloud_control import CloudControl, CloudUnavailable
from auto_promotion.continuation_preflight import local_handoff_preflight
from auto_promotion.execution_channels import binding, extend_offline_scope, read_state
from auto_promotion.local_authorization import authorize_local_channel
from auto_promotion.meta_migration import register
from auto_promotion.models import ItemInput, TaskInput, canonical
from auto_promotion.queue_store import JobQueue
from auto_promotion.routing import supports_independent_local
from auto_promotion.task_channels import TaskChannelRouter, channel_binding


class LocalAuthorizationTests(unittest.TestCase):
    write_source = fixtures.MetaBrowserTests.write_source
    identity = fixtures.MetaBrowserTests.identity
    ready = fixtures.MetaBrowserTests.ready

    def setUp(self):
        fixtures.MetaBrowserTests.setUp(self)
        patch.dict("os.environ", {"AP_NODE_ID": "synthetic-node"}).start()
        self.task, self.item = self.ready()
        self.now = time.time()
        self.engine.clock = lambda: self.now
        self.store_id = binding(self.engine)
        self.offline = {
            "active": "local",
            "authorization": "local_queue",
            "offline_only": True,
            "until": self.now + 4000,
            "account_ids": ["other-account"],
            "node_id": "synthetic-node",
            "store_id": self.store_id,
            "generation": "other-scope",
        }
        with self.store.connect(write=True) as db:
            for key, value in (
                ("cloud_control_required", "1"),
                ("execution_channel_policy", "cloud_then_local"),
                ("local_channel_authority", canonical({"account_ids": ["other-account"]})),
                ("execution_channel", canonical(self.offline)),
            ):
                db.execute("INSERT INTO meta VALUES(?,?)", (key, value))
        self.cloud = CloudControl(
            "http://127.0.0.1:1",
            node_id="synthetic-node",
            node_token="synthetic",
            operator_token="synthetic",
        )
        self.remote = None
        self.calls = []
        self.after_post = None
        patch.object(CloudControl, "configured", return_value=self.cloud).start()
        patch.object(self.cloud, "request", side_effect=self.respond).start()
        self.claim = patch.object(
            self.engine, "_claim", side_effect=AssertionError("outbound claim")
        ).start()

    def respond(self, path, payload=None, **kwargs):
        self.calls.append((path, copy.deepcopy(payload)))
        if path == "/v1/account-channel/status":
            return {"channel": copy.deepcopy(self.remote)}
        if path == "/v1/account-channel":
            expected = self.remote["generation"] if self.remote else None
            if payload["expected_generation"] != expected:
                raise CloudUnavailable("AccountChannelGenerationConflict")
            self.remote = {
                k: payload[k] for k in ("account_id", "task_id", "node_id", "generation", "channel")
            }
            self.remote.update(store_id=payload["local_store_id"], until_at=payload["until"])
            if self.after_post:
                self.after_post()
            return {"channel": copy.deepcopy(self.remote)}
        self.assertEqual(path, "/v1/sync")
        return {}

    def authorize(self, **kwargs):
        return authorize_local_channel(
            self.engine,
            self.task["id"],
            self.aid,
            coordinate_once=True,
            source="synthetic explicit instruction",
            **kwargs,
        )

    def assert_no_attempts(self):
        self.claim.assert_not_called()
        for table in ("attempts", "join_attempts", "publication_evidence", "task_channel_claims"):
            self.assertEqual(self.store.one("SELECT count(*) n FROM " + table)["n"], 0)
        self.assertEqual(read_state(self.store), self.offline)
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='cloud_control_required'")["value"],
            "1",
        )

    def test_first_facebook_coordination_only_preserves_scope_and_manifest(self):
        before = self.store.one("SELECT * FROM items WHERE id=?", (self.item["id"],))
        result = self.authorize()
        self.assertTrue(result["authorized"])
        self.assertTrue(result["authorization_only"])
        self.assertFalse(result["attempt_created"])
        self.assertEqual(
            before, self.store.one("SELECT * FROM items WHERE id=?", (self.item["id"],))
        )
        route = self.store.one(
            "SELECT * FROM account_task_channels WHERE account_id=?", (self.aid,)
        )
        self.assertEqual(
            (route["channel"], route["state"], route["fence_source"]),
            ("local", "ready", "cloud_acknowledged"),
        )
        with self.store.connect() as db:
            proof = channel_binding(db, self.aid)
        self.assertEqual(proof["coordination_task_id"], self.task["id"])
        self.assertEqual(proof["coordination_manifest_hash"], self.task["manifest_hash"])
        self.assertIsNone(
            self.store.one(
                "SELECT value FROM meta WHERE key=?", ("cloud_task_health:" + self.task["id"],)
            )
        )
        self.assert_no_attempts()

    def test_repeated_request_reuses_same_generation_and_never_reposts(self):
        first, second = self.authorize(), self.authorize()
        self.assertEqual(first, second)
        self.assertEqual(sum(p == "/v1/account-channel" for p, _ in self.calls), 1)
        self.assert_no_attempts()

    def test_preflight_accepts_current_scoped_proof_without_faking_full_task_sync(self):
        self.authorize()
        value = local_handoff_preflight(
            self.engine,
            self.aid,
            self.task["id"],
            required_until=self.now + 600,
            replacement=False,
            item_ids=[self.item["id"]],
        )
        self.assertTrue(value["ready"])
        self.assertIsNone(
            self.store.one(
                "SELECT value FROM meta WHERE key=?", ("cloud_task_health:" + self.task["id"],)
            )
        )
        replacement = local_handoff_preflight(
            self.engine,
            self.aid,
            self.task["id"],
            required_until=self.now + 600,
            replacement=True,
            item_ids=[self.item["id"]],
        )
        self.assertEqual(replacement["reason"], "local_authority_task_scope_limited")
        self.assert_no_attempts()

    def test_scoped_authority_can_be_consumed_locally_without_cloud_or_attempt(self):
        # Another account's offline node binding must not replace this task's node.
        self.offline["node_id"] = "unrelated-scope-node"
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta SET value=? WHERE key='execution_channel'", (canonical(self.offline),)
            )
        self.authorize()
        queue = JobQueue(self.engine)
        queue.sync(self.task["id"])
        job = queue.claim("facebook.desktop", "synthetic-sender")
        self.assertIsNotNone(job)
        with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud access")):
            context, cloud = TaskChannelRouter(self.engine).prepare(job)
        self.assertIsNone(cloud)
        self.assertEqual(context["channel"], "local")
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.claim.assert_not_called()

    def test_registered_facebook_extension_reuses_proof_and_keeps_other_scope(self):
        self.authorize()
        with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud access")):
            result = extend_offline_scope(
                self.engine, [self.aid], source="explicit_user_local_task"
            )
        self.assertEqual(result["added"], [self.aid])
        after = read_state(self.store)
        self.assertEqual(set(after["account_ids"]), {self.aid, "other-account"})
        self.assertEqual(after["generation"], self.offline["generation"])
        self.assertEqual(after["until"], self.offline["until"])
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_registered_facebook_without_proof_does_not_bypass_ownership(self):
        with self.assertRaisesRegex(CloudUnavailable, "OfflinePriorOwnershipUnverified"):
            extend_offline_scope(self.engine, [self.aid], source="explicit_user_local_task")
        self.assert_no_attempts()

    def test_unregistered_meta_and_unreviewed_routes_are_not_allowed(self):
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM meta_browser_accounts WHERE account_id=?", (self.aid,))
        with self.assertRaisesRegex(CloudUnavailable, "RouteUnsupported"):
            self.authorize()
        self.assertEqual(self.calls, [])
        self.assert_no_attempts()

    def test_scoped_sync_uploads_no_other_account_or_job_and_no_settings(self):
        self.authorize()
        for path, payload in self.calls:
            self.assertIn(path, {"/v1/sync", "/v1/account-channel/status", "/v1/account-channel"})
            if path == "/v1/sync":
                self.assertTrue(all(a["id"] == self.aid for a in payload["accounts"]))
                self.assertTrue(all(j["account_id"] == self.aid for j in payload["jobs"]))
                self.assertTrue(all(t["id"] == self.task["id"] for t in payload["tasks"]))
        self.assert_no_attempts()

    def test_multi_account_task_coordinates_only_selected_account(self):
        registration = copy.deepcopy(self.registrations[0])
        registration.update(account_id="facebook:987654321", label="Another synthetic account")
        registration["details"]["profile_url"] = "https://www.facebook.com/profile.php?id=987654321"
        other = register(self.browser, [registration])[0]
        connection = self.browser.connection(other)
        self.browser.observe(
            other,
            {
                "identity": connection["identity"],
                "profile_url": connection["profile_url"],
                "browser": connection["browser"],
                "identity_verified": True,
                "observed_at": self.now,
                "evidence": "Synthetic current identity",
            },
        )
        task = self.engine.create_task(
            TaskInput(
                name="Synthetic multi-account local task",
                account_ids=[self.aid, other],
                policy={**self.task["policy"], "execution_channel": "local"},
            )
        )
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=aid,
                    action="comment",
                    target=f"https://www.facebook.com/groups/example/posts/{90001 + n}/",
                    text="Synthetic frozen text",
                    scheduled_at=fixtures.iso(self.now + n * 120),
                )
                for n, aid in enumerate((self.aid, other))
            ],
        )
        self.engine.start_task(task["id"])
        self.task = self.engine.task(task["id"])
        before = self.store.one("SELECT * FROM accounts WHERE id=?", (other,))
        self.authorize()
        self.assertEqual(before, self.store.one("SELECT * FROM accounts WHERE id=?", (other,)))
        self.assertIsNone(
            self.store.one("SELECT * FROM account_task_channels WHERE account_id=?", (other,))
        )
        other_item = next(i for i in self.task["items"] if i["account_id"] == other)
        self.assertIsNone(
            self.store.one(
                "SELECT * FROM execution_job_routes WHERE job_id=?", ("send:" + other_item["id"],)
            )
        )
        for path, payload in self.calls:
            if path == "/v1/sync":
                self.assertTrue(all(a["id"] == self.aid for a in payload["accounts"]))
                self.assertTrue(all(j["account_id"] == self.aid for j in payload["jobs"]))
        self.assert_no_attempts()

    def test_other_task_cannot_borrow_the_coordination_proof(self):
        self.authorize()
        original = self.store.one(
            "SELECT * FROM account_task_channels WHERE account_id=?", (self.aid,)
        )
        # Build a different legitimate frozen task; do not execute its item.
        self.engine.clock = time.time
        other, _ = self.ready("https://www.facebook.com/groups/example/posts/999999/")
        self.now = time.time()
        self.engine.clock = lambda: self.now
        queue = JobQueue(self.engine)
        queue.sync(other["id"])
        job = queue.claim(
            "facebook.desktop",
            "other-sender",
            reference_id=self.engine.task(other["id"])["items"][0]["id"],
        )
        self.assertIsNotNone(job)
        with self.assertRaisesRegex(CloudUnavailable, "LocalAuthorizationTaskScopeMismatch"):
            TaskChannelRouter(self.engine).prepare(job)
        self.assertEqual(
            original,
            self.store.one("SELECT * FROM account_task_channels WHERE account_id=?", (self.aid,)),
        )
        self.assert_no_attempts()

    def test_requires_explicit_scope_without_starting_a_draft(self):
        with self.assertRaisesRegex(ValueError, "ExplicitLocalCoordinationRequired"):
            authorize_local_channel(self.engine, self.task["id"], self.aid, source="test")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='draft' WHERE id=?", (self.task["id"],))
        with self.assertRaisesRegex(CloudUnavailable, "RequiresStartedLocalTask"):
            self.authorize()
        self.assertEqual(self.calls, [])
        self.assert_no_attempts()

    def test_lost_response_is_reconciled_with_same_generation(self):
        def lost():
            raise CloudUnavailable("CloudTransportUnavailable")

        self.after_post = lost
        with self.assertRaisesRegex(CloudUnavailable, "CloudTransportUnavailable"):
            self.authorize()
        generation = self.remote["generation"]
        JobQueue(self.engine).sync(self.task["id"])
        self.assertIsNone(JobQueue(self.engine).claim("facebook.desktop", "sender"))
        self.now += 31
        self.after_post = None
        result = self.authorize()
        self.assertEqual(result["generation"], generation)
        self.assertEqual(sum(p == "/v1/account-channel" for p, _ in self.calls), 1)
        self.assert_no_attempts()

    def test_interrupted_process_keeps_lease_then_recovers(self):
        def crash():
            raise KeyboardInterrupt()

        self.after_post = crash
        with self.assertRaises(KeyboardInterrupt):
            self.authorize()
        with self.assertRaisesRegex(CloudUnavailable, "LocalAuthorizationBusy"):
            self.authorize()
        generation = self.remote["generation"]
        self.now += 181
        self.after_post = None
        self.assertEqual(self.authorize()["generation"], generation)
        self.assert_no_attempts()

    def test_mismatched_response_stays_pending(self):
        self.after_post = lambda: self.remote.update(store_id="different-store")
        with self.assertRaisesRegex(CloudUnavailable, "TaskChannelHandoffUnverified"):
            self.authorize()
        self.assertEqual(
            self.store.one("SELECT state FROM account_task_channels")["state"], "handoff"
        )
        self.assertEqual(
            self.store.one("SELECT state FROM channel_authorization_requests")["state"], "pending"
        )
        self.assert_no_attempts()

    def test_remote_generation_changed_after_read_is_not_overwritten(self):
        original = self.respond

        def conflict(path, payload=None, **kwargs):
            if path == "/v1/account-channel":
                self.remote = {"generation": "newer-generation"}
            return original(path, payload, **kwargs)

        with patch.object(self.cloud, "request", side_effect=conflict):
            with self.assertRaisesRegex(CloudUnavailable, "AccountChannelGenerationConflict"):
                self.authorize()
        self.assertEqual(self.remote["generation"], "newer-generation")
        self.assertEqual(
            self.store.one("SELECT state FROM account_task_channels")["state"], "handoff"
        )
        self.assert_no_attempts()

    def test_scope_change_or_task_expiry_during_response_is_blocked(self):
        def expire():
            self.now += 1000

        self.after_post = expire
        with self.assertRaisesRegex(CloudUnavailable, "RequiresStartedLocalTask"):
            self.authorize()
        self.assertEqual(
            self.store.one("SELECT state FROM account_task_channels")["state"], "handoff"
        )
        self.assert_no_attempts()

    def test_forged_control_context_cannot_bypass_normal_guard(self):
        job = {
            "id": "not-a-lease",
            "task_id": self.task["id"],
            "account_id": self.aid,
            "version": self.task["manifest_hash"],
            "owner": "nobody",
            "authorization_only": True,
        }
        with self.assertRaisesRegex(CloudUnavailable, "LocalAuthorizationLeaseExpired"):
            TaskChannelRouter(self.engine).prepare(job)
        with self.assertRaisesRegex(CloudUnavailable, "LocalAuthorizationLeaseExpired"):
            self.cloud.sync_channel_authorization(self.engine, job)
        self.assertEqual(self.calls, [])
        self.assert_no_attempts()

    def test_lease_expiry_or_stop_during_response_never_commits_ready(self):
        self.after_post = lambda: setattr(self, "now", self.now + 181)
        with self.assertRaisesRegex(CloudUnavailable, "PersistentJobClaimExpired"):
            self.authorize()
        self.assertEqual(
            self.store.one("SELECT state FROM account_task_channels")["state"], "handoff"
        )
        self.assert_no_attempts()

    def test_stop_during_response_never_commits_ready(self):
        def stop():
            with self.store.connect(write=True) as db:
                db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")

        self.after_post = stop
        with self.assertRaisesRegex(CloudUnavailable, "global_stop"):
            self.authorize()
        self.assertEqual(
            self.store.one("SELECT state FROM account_task_channels")["state"], "handoff"
        )
        self.assert_no_attempts()

    def test_concurrent_clicks_have_one_owner_and_block_sender(self):
        entered, release = threading.Event(), threading.Event()
        original = self.respond

        def wait(path, payload=None, **kwargs):
            if path == "/v1/account-channel/status":
                entered.set()
                self.assertTrue(release.wait(10))
            return original(path, payload, **kwargs)

        with patch.object(self.cloud, "request", side_effect=wait), ThreadPoolExecutor() as pool:
            first = pool.submit(self.authorize)
            self.assertTrue(entered.wait(10))
            try:
                with self.assertRaisesRegex(CloudUnavailable, "LocalAuthorizationBusy"):
                    self.authorize()
                JobQueue(self.engine).sync(self.task["id"])
                self.assertIsNone(JobQueue(self.engine).claim("facebook.desktop", "sender"))
            finally:
                release.set()
            self.assertTrue(first.result()["authorized"])
        self.assert_no_attempts()

    def test_no_cloud_configuration_is_not_offline_self_authorization(self):
        with patch.object(CloudControl, "configured", return_value=None):
            with self.assertRaisesRegex(CloudUnavailable, "CloudControlConfigurationMissing"):
                self.authorize()
        self.assert_no_attempts()

    def test_node_mismatch_and_other_pending_control_are_blocked(self):
        self.cloud.node_id = "different-node"
        with self.assertRaisesRegex(CloudUnavailable, "LocalChannelBindingMismatch"):
            self.authorize()
        self.assertEqual(self.calls, [])
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('cloud_pending_settings','{}')")
        with self.assertRaisesRegex(CloudUnavailable, "OtherControlPending"):
            self.authorize()
        self.assert_no_attempts()

    def test_stops_cooldowns_receipts_and_ownership_cas_are_retained(self):
        for sql, args, expected in (
            (
                "UPDATE accounts SET cooldown_until=? WHERE id=?",
                (self.now + 10, self.aid),
                "AccountUnavailable",
            ),
            ("UPDATE accounts SET disabled=1 WHERE id=?", (self.aid,), "AccountUnavailable"),
        ):
            with self.subTest(expected=expected):
                with self.store.connect(write=True) as db:
                    db.execute(sql, args)
                with self.assertRaisesRegex(CloudUnavailable, expected):
                    self.authorize()
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE accounts SET disabled=0,cooldown_until=0 WHERE id=?", (self.aid,)
                    )
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO cloud_receipt_outbox VALUES('pending',?,NULL,?,0,NULL)",
                (canonical({"account_id": self.aid}), self.now),
            )
        with self.assertRaisesRegex(CloudUnavailable, "account_receipt_pending"):
            self.authorize()
        self.assertEqual(self.calls, [])
        self.assert_no_attempts()

    def test_public_api_requires_explicit_intent_and_never_starts_or_sends(self):
        app = create_app(
            db_path=self.store.path,
            registry=self.engine.adapters,
            clock=self.engine.clock,
            token="synthetic-api",
        )
        path = "/api/tasks/" + self.task["id"] + "/authorize-local-channel"
        body = {"account_id": self.aid, "coordinate_once": True, "source": "synthetic UI click"}
        headers = {"authorization": "Bearer synthetic-api"}
        status, _, _ = asyncio.run(request(app, path, "POST", body))
        self.assertEqual(status, 401)
        status, _, _ = asyncio.run(
            request(app, path, "POST", {**body, "coordinate_once": False}, headers)
        )
        self.assertEqual(status, 400)
        status, result, _ = asyncio.run(request(app, path, "POST", body, headers))
        self.assertEqual(status, 200)
        self.assertTrue(result["authorization_only"])
        self.assert_no_attempts()

    def test_mcp_entry_is_explicit_control_coordination_not_local_read(self):
        from auto_promotion.mcp_server import create_server

        server = create_server(self.engine)
        tools = asyncio.run(server.list_tools())
        tool = next(t for t in tools if t.name == "authorize_local_channel")
        self.assertTrue(tool.annotations.openWorldHint)
        self.assertFalse(tool.annotations.readOnlyHint)
        asyncio.run(
            server.call_tool(
                "authorize_local_channel",
                {
                    "task_id": self.task["id"],
                    "account_id": self.aid,
                    "coordinate_once": True,
                    "source": "synthetic human instruction",
                },
            )
        )
        self.assert_no_attempts()

    def test_mcp_failure_reports_safe_specific_reason(self):
        from mcp.server.fastmcp.exceptions import ToolError

        from auto_promotion.mcp_server import create_server

        server = create_server(self.engine)
        with patch.object(CloudControl, "configured", return_value=None):
            with self.assertRaisesRegex(ToolError, "CloudControlConfigurationMissing"):
                asyncio.run(
                    server.call_tool(
                        "authorize_local_channel",
                        {
                            "task_id": self.task["id"],
                            "account_id": self.aid,
                            "coordinate_once": True,
                            "source": "synthetic human instruction",
                        },
                    )
                )
        self.assert_no_attempts()


class ReviewedLocalRouteTests(unittest.TestCase):
    def test_route_capability_is_distinct_from_platform_and_action_lists(self):
        for platform in ("facebook", "instagram"):
            account = {
                "id": "synthetic",
                "platform": platform,
                "backend_label": "synthetic",
                "mode": "live",
            }
            with self.subTest(platform=platform):
                self.assertTrue(
                    supports_independent_local(
                        account,
                        profile={
                            "connection_type": "meta_desktop",
                            "browser_channel": "browser_use",
                        },
                    )
                )
                self.assertFalse(supports_independent_local(account, profile={}))
                self.assertFalse(
                    supports_independent_local(account, profile={"connection_type": "meta_desktop"})
                )
        for platform in ("telegram", "discord", "reddit", "tiktok"):
            self.assertFalse(
                supports_independent_local(
                    {
                        "id": "synthetic",
                        "platform": platform,
                        "backend_label": "synthetic",
                        "mode": "live",
                    },
                    profile={},
                )
            )


if __name__ == "__main__":
    unittest.main()
