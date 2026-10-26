"""Synthetic _call boundary tests, not observed Telegram wire traffic."""

import asyncio
import math
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from auto_promotion.backends.common import save_profile
from auto_promotion.backends.telegram import TelegramBackend


class SyntheticTelegram:
    def __init__(self, messages=()):
        from telethon import types

        self.me = SimpleNamespace(id=42)
        self.entity = types.Channel(
            id=10,
            title="Public",
            photo=types.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc),
            username="public_group",
            megagroup=True,
            access_hash=12345,
        )
        self.history = list(messages)
        self.calls, self.sends = [], []
        self.fail_history_at = None
        self.history_calls = 0

    async def _call(self, sender, request):
        self.calls.append(type(request).__name__)
        error = getattr(self, "rpc_errors", {}).get(type(request).__name__)
        if error:
            raise error

    async def rpc(self, name):
        await self._call(None, type(name, (), {})())

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        await self.rpc("GetStateRequest")
        return True

    async def get_me(self):
        await self.rpc("GetUsersRequest")
        return self.me

    async def get_entity(self, target):
        await self.rpc("ResolveUsernameRequest")
        return self.entity

    async def get_permissions(self, entity, who):
        await self.rpc("GetParticipantRequest")
        return SimpleNamespace(is_banned=False, has_left=False, participant=None)

    async def get_messages(
        self,
        entity,
        limit=None,
        ids=None,
        min_id=0,
        max_id=0,
        reverse=False,
        offset_id=0,
        add_offset=0,
    ):
        if ids:
            await self.rpc("GetMessagesRequest")
            return [m for m in self.history if m.id in ids]
        values = sorted(
            (
                m
                for m in self.history
                if m.id > min_id
                and (not max_id or m.id < max_id)
                and (not offset_id or m.id < offset_id)
            ),
            key=lambda m: m.id,
            reverse=not reverse,
        )
        values = values[add_offset:][:limit]
        for _ in range(max(1, math.ceil(len(values) / 100))):
            self.history_calls += 1
            await self.rpc("GetHistoryRequest")
            if self.fail_history_at == self.history_calls:
                raise ConnectionError("synthetic interrupted history page")
        return values

    async def __call__(self, request):
        name = type(request).__name__
        await self._call(None, request)
        if name == "SearchRequest":
            return SimpleNamespace(chats=getattr(self, "search_chats", [self.entity]))
        if name == "GetFullChannelRequest":
            return SimpleNamespace(
                full_chat=SimpleNamespace(about="English public group", participants_count=99)
            )
        raise AssertionError(name)

    async def iter_messages(self, entity, *, min_id, reverse, limit):
        await self.rpc("GetHistoryRequest")
        values = sorted(
            (message for message in self.history if message.id > min_id),
            key=lambda message: message.id,
            reverse=not reverse,
        )
        for message in values[:limit]:
            yield message

    async def send_message(self, entity, text, **kwargs):
        await self.rpc("SendMessageRequest")
        self.sends.append(text)
        return SimpleNamespace(id=123, out=True, message=text, media=None)


def messages(count):
    return [
        SimpleNamespace(
            id=i,
            message="English message " + str(i),
            out=False,
            sender_id=100,
            date=datetime(2026, 10, 23, tzinfo=timezone.utc),
        )
        for i in range(1, count + 1)
    ]


