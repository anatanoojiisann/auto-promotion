"""Verify a real stdio handshake and safe calls against the local MCP server."""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from auto_promotion.diagnostics import diagnose


async def check():
    project = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "auto_promotion.mcp_server", "--project-dir", str(project)],
        env={"PYTHONIOENCODING": "utf-8"},
        cwd=project,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            tools = await client.list_tools()
            status = await client.call_tool("system_status", {})
            if status.isError:
                raise RuntimeError("MCP status call failed")
            value = status.structuredContent
            if value is None:
                value = json.loads(status.content[0].text)
            platforms = value["platforms"]
            assert {p["platform"] for p in platforms} == {
                "telegram",
                "reddit",
                "instagram",
                "x",
                "discord",
            }
            assert all(p["installed"] and p["dependencies_ready"] for p in platforms)
            names = {t.name for t in tools.tools}
            assert {"list_accounts", "create_task", "run_approved_task"} <= names
            assert not {"approve", "set_stop", "login", "send_message"} & names
            for platform in ("telegram", "reddit", "instagram", "x", "discord"):
                result = await client.call_tool("list_accounts", {"platform": platform})
                assert not result.isError
            print(
                json.dumps(
                    {
                        "connected": True,
                        "server": initialized.serverInfo.name,
                        "transport": "stdio",
                        "tools": len(names),
                        "platforms": [p["platform"] for p in platforms],
                        "all_backends_ready": True,
                        "external_actions": 0,
                    }
                )
            )


if __name__ == "__main__":
    if not diagnose()["chromium_ready"]:
        raise SystemExit("Project Chromium is not installed")
    asyncio.run(asyncio.wait_for(check(), timeout=45))
