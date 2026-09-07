import asyncio
import importlib.util
import types
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "integrations/telegram/message_receipt.py"
spec = importlib.util.spec_from_file_location("receipt", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReceiptTests(unittest.TestCase):
    def receipt(self, observed, **kwargs):
        class Client:
            async def get_messages(self, entity, ids):
                return observed

        return asyncio.run(module.verify_message_receipt(Client(), "target", 42, "exact", **kwargs))

    def test_outgoing_message_id_text_and_parent(self):
        m = types.SimpleNamespace(id=42, message="exact", out=True, reply_to_msg_id=5, media=None)
        self.assertTrue(self.receipt(m, reply_to=5)["verified"])
        self.assertFalse(self.receipt(m, reply_to=6)["verified"])
        m.out = False
        self.assertFalse(self.receipt(m)["verified"])

    def test_media_and_changed_caption(self):
        m = types.SimpleNamespace(id=42, message="exact", out=True, media=None)
        self.assertFalse(self.receipt(m, media=True)["verified"])
        m.media = object()
        self.assertTrue(self.receipt(m, media=True)["verified"])
        m.message = "changed"
        self.assertFalse(self.receipt(m, media=True)["verified"])

    def test_read_failure_keeps_id_without_success(self):
        class Client:
            async def get_messages(self, *args, **kwargs):
                raise ConnectionError("private")

        r = asyncio.run(module.verify_message_receipt(Client(), "target", 42, "exact"))
        self.assertFalse(r["verified"])
        self.assertEqual(r["message_id"], 42)
        self.assertNotIn("private", str(r))
