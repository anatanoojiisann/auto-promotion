import unittest

from auto_promotion.models import target_allowed


class TelegramTargetScopeTests(unittest.TestCase):
    def test_only_public_telegram_handles_match_ignoring_case(self):
        self.assertTrue(target_allowed("telegram", "@EXAMPLEGROUP", ["@examplegroup"]))
        self.assertTrue(target_allowed("telegram", "@SECOND_GROUP", ["@second_group"]))
        self.assertFalse(target_allowed("telegram", "@othergroup", ["@examplegroup"]))
        self.assertFalse(target_allowed("x", "@EXAMPLEGROUP", ["@examplegroup"]))
        self.assertFalse(target_allowed("telegram", "https://t.me/+AbCdE", ["https://t.me/+abcde"]))
        self.assertTrue(target_allowed("telegram", "https://t.me/+AbCdE", ["https://t.me/+AbCdE"]))
