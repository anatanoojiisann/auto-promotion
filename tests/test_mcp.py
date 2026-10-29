import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.core import Engine
from auto_promotion.models import PLATFORMS, AccountInput, ItemInput, TaskInput
from auto_promotion.store import Store

MCP_AVAILABLE = find_spec("mcp") is not None
if MCP_AVAILABLE:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.server.fastmcp.exceptions import ToolError

    from auto_promotion.mcp_server import client_config, create_server


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class CountingAdapter(SimulationAdapter):
    def __init__(self):
        self.sends = 0
        self.reads = 0

    def call(self, account, operation, payload):
        if operation == "execute":
            self.sends += 1
        elif operation == "discover":
            self.reads += 1
        return super().call(account, operation, payload)


@unittest.skipUnless(MCP_AVAILABLE, "MCP extra is not installed")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.provider = CountingAdapter()
        self.registry = AdapterRegistry(
            Path(self.tmp.name), overrides={p: self.provider for p in PLATFORMS}
        )
        self.engine = Engine(
            Store(Path(self.tmp.name) / "mcp.db"), self.registry, clock=lambda: self.now
        )
        self.accounts = {}
        for platform in PLATFORMS:
            account = self.engine.add_account(
                AccountInput(
                    platform=platform,
                    label=platform,
                    backend_label=platform,
                    mode="simulation",
                    min_interval=0,
                )
            )
            self.engine.check_account(account["id"])
            self.accounts[platform] = account
        self.server = create_server(self.engine)

    async def call(self, tool_name, **arguments):
        result = await self.server.call_tool(tool_name, arguments)
        if isinstance(result, dict):
            return result
        if isinstance(result, tuple):
            return result[1]
        return json.loads(result[0].text)

    def task(self):
        return self.engine.create_task(
            TaskInput(
                name="MCP contract",
                account_ids=[self.accounts["telegram"]["id"]],
                policy={
                    "starts_at": iso(self.now - 60),
                    "ends_at": iso(self.now + 3600),
                    "min_interval": 0,
                    "queries": ["AI video"],
                },
            )
        )

    def approved_task(self):
        task = self.task()
        self.engine.start_task(task["id"], sending=False)
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=self.accounts["telegram"]["id"],
                    action="send_message",
                    target="@ai_group",
                    text="AI video example",
                    scheduled_at=iso(self.now),
                )
            ],
        )
        task = self.engine.prepare(task["id"])
        return self.engine.start_task(task["id"])

    async def test_catalog_and_accounts_cover_all_six_platforms(self):
        tools = {t.name: t for t in await self.server.list_tools()}
        self.assertEqual(len(tools), 69)
        self.assertIn("remove_x_local_account_daily_limits", tools)
        self.assertIn("search_x_content", tools)
        self.assertEqual(
            tools["search_x_content"].inputSchema["properties"]["lookback_hours"]["default"], 12
        )
        self.assertTrue(
            {"tiktok_dm_collect", "tiktok_dm_inbox", "tiktok_dm_messages", "tiktok_dm_enable"}
            <= tools.keys()
        )
        self.assertTrue(
            {
                "tiktok_browser_connect",
                "tiktok_browser_jobs",
                "tiktok_browser_guard",
                "tiktok_browser_begin",
                "tiktok_browser_result",
            }
            <= tools.keys()
        )
        self.assertIn("proof", tools["tiktok_browser_guard"].inputSchema["required"])
        self.assertIn("tiktok_check_interaction_input", tools)
        self.assertTrue(
            {
                "list_task_templates",
                "save_task_template",
                "create_task_from_template",
                "task_evidence",
                "task_review",
            }
            <= tools.keys()
        )
        self.assertTrue(tools["task_evidence"].annotations.readOnlyHint)
        self.assertTrue(tools["task_review"].annotations.readOnlyHint)
        self.assertTrue(
            {
                "list_business_goals",
                "business_growth_report",
                "save_business_goal",
                "import_conversion_events",
                "import_store_metrics",
                "list_reply_inbox",
            }.issubset(tools.keys())
        )
        self.assertTrue(
            {
                "list_account_pools",
                "get_account_profile",
                "import_opportunities",
                "plan_campaign",
                "create_campaign_batches",
                "edit_campaign_draft",
            }
            <= tools.keys()
        )
        self.assertTrue(tools["get_campaign"].annotations.readOnlyHint)
        self.assertFalse(tools["plan_campaign"].annotations.openWorldHint)
        self.assertTrue(tools["interaction_statistics"].annotations.readOnlyHint)
        self.assertTrue(tools["statistical_reports"].annotations.readOnlyHint)
        self.assertTrue(tools["system_status"].annotations.readOnlyHint)
        self.assertFalse(tools["start_task"].annotations.readOnlyHint)
        self.assertFalse({"approve", "set_stop", "login", "send_message"} & tools.keys())
        status = await self.call("system_status")
        self.assertEqual({p["platform"] for p in status["platforms"]}, set(PLATFORMS))
        self.assertTrue(status["global_stop"])
        for platform in PLATFORMS:
            accounts = await self.call("list_accounts", platform=platform)
            self.assertEqual([a["platform"] for a in accounts["accounts"]], [platform])
        self.assertEqual(self.provider.sends, 0)

    async def test_tiktok_preparation_tool_is_local_and_missing_parent_can_be_corrected(self):
        account_id = self.accounts["tiktok"]["id"]
        with patch.object(self.engine, "call", side_effect=AssertionError("No platform probe")):
            blocked = await self.call(
                "tiktok_check_interaction_input",
                account_id=account_id,
                action="reply",
                video_id="123",
                target_owned=True,
            )
            self.assertEqual(blocked["reason"], "TikTokParentCommentRequired")
            corrected = await self.call(
                "tiktok_check_interaction_input",
                account_id=account_id,
                action="reply",
                video_id="123",
                parent_comment_id="456",
                comment_url="https://www.tiktok.com/@target/video/123?comment_id=456",
                target_owned=True,
            )
            self.assertTrue(corrected["input_valid"])
            self.assertFalse(corrected["execution_ready"])
        self.assertEqual(self.engine.store.rows("SELECT * FROM attempts"), [])

    async def test_draft_creation_does_not_grant_approval_or_publish(self):
        task = await self.call(
            "create_task",
            name="Draft",
            account_ids=[self.accounts["x"]["id"]],
            policy={"starts_at": iso(self.now), "ends_at": iso(self.now + 3600), "min_interval": 0},
        )
        self.assertEqual(task["approvals"], [])
        self.assertEqual(self.provider.sends, 0)
        started = await self.call("start_task", task_id=task["id"])
        self.assertEqual(started["approvals"], [])
        self.assertEqual(started["execution_result"]["processed"], [])
        self.assertEqual(self.provider.sends, 0)

    async def test_reusable_template_and_task_evidence_keep_approvals_task_specific(self):
        source = self.approved_task()
        template = await self.call(
            "save_task_template", source_task_id=source["id"], name="Reusable"
        )
        fresh = await self.call(
            "create_task_from_template",
            template_id=template["id"],
            task={
                "name": "New",
                "account_ids": [self.accounts["telegram"]["id"]],
                "starts_at": iso(self.now),
                "ends_at": iso(self.now + 3600),
            },
        )
        self.assertEqual(fresh["approvals"], [])
        self.assertEqual(fresh["items"], [])
        self.assertEqual(
            (await self.call("task_evidence", task_id=source["id"]))["task"]["id"], source["id"]
        )
        self.assertIn(source["id"], (await self.call("task_review", task_id=source["id"]))["html"])
        self.assertEqual(self.provider.sends, 0)
        self.assertEqual(fresh["state"], "draft")
        started = await self.call("start_task", task_id=fresh["id"])
        self.assertEqual(started["execution_result"]["processed"], [])
        self.assertEqual(self.provider.sends, 0)

    async def test_discovery_is_read_only_without_approval(self):
        task = self.task()
        result = await self.call(
            "discover_candidates", task_id=task["id"], account_id=self.accounts["telegram"]["id"]
        )
        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(self.provider.reads, 1)
        self.assertEqual(self.provider.sends, 0)

    async def test_statistics_read_all_live_tasks_without_executing_platform_actions(self):
        result = await self.call("interaction_statistics", all_dates=True)
        self.assertEqual(result["totals"]["published_success"], 0)
        self.assertIsNone(result["totals"]["received_replies"])
        self.assertEqual(self.provider.sends, 0)
        self.assertEqual(self.provider.reads, 0)

    async def test_approved_execution_honors_stop_and_attempts_once(self):
        task = self.approved_task()
        self.assertEqual(
            (await self.call("start_task", task_id=task["id"]))["execution_result"]["processed"], []
        )
        self.assertEqual(self.provider.sends, 0)
        self.engine.set_stop(False)
        result = await self.call("start_task", task_id=task["id"])
        self.assertTrue(result["execution_result"]["already_started"])
        self.assertEqual(result["execution_result"]["processed"], [])
        self.assertEqual(self.provider.sends, 0)
        # A repeated Start is not a scheduler tick. Only this isolated fake
        # worker consumes the already-authorized task after stop is released.
        self.engine.run_cycle()
        self.assertEqual(self.provider.sends, 1)
        with self.assertRaises(ToolError):
            await self.call("start_task", task_id=task["id"])
        self.assertEqual(self.provider.sends, 1)

    async def test_platform_errors_never_expose_private_values(self):
        with patch.object(self.engine, "account_targets", side_effect=RuntimeError("secret-token")):
            with self.assertRaises(ToolError) as error:
                await self.call("discord_list_channels", account_id="one")
        self.assertNotIn("secret-token", str(error.exception))
        self.assertIn("RuntimeError", str(error.exception))

    async def test_discord_channel_read_has_no_outbound_action(self):
        result = await self.call("discord_list_channels", account_id=self.accounts["discord"]["id"])
        self.assertEqual(len(result["targets"]), 1)
        self.assertEqual(self.provider.sends, 0)

    async def test_stdio_handshake_is_independent_of_client_directory(self):
        project = Path(__file__).resolve().parents[1]
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "auto_promotion.mcp_server",
                "--project-dir",
                str(project),
                "--db",
                str(Path(self.tmp.name) / "stdio.db"),
                "--no-env",
            ],
            env={
                "PYTHONIOENCODING": "utf-8",
                "AUTO_PROMOTION_LIVE_WRITES": "0",
                "PYTHONPATH": str(project / "src"),
                "AP_ACCOUNTS_DIR": str(Path(self.tmp.name) / "stdio-accounts"),
            },
            cwd=self.tmp.name,
        )
        async with asyncio.timeout(30):
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.serverInfo.name, "Auto Promotion")
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    self.assertEqual(len(tools), 69)
                    self.assertEqual(
                        set(tools), {tool.name for tool in await self.server.list_tools()}
                    )
                    self.assertTrue(
                        tools["tiktok_check_interaction_input"].annotations.readOnlyHint
                    )
                    status = await client.call_tool("system_status", {})
                    self.assertFalse(status.isError)
                    value = status.structuredContent or json.loads(status.content[0].text)
                    self.assertTrue(value["global_stop"])
                    result = await client.call_tool("get_task", {"task_id": "absent"})
                    self.assertTrue(result.isError)

    async def test_generated_config_has_no_credentials(self):
        config = client_config(self.tmp.name)
        settings = config["mcpServers"]["auto-promotion"]
        self.assertEqual(settings["command"], sys.executable)
        self.assertIn(str(Path(self.tmp.name).resolve()), settings["args"])
        self.assertNotIn("env", settings)
