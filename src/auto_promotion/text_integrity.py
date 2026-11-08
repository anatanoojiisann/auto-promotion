"""Reject damaged outbound Unicode without rewriting content or historical receipts."""

import sys


class TextIntegrityError(ValueError):
    safe_code = "InvalidOutboundTextEncoding"

    def __init__(self):
        super().__init__("正文或标题包含乱码替换符或无效字符，已阻止发送；请从原始文案重新导入")


def validate_outbound_text(value):
    """Keep exact valid text, including CJK, emoji, combining marks and newlines."""
    if not isinstance(value, str) or any(
        char == "\ufffd"
        or 0xD800 <= ord(char) <= 0xDFFF
        or ord(char) < 32
        and char not in "\t\r\n"
        or ord(char) == 127
        or ord(char) & 0xFFFF in {0xFFFE, 0xFFFF}
        for char in value
    ):
        raise TextIntegrityError()
    # No replacement or ignore fallback: malformed Unicode must fail closed.
    value.encode("utf-8", errors="strict")
    return value


def validate_outbound_payload(payload):
    for name in ("text", "title"):
        validate_outbound_text(payload.get(name, ""))


def configure_utf8_stdio():
    """Do not let a Windows code page silently recode JSON at a process boundary."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")