class TelegramIncrementalReadTests(unittest.TestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        environment = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": private.name})
        environment.start()
        self.addCleanup(environment.stop)
        save_profile("telegram", "test", {"identity": "42", "api_id": 1, "api_hash": "synthetic"})
        self.client = SyntheticTelegram(messages(30))
        self.policy = {"messages_per_page": 5}

    def run_backend(self, operation="content", **payload):
        backend = TelegramBackend("test", client_factory=lambda *args: self.client)
        return asyncio.run(
            backend.run(
                operation,
                {
                    "target": "@public_group",
                    "limit": 12,
                    "read_policy": self.policy,
                    "read_connection_revision": "r1",
                    **payload,
                },
                lambda result: None,
                lambda: None,
            )
        )

    def test_recent_bootstrap_pages_then_new_messages_without_skipping_backlog(self):
        first = self.run_backend()
        self.assertEqual(
            [int(i["external_id"]) for i in first["content_items"]], list(range(19, 24))
        )
        self.assertFalse(first["read_complete"])
        self.assertTrue(first["read_page_complete"])
        self.assertEqual(first["initial_window_scope"], "recent_requested_limit")
        second = self.run_backend(read_cursor=first["read_cursor_next"])
        third = self.run_backend(read_cursor=second["read_cursor_next"])
        self.assertTrue(third["read_complete"])
        self.assertEqual([int(i["external_id"]) for i in third["content_items"]], [29, 30])
        self.client.history.extend(messages(37)[30:])
        fourth = self.run_backend(read_cursor=third["read_cursor_next"])
        self.assertEqual(
            [int(i["external_id"]) for i in fourth["content_items"]], list(range(31, 36))
        )
        self.assertFalse(fourth["read_complete"])
        # New arrivals above this round's frozen upper bound await the NEXT round.
        self.client.history.extend(messages(40)[37:])
        fifth = self.run_backend(read_cursor=fourth["read_cursor_next"])
        self.assertEqual([int(i["external_id"]) for i in fifth["content_items"]], [36, 37])
        self.assertTrue(fifth["read_complete"])

    def test_filtered_messages_still_advance_the_complete_page_cursor(self):
        self.client.history[20].out = True
        self.client.history[21].message = ""
        page = self.run_backend()
        self.assertEqual(page["read_cursor_next"]["last"], 23)
        self.assertEqual([int(i["external_id"]) for i in page["content_items"]], [19, 20, 23])

    def test_interrupted_page_cannot_advance_cursor_and_resume_repeats_only_that_page(self):
        first = self.run_backend()
        token = first["read_cursor_next"]
        self.client.fail_history_at = self.client.history_calls + 1
        partial = self.run_backend(read_cursor=token)
        self.assertFalse(partial["read_page_complete"])
        self.assertEqual(partial["read_cursor_next"], token)
        self.assertEqual(partial["read_state"], "network_error")
        self.client.fail_history_at = None
        resumed = self.run_backend(read_cursor=token)
        self.assertEqual(
            [int(i["external_id"]) for i in resumed["content_items"]], list(range(24, 29))
        )

    def test_budget_denial_is_partial_without_floodwait_and_never_invokes_denied_rpc(self):
        self.policy.update(requests_per_window=2)
        page = self.run_backend()
        self.assertEqual(page["read_state"], "budget_exhausted")
        self.assertFalse(page["read_page_complete"])
        self.assertEqual(self.client.calls, ["GetStateRequest", "GetUsersRequest"])
        self.assertFalse(page.get("rate_limited", False))
        self.assertNotIn("cooldown_source", page)

    def test_entity_cache_reuses_read_data_but_identity_and_send_permissions_stay_fresh(self):
        first = self.run_backend()
        before = list(self.client.calls)
        second = self.run_backend(read_cursor=first["read_cursor_next"])
        extra = self.client.calls[len(before) :]
        self.assertEqual(extra.count("ResolveUsernameRequest"), 0)
        self.assertEqual(extra.count("GetUsersRequest"), 1)
        self.assertEqual(second["read_cache_hits"], 1)
        self.run_backend("preflight", payload={"target": "@public_group"})
        self.assertEqual(
            self.client.calls[-2:], ["ResolveUsernameRequest", "GetParticipantRequest"]
        )

    def test_unknown_submission_keeps_ids_when_read_budget_ends_after_send(self):
        self.policy.update(requests_per_window=4)
        page = self.run_backend(
            "execute",
            payload={
                "action": "send_message",
                "target": "@public_group",
                "text": "exact",
                "media": [],
            },
        )
        self.assertEqual(page["status"], "unknown")
        self.assertEqual(page["message_ids"], [123])
        self.assertEqual(self.client.sends, ["exact"])

    def test_cache_revision_and_forged_or_repointed_cursor_are_rejected(self):
        first = self.run_backend()
        token = first["read_cursor_next"]
        forged = self.run_backend(read_cursor={**token, "last": 30})
        self.assertFalse(forged["read_page_complete"])
        self.assertEqual(forged["read_state"], "cursor_invalid")
        changed = self.run_backend(read_cursor=token, read_connection_revision="r2")
        self.assertTrue(changed["read_cursor_rebound"])
        self.assertEqual(
            [int(i["external_id"]) for i in changed["content_items"]], list(range(24, 29))
        )
        self.client.entity.id = 11
        self.policy["cache_ttl_seconds"] = 0
        repointed = self.run_backend(read_cursor=token)
        self.assertEqual(repointed["read_state"], "cursor_peer_changed")

    def test_duplicate_search_queries_and_warm_catalog_reduce_logical_rpc_attempts(self):
        policy = {"queries": ["video", "video", "tools"], "max_candidates": 10}
        first = self.run_backend("discover", policy=policy)
        cold = len(self.client.calls)
        second = self.run_backend("discover", policy=policy)
        warm = len(self.client.calls) - cold
        self.assertEqual(len(first["candidates"]), 1)
        self.assertEqual(len(second["candidates"]), 1)
        self.assertEqual(warm, 2, "only fresh identity RPCs remain on a hot directory cache")
        self.assertLess(warm, cold)
        self.assertLessEqual(second["read_observed_at"], first["read_observed_at"])

    def test_directory_paging_freezes_order_across_ttl_expiry_and_process_restart(self):
        from copy import copy

        second = copy(self.client.entity)
        second.id, second.username = 11, "second_group"
        self.client.search_chats = [self.client.entity, second]
        self.policy.update(groups_per_scan=1, cache_ttl_seconds=0)
        policy = {"queries": ["video"], "max_candidates": 10}
        first = self.run_backend("discover", policy=policy)
        self.assertEqual([r["target"] for r in first["candidates"]], ["@public_group"])
        self.client.search_chats.reverse()
        next_page = self.run_backend(
            "discover", policy=policy, read_cursor=first["read_cursor_next"]
        )
        self.assertEqual([r["target"] for r in next_page["candidates"]], ["@second_group"])
        self.assertTrue(next_page["read_complete"])

    def test_small_bootstrap_uses_one_history_call_and_incomplete_budget_resumes_backlog(self):
        small = self.run_backend(limit=5)
        self.assertEqual(self.client.calls.count("GetHistoryRequest"), 1)
        self.assertTrue(small["read_complete"])
        self.assertEqual(
            [int(i["external_id"]) for i in small["content_items"]], list(range(26, 31))
        )

    def test_account_budget_denial_mid_window_keeps_committed_cursor(self):
        self.policy["requests_per_window"] = 8
        first = self.run_backend()
        token = first["read_cursor_next"]
        partial = self.run_backend(read_cursor=token)
        self.assertEqual(partial["read_state"], "budget_exhausted")
        self.assertEqual(partial["read_cursor_next"], token)
        self.assertFalse(partial["read_page_complete"])
        self.policy["requests_per_window"] = 120
        continued = self.run_backend(read_cursor=token)
        self.assertEqual(
            [int(i["external_id"]) for i in continued["content_items"]], list(range(24, 29))
        )

    def test_floodwait_preserves_cursor_and_failed_attempt_count_without_budget_conflation(self):
        from telethon import errors

        first = self.run_backend()
        token = first["read_cursor_next"]
        self.client.rpc_errors = {"GetHistoryRequest": errors.FloodWaitError(None, capture=60)}
        partial = self.run_backend(read_cursor=token)
        self.assertEqual(partial["read_state"], "rate_limited")
        self.assertTrue(partial["rate_limited"])
        self.assertEqual(partial["cooldown_source"], "telegram_rpc_wait")
        self.assertEqual(partial["read_cursor_next"], token)
        self.assertEqual(partial["rpc_counts"]["history"], 1)
        self.assertEqual(partial["rpc_count_source"], "telethon_logical_call_attempt")
        self.assertEqual(partial["read_rpc_wire_attempts"], "not_observed")

    def test_sdk_authorization_helper_cannot_swallow_floodwait_as_logout(self):
        from telethon import errors

        self.client.rpc_errors = {"GetStateRequest": errors.FloodWaitError(None, capture=60)}

        async def sdk_authorized():
            # Match Telethon's real broad RPCError catch in is_user_authorized.
            try:
                await self.client.rpc("GetStateRequest")
            except errors.RPCError:
                return False
            return True

        self.client.is_user_authorized = sdk_authorized
        result = self.run_backend("health")
        self.assertTrue(result.get("rate_limited"))
        self.assertEqual(result["reason"], "FloodWaitError")
        self.assertEqual(result["cooldown_source"], "telegram_rpc_wait")

    def test_pre_submit_budget_denial_does_not_send(self):
        self.policy["requests_per_window"] = 3
        result = self.run_backend(
            "execute",
            payload={
                "action": "send_message",
                "target": "@public_group",
                "text": "exact",
                "media": [],
            },
        )
        self.assertEqual(result["status"], "failed_known")
        self.assertTrue(result["confirmed_not_submitted"])
        self.assertFalse(self.client.sends)
        self.assertNotIn("GetParticipantRequest", self.client.calls)

    def test_budget_denied_inside_send_helper_before_mutation_is_known_not_submitted(self):
        self.policy["requests_per_window"] = 4

        async def send(entity, text, **kwargs):
            await self.client.rpc("GetChannelsRequest")
            await self.client.rpc("SendMessageRequest")
            self.client.sends.append(text)

        self.client.send_message = send
        result = self.run_backend(
            "execute",
            payload={
                "action": "send_message",
                "target": "@public_group",
                "text": "exact",
                "media": [],
            },
        )
        self.assertEqual(result["status"], "failed_known")
        self.assertTrue(result["confirmed_not_submitted"])
        self.assertNotIn("mutation", result["rpc_counts"])
        self.assertFalse(self.client.sends)

    def test_budget_denied_inside_send_helper_after_mutation_stays_unknown(self):
        self.policy["requests_per_window"] = 4

        async def send(entity, text, **kwargs):
            await self.client.rpc("SendMessageRequest")
            self.client.sends.append(text)
            await self.client.rpc("GetChannelsRequest")

        self.client.send_message = send
        result = self.run_backend(
            "execute",
            payload={
                "action": "send_message",
                "target": "@public_group",
                "text": "exact",
                "media": [],
            },
        )
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result.get("confirmed_not_submitted", False))
        self.assertEqual(result["rpc_counts"]["mutation"], 1)
        self.assertEqual(self.client.sends, ["exact"])

    def test_identity_switch_does_not_read_hot_cache_or_history(self):
        self.run_backend()
        before = len(self.client.calls)
        self.client.me.id = 43
        result = self.run_backend()
        self.assertFalse(result["read_identity_verified"])
        self.assertEqual(self.client.calls[before:], ["GetStateRequest", "GetUsersRequest"])
        self.assertEqual(result["content_items"], [])

    def test_page_conversion_failure_never_issues_a_cursor(self):
        self.client.history[18].date = "invalid synthetic date"
        with patch("auto_promotion.backends.telegram_reads.TelegramReads.issue_cursor") as issue:
            with self.assertRaises(AttributeError):
                self.run_backend()
        issue.assert_not_called()

    def test_feedback_peer_mismatch_and_missing_unbound_parent_never_scan(self):
        import time

        receipt = {
            "message_ids": [123],
            "verified": True,
            "status": "confirmed",
            "published_at": time.time() - 100,
            "external_reference": "telegram:@public_group:123",
        }
        payload = {"target": "@public_group", "text": "exact", "media": []}
        mismatch = self.run_backend(
            "feedback", payload=payload, result={**receipt, "message_chat_id": "-1000000000011"}
        )
        self.assertFalse(mismatch["read_page_complete"])
        self.assertEqual(mismatch["read_state"], "cursor_peer_changed")
        self.assertEqual(self.client.history_calls, 0)
        unbound = self.run_backend("feedback", payload=payload, result=receipt)
        self.assertFalse(unbound["read_page_complete"])
        self.assertEqual(unbound["reason"], "feedback_parent_unavailable")
        self.assertEqual(self.client.calls.count("GetHistoryRequest"), 0)


if __name__ == "__main__":
    unittest.main()
