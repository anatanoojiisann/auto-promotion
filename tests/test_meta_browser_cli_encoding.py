import io
import json
import unittest
from contextlib import redirect_stdout

from scripts.meta_browser import emit_result


class MetaBrowserCliEncodingTests(unittest.TestCase):
    def test_gbk_console_preserves_unicode_claim_payload(self):
        claim = {
            "item_id": "item-example",
            "owner": "example-owner",
            "payload": {"text": "¡Casi te quedas encerrada! 😅"},
        }
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk", write_through=True)
        with redirect_stdout(stream):
            emit_result(claim)
        self.assertEqual(json.loads(buffer.getvalue().decode("gbk")), claim)
        stream.detach()


if __name__ == "__main__":
    unittest.main()
