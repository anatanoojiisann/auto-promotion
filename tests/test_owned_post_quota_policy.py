import unittest

from pydantic import ValidationError

from auto_promotion.core import Conflict, Engine
from auto_promotion.models import ItemInput, Policy


class OwnedPostQuotaPolicyTests(unittest.TestCase):
    base = {"starts_at": "2026-10-21T00:00:00Z", "ends_at": "2026-10-22T00:00:00Z"}

    def test_existing_default_remains_two(self):
        self.assertEqual(Policy(**self.base).user_daily_limit, 2)

    def test_three_requires_cloud_owned_single_daily_post(self):
        for values in (
            {},
            {"allow_owned_x_posts": True},
            {"allow_owned_x_posts": True, "execution_channel": "local", "daily_limit": 1},
            {"allow_owned_x_posts": True, "execution_channel": "cloud", "daily_limit": 2},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Policy(**self.base, user_daily_limit=3, **values)
        policy = Policy(
            **self.base,
            user_daily_limit=3,
            allow_owned_x_posts=True,
            execution_channel="cloud",
            daily_limit=1,
        )
        self.assertEqual(policy.user_daily_limit, 3)

    def test_above_three_is_still_rejected(self):
        with self.assertRaises(ValidationError):
            Policy(
                **self.base,
                user_daily_limit=4,
                allow_owned_x_posts=True,
                execution_channel="cloud",
                daily_limit=1,
            )

    def test_quota_three_does_not_accept_a_reply_manifest(self):
        class Probe:
            def task(self, task_id):
                return {"policy": {"user_daily_limit": 3}}

        reply = ItemInput(
            account_id="account",
            action="reply",
            target="https://x.com/brand/status/1",
            text="exact",
            scheduled_at="2026-10-21T01:00:00Z",
        )
        with self.assertRaisesRegex(Conflict, "一条自有账号原创"):
            Engine.set_items(Probe(), "draft", [reply])
