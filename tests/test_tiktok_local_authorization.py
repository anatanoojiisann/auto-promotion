"""TikTok renewal uses the existing fenced queue, never a direct platform writer."""

import unittest
from unittest.mock import patch

import test_local_authorization as local_tests
import test_tiktok_browser as browser_tests
import test_tiktok_messages as message_tests

from auto_promotion.cloud_control import CloudControl, CloudUnavailable
from auto_promotion.core import Conflict, Engine
from auto_promotion.execution_channels import read_state
from auto_promotion.models import canonical


class TikTokLocalAuthorizationTests(unittest.TestCase):
    identity = browser_tests.TikTokBrowserTests.identity
    snapshot = message_tests.TikTokMessageTests.snapshot
    enable = message_tests.TikTokMessageTests.enable
    setup_coordination = local_tests.LocalAuthorizationTests.setup_coordination
    respond = local_tests.LocalAuthorizationTests.respond
    authorize = local_tests.LocalAuthorizationTests.authorize
    assert_no_attempts = local_tests.LocalAuthorizationTests.assert_no_attempts

    def setUp(self):
        browser_tests.TikTokBrowserTests.setUp(self)
        self.addCleanup(patch.stopall)
        self.task, self.item = message_tests.TikTokMessageTests.ready(self, reply=True)
        self.setup_coordination()
        # Reproduce this incident: the old account-wide authority expired yesterday.
        self.offline.update(account_ids=[self.aid, "other-account"], until=self.now - 86400)
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta SET value=? WHERE key='execution_channel'", (canonical(self.offline),)
            )
            db.execute(
                "UPDATE meta SET value=? WHERE key='local_channel_authority'",
                (canonical({"account_ids": self.offline["account_ids"]}),),
            )

    def test_expired_tiktok_authority_can_coordinate_only_the_started_task(self):
        before = self.engine.task(self.task["id"])
        self.assertTrue(before["channel"]["local_authority_expired"])
        self.assertEqual(before["channel"]["local_authorization_accounts"], [self.aid])
        self.assertEqual(before["readiness"]["workflow"]["next_action"], "authorize_local_channel")
        result = self.authorize()
        self.assertTrue(result["authorized"])
        after = self.engine.task(self.task["id"])
        self.assertFalse(after["channel"]["local_authority_expired"])
        self.assertNotEqual(after["readiness"]["workflow"]["status"], "waiting_local_authority")
        self.assertEqual(after["manifest_hash"], before["manifest_hash"])
        self.assertEqual(after["items"], before["items"])
        self.assert_no_attempts()

    def test_repeated_tiktok_coordination_keeps_one_generation(self):
        self.assertEqual(self.authorize(), self.authorize())
        self.assertEqual(sum(p == "/v1/account-channel" for p, _ in self.calls), 1)
        self.assert_no_attempts()

    def test_registered_video_comment_can_use_the_same_coordination(self):
        self.task, self.item = browser_tests.TikTokBrowserTests.ready(self)
        self.assertTrue(self.authorize()["authorized"])
        self.assertFalse(self.engine.task(self.task["id"])["channel"]["local_authority_expired"])
        self.assert_no_attempts()

    def test_valid_scoped_proof_runs_normal_dm_queue_and_one_use_guard(self):
        self.authorize()
        proof = message_tests.TikTokMessageTests.proof(self, self.item)
        with (
            patch.object(CloudControl, "configured", side_effect=AssertionError("cloud access")),
            patch.object(self.engine, "_claim", wraps=Engine._claim.__get__(self.engine)),
        ):
            owner = self.browser.begin(self.item["id"], proof)["owner"]
            permit = self.browser.guard(self.item["id"], owner, proof)
            self.assertTrue(permit["allowed"])
            with self.assertRaises(Conflict):
                self.browser.guard(self.item["id"], owner, proof)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(read_state(self.store), self.offline)

    def test_tiktok_renewal_still_rejects_paused_or_unregistered_accounts(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='paused' WHERE id=?", (self.task["id"],))
        with self.assertRaisesRegex(CloudUnavailable, "RequiresStartedLocalTask"):
            self.authorize()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='active' WHERE id=?", (self.task["id"],))
            db.execute("DELETE FROM meta_browser_accounts WHERE account_id=?", (self.aid,))
        with self.assertRaisesRegex(CloudUnavailable, "RouteUnsupported"):
            self.authorize()
        self.assertEqual(self.calls, [])
        self.assert_no_attempts()

    def test_stale_or_mismatched_task_proof_does_not_hide_expiry(self):
        self.authorize()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE account_task_channels SET state='handoff' WHERE account_id=?", (self.aid,)
            )
        self.assertTrue(self.engine.task(self.task["id"])["channel"]["local_authority_expired"])
        self.assert_no_attempts()


if __name__ == "__main__":
    unittest.main()
