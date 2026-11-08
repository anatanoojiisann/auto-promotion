import asyncio
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_engine as fixtures
from pydantic import ValidationError

from auto_promotion import native_bridge
from auto_promotion.core import Conflict
from auto_promotion.models import ItemInput, canonical, digest
from auto_promotion.text_integrity import TextIntegrityError, validate_outbound_text

ORIGINAL = (
    "视频转写、放大和格式转换串起来时，失败恢复很关键。"
    "这个流水线能从失败节点续跑，并记录每一步的输入文件哈希和模型版本吗？"
    "这样替换某个处理步骤时，更容易定位成片差异。"
)
CORRUPTED = ORIGINAL.encode("gbk").decode("utf-8", errors="replace")


class OutboundTextIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EngineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine = self.fixture.engine

    def ready(self, text=ORIGINAL):
        task = self.fixture.task("x")
        self.engine.set_items(task["id"], [self.fixture.item("x", text=text)])
        return self.engine.start_task(task["id"])

    def damage_frozen_item(self, task):
        item = task["items"][0]
        payload = {**item["payload"], "text": CORRUPTED}
        manifest = {**task["manifest"], "items": [payload]}
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET payload=?,payload_hash=? WHERE id=?",
                (canonical(payload), digest(payload), item["id"]),
            )
            db.execute(
                "UPDATE tasks SET manifest=?,manifest_hash=? WHERE id=?",
                (canonical(manifest), digest(manifest), task["id"]),
            )
        return item["id"]

    def test_actual_gbk_corruption_is_rejected_in_body_and_title(self):
        self.assertEqual(CORRUPTED.count("\ufffd"), 87)
        good = self.fixture.item("x").model_dump()
        for field in ("text", "title"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ItemInput(**{**good, field: CORRUPTED})
        self.assertEqual(self.engine.store.one("SELECT count(*) n FROM items")["n"], 0)

    def test_exact_multilingual_text_survives_save_freeze_and_send(self):
        text = ORIGINAL + "\nEnglish / 日本語 / 한국어 / العربية / e\u0301 / 👩🏽‍💻\t✅"
        task = self.ready(text)
        self.assertEqual(task["manifest"]["items"][0]["text"], text)
        with patch.object(self.fixture.provider, "call", wraps=self.fixture.provider.call) as call:
            self.engine.run_once(task["id"])
        sent = [c.args[2]["payload"]["text"] for c in call.call_args_list if c.args[1] == "execute"]
        self.assertEqual(sent, [text])
        self.assertEqual(self.fixture.provider.sends, 1)

    def test_model_construct_cannot_bypass_save_validation(self):
        task = self.fixture.task("x")
        item = ItemInput.model_construct(
            **{**self.fixture.item("x").model_dump(), "text": CORRUPTED}
        )
        with self.assertRaises(Conflict):
            self.engine.set_items(task["id"], [item])
        self.assertEqual(self.engine.task(task["id"])["items"], [])

    def test_legacy_frozen_damage_is_blocked_before_claim_or_send(self):
        task = self.ready()
        item_id = self.damage_frozen_item(task)
        result = self.engine.run_once(task["id"])
        self.assertEqual(result["processed"][0]["status"], "blocked")
        item = self.engine.task(task["id"])["items"][0]
        self.assertEqual(item["status"], "not_attempted")
        self.assertIsNone(item["attempt_id"])
        self.assertEqual(
            self.engine.task(task["id"])["readiness"]["items"][0]["reason"],
            "outbound_text_encoding_invalid",
        )
        self.assertEqual(self.fixture.provider.sends, 0)
        with self.assertRaises(Conflict):
            self.engine._claim(item_id)
        self.assertEqual(self.engine.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_invalid_unicode_fails_without_changing_valid_text(self):
        for text in ("\ufffd", "bad\ud800", "bad\udfff", "bad\x00", "bad\ufffe"):
            with self.subTest(text=ascii(text)), self.assertRaises(TextIntegrityError):
                validate_outbound_text(text)
        self.assertEqual(validate_outbound_text("\n\r\t正常\U0001f680"), "\n\r\t正常\U0001f680")

    def test_final_bridge_rejects_before_authorization_lock_or_receipt(self):
        for operation in ("preflight", "execute"):
            with (
                self.subTest(operation=operation),
                patch.object(native_bridge, "verify_claim") as claim,
                patch.object(native_bridge, "account_lock") as lock,
                patch.object(native_bridge, "Receipts") as receipts,
            ):
                with self.assertRaises(TextIntegrityError):
                    native_bridge.dispatch(
                        {
                            "platform": "x",
                            "account": "unused",
                            "operation": operation,
                            "payload": {"payload": {"text": CORRUPTED}},
                        }
                    )
                claim.assert_not_called()
                lock.assert_not_called()
                receipts.assert_not_called()

    def test_registry_blocks_new_execution_but_keeps_old_receipts_readable(self):
        account = self.fixture.accounts["x"]
        payload = {"item_id": "old", "payload": {"action": "reply", "text": CORRUPTED}}
        for operation in ("preflight", "execute"):
            with self.assertRaises(TextIntegrityError):
                self.fixture.registry.call(account, operation, payload)
        result = self.fixture.registry.call(account, "verify", payload)
        self.assertTrue(result["verified"])
        self.assertEqual(self.fixture.provider.sends, 0)

    def test_utf8_stdio_overrides_lossy_windows_locale(self):
        code = (
            "import json,sys; from auto_promotion.text_integrity import configure_utf8_stdio; "
            "configure_utf8_stdio(); value=json.load(sys.stdin); "
            "print(json.dumps(value,ensure_ascii=False))"
        )
        environment = {**os.environ, "PYTHONIOENCODING": "gbk:replace"}
        good = subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps({"text": ORIGINAL}, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=environment,
            timeout=20,
        )
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout.decode("utf-8")), {"text": ORIGINAL})
        bad = subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps({"text": ORIGINAL}, ensure_ascii=False).encode("gbk"),
            capture_output=True,
            env=environment,
            timeout=20,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn(b"UnicodeDecodeError", bad.stderr)

    def test_native_child_blocks_damage_even_with_an_old_parent(self):
        request = {
            "platform": "x",
            "account": "unused",
            "operation": "preflight",
            "payload": {"payload": {"text": CORRUPTED}},
        }
        result = subprocess.run(
            [sys.executable, "-m", "auto_promotion.native_bridge"],
            input=json.dumps(request).encode("ascii"),
            capture_output=True,
            timeout=20,
            env={**os.environ, "PYTHONIOENCODING": "gbk:replace"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["bridge_error"], "InvalidOutboundTextEncoding")

    def test_mcp_rejects_damaged_text_before_saving_any_item(self):
        from mcp.server.fastmcp.exceptions import ToolError

        from auto_promotion.mcp_server import create_server

        task = self.fixture.task("x")
        item = {**self.fixture.item("x").model_dump(), "text": CORRUPTED}
        with self.assertRaises(ToolError):
            asyncio.run(
                create_server(self.engine).call_tool(
                    "set_task_items", {"task_id": task["id"], "items": [item]}
                )
            )
        self.assertEqual(self.engine.task(task["id"])["items"], [])


if __name__ == "__main__":
    unittest.main()
