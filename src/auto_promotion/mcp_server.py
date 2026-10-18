"""Local stdio MCP server for the six bundled platform backends."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from .adapters import AdapterRegistry
from .campaigns import Campaigns
from .config import database_path, load_environment, workspace_path
from .core import Conflict, Engine
from .growth import Growth
from .growth_models import ConversionImport, GrowthGoal, StoreMetricImport
from .models import (
    AccountProfile,
    AssignmentUpdate,
    CampaignInput,
    CandidateInput,
    ItemInput,
    OpportunityInput,
    PoolInput,
    TaskInput,
)
from .pools import Pools
from .statistics import ZONE, Statistics
from .store import Store
from .task_tool_models import TemplateSave, TemplateTask
from .task_tools import TaskTools
from .tiktok import TikTokManager, TikTokReadInput
from .workers import Workers

Platform = Literal["telegram", "reddit", "instagram", "x", "discord", "tiktok"]


def client_config(project_dir, python=None):
    """Return a credential-free configuration for stdio MCP clients."""
    return {
        "mcpServers": {
            "auto-promotion": {
                "command": str(python or sys.executable),
                "args": [
                    "-m",
                    "auto_promotion.mcp_server",
                    "--project-dir",
                    str(Path(project_dir).resolve()),
                ],
            }
        }
    }


def create_server(engine=None):
    engine = engine or Engine(
        Store(database_path()),
        AdapterRegistry(workspace_path()),
        live_enabled=os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1",
    )
    server = FastMCP(
        "Auto Promotion",
        instructions=(
            "Manage Telegram, Reddit, Instagram, X, Discord and TikTok through persistent tasks. "
            "When the user explicitly instructs you to start sending, replying or joining, "
            "call start_task or start_joins for the specified task without asking for approval phrases. "
            "Creating, editing, planning or previewing a task never starts execution. "
            "Tools do not enable live writes or release the global stop. "
            "Account health and tool availability do not prove publication permission."
        ),
        log_level="WARNING",
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    local = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    remote_read = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    outbound = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    pools, campaigns = Pools(engine), Campaigns(engine)
    growth = Growth(engine)
    task_tools = TaskTools(engine)
    tiktok = TikTokManager(engine)

    @server.tool(annotations=read)
    def tiktok_configuration_status() -> dict:
        """Read TikTok application setup without secrets; connect accounts in the local workbench."""
        return invoke(tiktok.status)

    @server.tool(annotations=remote_read)
    def tiktok_list_videos(account_id: str, cursor: int = 0, limit: int = 20) -> dict:
        """Read one page of owned TikTok posts; never publishes."""
        return invoke(
            lambda: tiktok.read(account_id, "videos", TikTokReadInput(cursor=cursor, limit=limit))
        )

    @server.tool(annotations=remote_read)
    def tiktok_read_comments(
        account_id: str,
        video_id: str,
        cursor: int = 0,
        limit: int = 20,
        import_to_pool: bool = False,
    ) -> dict:
        """Read owned-video comments; optionally import into the shared pool; never replies."""
        return invoke(
            lambda: tiktok.read(
                account_id,
                "comments",
                TikTokReadInput(
                    video_id=video_id, cursor=cursor, limit=limit, import_to_pool=import_to_pool
                ),
            )
        )

    @server.tool(annotations=remote_read)
    def tiktok_account_insights(account_id: str) -> dict:
        """Read permitted TikTok account metrics; absent values remain unknown."""
        return invoke(lambda: tiktok.read(account_id, "insights", TikTokReadInput()))

    @server.tool(annotations=read)
    def list_task_templates() -> dict:
        """List reusable discovery and pacing settings; templates never start execution."""
        return invoke(lambda: {"templates": task_tools.templates()})

    @server.tool(annotations=local)
    def save_task_template(source_task_id: str, name: str, description: str = "") -> dict:
        """Save a task's reusable settings, excluding dates, targets, items and approvals."""
        return invoke(
            lambda: task_tools.save_template(
                TemplateSave(source_task_id=source_task_id, name=name, description=description)
            )
        )

    @server.tool(annotations=local)
    def create_task_from_template(template_id: str, task: dict) -> dict:
        """Create a fresh draft with explicit accounts and dates; never starts execution."""
        return invoke(
            lambda: task_tools.create_from_template(template_id, TemplateTask.model_validate(task))
        )

    @server.tool(annotations=read)
    def task_evidence(task_id: str) -> dict:
        """Read all task-scoped results, historical approvals and sanitized audit evidence in one snapshot."""
        return invoke(task_tools.evidence, task_id)

    @server.tool(annotations=read)
    def task_review(task_id: str) -> dict:
        """Read an HTML review of exact task content and timing; never starts execution."""
        return invoke(lambda: {"task_id": task_id, "html": task_tools.review(task_id)})

    @server.tool(annotations=read)
    def list_business_goals() -> dict:
        """Read business targets and measurement windows; never publishes."""
        return invoke(lambda: {"goals": growth.goals()})

    @server.tool(annotations=read)
    def business_growth_report(goal_id: str) -> dict:
        """Read imported download cohorts, mature trials, charges and refunds with data gaps."""
        return invoke(growth.report, goal_id)

    @server.tool(annotations=local)
    def save_business_goal(goal: dict, goal_id: str | None = None) -> dict:
        """Save a local business target; does not configure accounts or start tasks."""
        return invoke(lambda: growth.save_goal(GrowthGoal.model_validate(goal), goal_id))

    @server.tool(annotations=local)
    def import_conversion_events(goal_id: str, items: list[dict]) -> dict:
        """Import evidenced anonymous app events; deduplicates and never sends messages."""
        return invoke(lambda: growth.import_events(goal_id, ConversionImport(items=items).items))

    @server.tool(annotations=local)
    def import_store_metrics(goal_id: str, items: list[dict]) -> dict:
        """Import channel/day App Store reports; absent metrics remain unknown."""
        return invoke(lambda: growth.import_metrics(goal_id, StoreMetricImport(items=items).items))

    @server.tool(annotations=local)
    def list_reply_inbox(offset: int = 0, limit: int = 100) -> dict:
        """Synchronize read-only feedback into a local review queue; never replies."""
        return invoke(growth.inbox, offset, limit)

    def invoke(function, *args):
        try:
            return function(*args)
        except Conflict as exc:
            raise ToolError(str(exc)) from None
        except ValidationError:
            raise ToolError("Invalid task, candidate or item input") from None
        except Exception as exc:
            # Provider exceptions can contain login data or configuration values.
            code = getattr(exc, "safe_code", type(exc).__name__)
            if not isinstance(code, str) or not code.isidentifier() or len(code) > 80:
                code = "BackendError"
            raise ToolError(code) from None

    @server.tool(annotations=read)
    def system_status() -> dict:
        """Read MCP/platform readiness and execution switches without contacting platforms."""
        from .queue_store import JobQueue

        return invoke(
            lambda: {
                "mcp_server": "auto-promotion",
                "transport": "stdio",
                "platforms": engine.adapters.services(),
                "global_stop": engine.store.one("SELECT value FROM meta WHERE key='global_stop'")[
                    "value"
                ]
                != "0",
                "live_writes_enabled": engine.live_enabled,
                "execution_control": "explicit_chat_instruction_or_client_start",
                "execution_pools": JobQueue(engine).pools(),
            }
        )

    @server.tool(annotations=read)
    def list_accounts(platform: Platform | None = None) -> dict:
        """List configured account labels and status; no login credentials are returned."""
        return invoke(
            lambda: {
                "accounts": [
                    a for a in engine.accounts() if platform is None or a["platform"] == platform
                ]
            }
        )

    @server.tool(annotations=read)
    def list_account_pools() -> dict:
        """Read reusable account pools and their quotas, without credentials."""
        return invoke(lambda: {"pools": pools.list()})

    @server.tool(annotations=read)
    def get_account_profile(account_id: str) -> dict:
        """Read the versioned operational persona used for campaign drafts."""
        return invoke(pools.profile, account_id)

    @server.tool(annotations=local)
    def save_account_profile(account_id: str, profile: dict) -> dict:
        """Save a new persona version for future drafts; never changes frozen text."""
        return invoke(lambda: pools.set_profile(account_id, AccountProfile.model_validate(profile)))

    @server.tool(annotations=local)
    def save_account_pool(pool: dict, pool_id: str | None = None) -> dict:
        """Create or edit a local account pool; does not authorize any interaction."""
        return invoke(lambda: pools.save(PoolInput.model_validate(pool), pool_id))

    @server.tool(annotations=read)
    def list_opportunities(offset: int = 0, limit: int = 100) -> dict:
        """Page through the shared content pool, with at most 500 rows per request."""
        return invoke(lambda: campaigns.content(offset=offset, limit=limit))

    @server.tool(annotations=local)
    def import_opportunities(items: list[dict]) -> dict:
        """Import and deduplicate at most 5000 content records without contacting platforms."""
        if not 1 <= len(items) <= 5000:
            raise ToolError("Import requires 1–5000 records")
        return invoke(
            lambda: campaigns.import_content([OpportunityInput.model_validate(i) for i in items])
        )

    @server.tool(annotations=remote_read)
    def collect_telegram_content(account_id: str, target: str, limit: int = 200) -> dict:
        """Read up to 1000 messages from a public Telegram group into the shared pool; never joins or replies."""
        return invoke(campaigns.collect_telegram_content, account_id, target, limit)

    @server.tool(annotations=read)
    def list_campaigns() -> dict:
        """Read campaign plans, execution-batch counts and verified outcome summaries."""
        return invoke(lambda: {"campaigns": campaigns.list()})

    @server.tool(annotations=read)
    def get_campaign(campaign_id: str, offset: int = 0, limit: int = 100) -> dict:
        """Read a campaign and paginated assignment reasons, exact drafts and schedules."""
        return invoke(lambda: campaigns.get(campaign_id, offset=offset, limit=limit))

    @server.tool(annotations=local)
    def create_campaign(campaign: dict) -> dict:
        """Create a campaign draft from a pool and explicit shared-content IDs."""
        return invoke(lambda: campaigns.create(CampaignInput.model_validate(campaign)))

    @server.tool(annotations=local)
    def plan_campaign(campaign_id: str) -> dict:
        """Preview persona matching and quota-aware scheduling; never starts execution."""
        return invoke(campaigns.plan, campaign_id)

    @server.tool(annotations=local)
    def create_campaign_batches(campaign_id: str) -> dict:
        """Create idempotent draft Tasks for a plan; execution requires an explicit start."""
        return invoke(campaigns.materialize, campaign_id)

    @server.tool(annotations=local)
    def edit_campaign_draft(assignment_id: str, text: str, title: str = "") -> dict:
        """Save a reviewed or AI-written exact draft before batches are created; never publishes."""
        return invoke(
            lambda: campaigns.edit_assignment(
                assignment_id, AssignmentUpdate(text=text, title=title)
            )
        )

    @server.tool(annotations=local)
    def populate_campaign_batch(task_id: str) -> dict:
        """Load planned content into a draft without starting execution."""
        return invoke(campaigns.populate, task_id)

    @server.tool(annotations=remote_read)
    def check_account(account_id: str) -> dict:
        """Check a registered platform identity and save its health, without publishing."""
        return invoke(engine.check_account, account_id)

    @server.tool(annotations=remote_read)
    def discord_list_channels(account_id: str) -> dict:
        """Read public channels of servers where the configured Discord bot is authorized."""
        return invoke(engine.account_targets, account_id)

    @server.tool(annotations=read)
    def list_tasks(limit: int = 20) -> dict:
        """List the most recent task summaries, with a limit from 1 to 100."""
        if not 1 <= limit <= 100:
            raise ToolError("Limit must be between 1 and 100")
        return invoke(
            lambda: {
                "tasks": [
                    {
                        **row,
                        "execution": Workers(engine.store, engine.clock).snapshot(
                            row["id"], live=row["mode"] == "live"
                        ),
                    }
                    for row in engine.store.rows(
                        "SELECT id,name,mode,state,phase,created_at,updated_at "
                        "FROM tasks ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    )
                ]
            }
        )

    @server.tool(annotations=read)
    def get_task(task_id: str) -> dict:
        """Read policy, exact outbound manifest, historical approvals, candidates and task results."""
        return invoke(engine.task, task_id)

    @server.tool(annotations=read)
    def interaction_statistics(day: str | None = None, all_dates: bool = False) -> dict:
        """Read all live tasks: verified publications, pending outcomes and reply data coverage."""
        return invoke(
            lambda: Statistics(engine.store, clock=engine.clock).snapshot(
                day=None
                if all_dates
                else day or datetime.fromtimestamp(engine.clock(), ZONE).date()
            )
        )

    @server.tool(annotations=read)
    def statistical_reports(limit: int = 30) -> dict:
        """Read saved UTC+8 3-hour, 20:00 and full-day reports, including later revisions."""
        return invoke(lambda: {"reports": Statistics(engine.store).reports(limit)})

    @server.tool(annotations=local)
    def create_task(name: str, account_ids: list[str], policy: dict) -> dict:
        """Create a draft task without starting execution. Use start_task on an explicit user instruction."""
        return invoke(
            lambda: engine.create_task(TaskInput(name=name, account_ids=account_ids, policy=policy))
        )

    @server.tool(annotations=remote_read)
    def discover_candidates(task_id: str, account_id: str) -> dict:
        """Search Telegram/Reddit/X within the task discovery policy; never join or send."""
        return invoke(engine.discover, task_id, account_id)

    @server.tool(annotations=local)
    def record_candidate(task_id: str, candidate: dict) -> dict:
        """Apply deterministic task qualification rules to a candidate."""
        return invoke(lambda: engine.candidate(task_id, CandidateInput(**candidate)))

    @server.tool(annotations=local)
    def set_task_items(task_id: str, items: list[dict]) -> dict:
        """Save draft outbound items without starting execution."""
        return invoke(lambda: engine.set_items(task_id, [ItemInput(**i) for i in items]))

    @server.tool(annotations=local)
    def prepare_task(task_id: str) -> dict:
        """Optionally freeze and preview the exact manifest without starting execution."""
        return invoke(engine.prepare, task_id)

    @server.tool(annotations=outbound)
    def start_joins(task_id: str) -> dict:
        """Start qualified Telegram joins after an explicit user instruction; honor all stops."""

        def run():
            engine.start_task(task_id, source="codex", sending=False)
            return engine.run_joins(task_id)

        return invoke(run)

    @server.tool(annotations=outbound)
    def start_task(task_id: str) -> dict:
        """On an explicit user instruction, start sending/interacting with this task now.

        Automatically load campaign items and freeze the exact manifest. No approval
        phrases are needed. Honor global stops, account cooldowns, quotas and schedules.
        Never call merely because the user asked to draft, analyze, plan or preview.
        """
        return invoke(lambda: engine.start_and_run(task_id, source="codex"))

    @server.tool(annotations=remote_read)
    def verify_task_item(item_id: str) -> dict:
        """Read platform receipts for unknown or scheduled results; never resend an item."""
        return invoke(engine.reconcile, item_id)

    @server.tool(annotations=remote_read)
    def verify_telegram_membership(candidate_id: str) -> dict:
        """Read-only check of an attempted Telegram join or existing member's current permissions."""
        return invoke(engine.reconcile_join, candidate_id)

    @server.tool(annotations=local)
    def pause_task(task_id: str) -> dict:
        """Pause a local task; platform-accepted schedules require separate reconciliation."""
        return invoke(engine.change_state, task_id, "paused")

    return server


def main():
    parser = argparse.ArgumentParser(description="Auto Promotion MCP (stdio)")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path)
    parser.add_argument("--print-config", action="store_true")
    parser.add_argument("--no-env", action="store_true", help="Do not load the project .env")
    args = parser.parse_args()
    project = args.project_dir.resolve()
    if not project.is_dir():
        parser.error("Project directory does not exist")
    if args.print_config:
        print(json.dumps(client_config(project), ensure_ascii=False, indent=2))
        return
    os.chdir(project)
    if not args.no_env:
        load_environment(project / ".env")
    if args.db:
        os.environ["AP_DB"] = str(args.db.resolve())
    if not args.no_env:
        load_environment(database_path().parent / "cloud-node.env")
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
