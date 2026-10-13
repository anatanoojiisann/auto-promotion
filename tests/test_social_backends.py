import os
import tempfile
import types
import unittest
from unittest.mock import patch

from auto_promotion.backends.browser import BrowserBackend
from auto_promotion.backends.common import BackendError, save_profile
from auto_promotion.backends.instagram import InstagramBackend


class FakeGraph:
    def __init__(self, settings):
        self.calls = []
        self.me = {"user_id": "700", "username": "brand"}
        self.media = [
            {"id": "900", "caption": "exact", "permalink": "https://www.instagram.com/p/exact/"}
        ]

    def request(self, path, *, fields=None, body=None):
        self.calls.append((path, fields, body))
        if path == "me":
            return self.me
        if path == "700/media" and body is None:
            return {"data": self.media}
        if path == "700/media":
            return {"id": "800"}
        if path == "800":
            return {"status_code": "FINISHED"}
        if path == "700/media_publish":
            return {"id": "900"}
        raise AssertionError(path)


class InstagramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        save_profile("instagram", "default", {"identity": "700", "username": "brand"})
        self.backend = InstagramBackend("default", graph_factory=FakeGraph)
        self.checkpoints = []
        self.payload = {
            "payload": {
                "action": "publish",
                "target": "700",
                "target_owned": True,
                "text": "exact",
                "media": [
                    {"media_type": "IMAGE", "source_url": "https://example.test/approved.png"}
                ],
            }
        }

    def test_publish_checkpoints_id_before_independent_owned_media_lookup(self):
        result = self.backend.run("execute", self.payload, self.checkpoints.append, lambda: None)
        self.assertTrue(result["verified"])
        self.assertEqual(self.checkpoints[-1]["media_id"], "900")
        self.assertEqual(self.backend.graph.calls[-1][0], "700/media")

    def test_foreign_account_target_cannot_publish(self):
        self.payload["payload"]["target"] = "701"
        with self.assertRaises(BackendError):
            self.backend.run("execute", self.payload, self.checkpoints.append, lambda: None)
        self.assertFalse(any(body for _, _, body in self.backend.graph.calls))

    def test_changed_identity_cannot_publish(self):
        self.backend.graph.me["username"] = "another"
        with self.assertRaises(BackendError):
            self.backend.run("execute", self.payload, self.checkpoints.append, lambda: None)
        self.assertEqual(len(self.backend.graph.calls), 1)

    def test_missing_receipt_never_publishes_during_reconciliation(self):
        result = self.backend.run(
            "verify",
            self.payload,
            self.checkpoints.append,
            lambda: self.fail("Write during verify"),
        )
        self.assertFalse(result["verified"])
        self.assertFalse(any(body for _, _, body in self.backend.graph.calls))

    def test_caption_mismatch_or_missing_owned_media_is_not_confirmed(self):
        self.backend.graph.media[0]["caption"] = "changed"
        result = self.backend.verify({**self.payload, "receipt": {"media_id": "900"}})
        self.assertFalse(result["verified"])


class RedditReceiptTests(unittest.TestCase):
    def verify(
        self, record, *, action="reply", target="https://www.reddit.com/r/test/comments/abc/title/"
    ):
        response = types.SimpleNamespace(
            ok=True,
            status=200,
            json=lambda: [
                {"kind": "Listing", "data": {"children": [{"kind": "t1", "data": record}]}}
            ],
        )
        context = types.SimpleNamespace(request=types.SimpleNamespace(get=lambda url: response))
        backend = BrowserBackend("reddit", "default", context, None, {"identity": "brand"})
        return backend.verify(
            {
                "payload": {"action": action, "target": target, "text": "exact"},
                "receipt": {
                    "external_reference": "https://www.reddit.com/r/test/comments/abc/title/def/"
                },
            }
        )

    def test_comment_receipt_requires_exact_author_text_and_parent(self):
        record = {"id": "def", "author": "brand", "body": "exact", "parent_id": "t3_abc"}
        self.assertTrue(self.verify(record)["verified"])
        record["parent_id"] = "t3_other"
        self.assertFalse(self.verify(record)["verified"])
        record.update(parent_id="t3_abc", author="other")
        self.assertFalse(self.verify(record)["verified"])

    def test_nested_reply_is_bound_to_exact_comment(self):
        record = {"id": "def", "author": "brand", "body": "exact", "parent_id": "t1_parent"}
        target = "https://www.reddit.com/r/test/comments/abc/title/parent/"
        self.assertTrue(self.verify(record, target=target)["verified"])

    def test_missing_comment_stays_unknown(self):
        result = self.verify({"id": "different", "author": "brand", "body": "exact"})
        self.assertEqual(result["status"], "unknown")
