"""Local checks without reading credentials or contacting social platforms."""

import sys
from importlib import metadata
from importlib.util import find_spec
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapters import AdapterRegistry
from .config import database_path, workspace_path


def diagnose():
    packages = {}
    for name in ("fastapi", "pydantic", "uvicorn", "tzdata", "telethon", "playwright", "mcp"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    try:
        ZoneInfo("Asia/Shanghai")
        timezone_ready = True
    except Exception:
        timezone_ready = False
    chromium_ready = False
    if find_spec("playwright"):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as browser:
            chromium_ready = Path(browser.chromium.executable_path).is_file()
    return {
        "python": sys.version.split()[0],
        "interpreter": sys.executable,
        "timezone_ready": timezone_ready,
        "packages": packages,
        "chromium_ready": chromium_ready,
        "database": str(database_path()),
        "services": AdapterRegistry(workspace_path()).services(),
        "account_credentials_checked": False,
        "external_actions": 0,
        "mcp": {
            "installed": packages["mcp"] is not None,
            "server_module": "auto_promotion.mcp_server",
            "transport": "stdio",
        },
    }
