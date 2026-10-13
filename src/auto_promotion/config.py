"""Minimal .env loader: plain values only, no shell evaluation or secret output."""

import os
import re
import shlex
from pathlib import Path


def load_environment(path):
    path = Path(path)
    if not path.is_file():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            continue
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            raise ValueError(f"Invalid .env quoting on line {number}") from None
        os.environ.setdefault(key.strip(), " ".join(parts))


PROJECTS = {
    "telegram": "telegram-mcp",
    "reddit": "reddit-engagement-workflow",
    "instagram": "meta-social-engagement",
    "x": "x-engagement",
    "discord": "discord-bot",
}


def workspace_path():
    return Path(os.getenv("AUTO_PROMOTION_WORKSPACE", Path.cwd().parent)).resolve()


def database_path():
    return Path(os.getenv("AP_DB", "var/auto-promotion.db")).resolve()
