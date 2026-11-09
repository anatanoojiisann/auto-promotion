"""Local stdio MCP server for the six bundled platform backends."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
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
from .cloud_control import CloudUnavailable
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
from .text_integrity import configure_utf8_stdio
from .tiktok import TikTokInteractionInput, TikTokManager, TikTokReadInput
from .tiktok_browser import TikTokBrowser, TikTokBrowserConnect
from .workers import Workers

Platform = Literal["telegram", "reddit", "instagram", "facebook", "x", "discord", "tiktok"]


def client_config(project_dir, python=None, *, db=None, phone_social_config=None, phone_reddit_config=None, phone_rounds=False):
    """Return a credential-free configuration for stdio MCP clients."""
    arguments = ["-m", "auto_promotion.mcp_server", "--project-dir", str(Path(project_dir).resolve())]
    for flag, path in (("--db", db), ("--phone-social-config", phone_social_config), ("--phone-reddit-config", phone_reddit_config)):
        if path is not None:
            arguments.extend((flag, str(Path(path).resolve())))
    if phone_rounds:
        arguments.append("--phone-rounds")
    return {
        "mcpServers": {
            "auto-promotion": {
                "command": str(python or sys.executable),
                "env": {"PYTHONIOENCODING": "utf-8:strict", "PYTHONUTF8": "1"},
                "args": arguments,
            }
        }
    }


PhonePlatform = Literal["telegram", "reddit", "instagram", "facebook", "x", "tiktok"]


def create_server(engine=None, *, phone_rounds_enabled=False, phone_social_config=None, phone_reddit_config=None):
    from .runtime_identity import capture_runtime

    runtime = capture_runtime("mcp")
    engine = engine or Engine(
        Store(database_path()),
        AdapterRegistry(workspace_path()),
        live_enabled=os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1",
    )
    if phone_social_config is not None:
        from .phone_social import PhoneSocial
        PhoneSocial(engine, phone_social_config)
    if phone_reddit_config is not None:
        from .phone_reddit import PhoneReddit
        PhoneReddit(engine, phone_reddit_config)
    server = FastMCP(
        "Auto Promotion",
        instructions=(
            "Manage Telegram, Reddit, Instagram, Facebook, X, Discord and TikTok through persistent tasks. "
            "When the user explicitly instructs you to start sending, replying or joining, "
            "call start_task or start_joins for the specified task without asking for approval phrases. "
            "Creating, editing, planning or previewing a task never starts execution. "
            "Tools do not enable live writes or release the global stop. "
            "Account health and tool availability do not prove publication permission."
            " Before preparing TikTok video interactions, call tiktok_check_interaction_input. "
            "It checks input only and never authorizes sending; external-video work also needs "
            "a bound desktop provider and independently verifiable results. "
            "For TikTok ID-only drafts, omit target in set_task_items so the Engine resolves "
            "and freezes account-scoped source evidence; resolved_target is a preview only."
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
    from .meta_browser import MetaBrowser

    meta_browser = MetaBrowser(engine)
    tiktok_browser = TikTokBrowser(engine)

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
        comment_id: str = "",
        comment_url: str = "",
    ) -> dict:
        """Read owned-video comments; optionally import into the shared pool; never replies."""
        return invoke(
            lambda: tiktok.read(
                account_id,
                "comments",
                TikTokReadInput(
                    video_id=video_id,
                    cursor=cursor,
                    limit=limit,
                    import_to_pool=import_to_pool,
                    comment_id=comment_id,
                    comment_url=comment_url,
                ),
            )
        )

    @server.tool(annotations=remote_read)
    def tiktok_account_insights(account_id: str) -> dict:
        """Read permitted TikTok account metrics; absent values remain unknown."""
        return invoke(lambda: tiktok.read(account_id, "insights", TikTokReadInput()))

    @server.tool(annotations=read)
    def tiktok_check_interaction_input(
        account_id: str,
        action: str,
        video_id: str = "",
        parent_comment_id: str = "",
        comment_url: str = "",
        target_owned: bool = False,
        task_id: str = "",
        opportunity_id: str = "",
    ) -> dict:
        """Resolve ID from account-scoped candidate evidence and check native video/parent binding locally before preparation; never grants send permission."""
        return invoke(
            lambda: tiktok.check_interaction(
                account_id,
                TikTokInteractionInput(
                    action=action,
                    video_id=video_id,
                    parent_comment_id=parent_comment_id,
                    comment_url=comment_url,
                    target_owned=target_owned,
                    task_id=task_id,
                    opportunity_id=opportunity_id,
                ),
            )
        )

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
        except CloudUnavailable as exc:
            from .execution_diagnostics import admission_failure_reason

            raise ToolError(admission_failure_reason(exc)) from None
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

    @server.tool(annotations=local)
    def meta_browser_status() -> dict:
        """Synchronize existing Meta workflow states without starting any task."""
        return invoke(meta_browser.status)

    @server.tool(annotations=local)
    def acknowledge_unrelated_x_history(task_id: str, account_id: str, source: str) -> dict:
        """Only on an explicit user instruction: acknowledge unrelated old X unknowns
        for this exact frozen local task and account. Never alter old outcomes, permit
        old targets, grant account ownership, start a task, or send a message.
        """
        from .x_history_ack import record_ack

        return invoke(lambda: record_ack(engine, task_id, account_id, source=source))

    @server.tool(annotations=remote_read)
    def authorize_local_channel(
        task_id: str, account_id: str, coordinate_once: bool, source: str
    ) -> dict:
        """Only on an explicit human request: coordinate this account/task with cloud
        control once to establish local ownership. Does not start a task, operate
        a browser, create an outbound attempt or submit. Preserve global offline
        scopes. source must identify the human instruction; coordinate_once=true
        explicitly permits this narrow control handshake. No credentials created.
        """
        from .cloud_control import CloudUnavailable
        from .local_authorization import authorize_local_channel as authorize
        from .task_channels import safe_channel_code

        try:
            return authorize(
                engine, task_id, account_id, coordinate_once=coordinate_once, source=source
            )
        except CloudUnavailable as exc:
            raise ToolError(safe_channel_code(str(exc))) from None
        except ValueError:
            raise ToolError("ExplicitLocalCoordinationRequired") from None
        except sqlite3.OperationalError as exc:
            try:
                locked = Store.locked(exc)
            except (AttributeError, TypeError, ValueError):
                locked = False
            if locked:
                raise ToolError("LocalDatabaseUnavailable") from None
            raise ToolError("LocalAuthorizationUnavailable") from None
        except Exception:
            raise ToolError("LocalAuthorizationUnavailable") from None

    @server.tool(annotations=local)
    def meta_browser_jobs() -> dict:
        """Read due Facebook/IG desktop jobs and unknown receipts; never submit."""
        return invoke(meta_browser.pending)

    @server.tool(annotations=local)
    def meta_browser_observe(account_id: str, proof: dict) -> dict:
        """Save a fresh identity observation made through the account's existing browser."""
        return invoke(lambda: meta_browser.observe(account_id, proof))

    @server.tool(annotations=local)
    def meta_browser_begin(item_id: str, proof: dict) -> dict:
        """Claim one due item of an explicitly started task before operating its browser."""
        return invoke(lambda: meta_browser.begin(item_id, proof))

    @server.tool(annotations=local)
    def meta_browser_guard(item_id: str, owner: str) -> dict:
        """Immediately before clicking Submit, recheck all stops and use its one submission permit."""
        return invoke(lambda: meta_browser.guard(item_id, owner))

    @server.tool(annotations=local)
    def meta_browser_result(item_id: str, receipt: dict, owner: str | None = None) -> dict:
        """Save independent browser receipt evidence; an unknown item can never be resent."""
        return invoke(lambda: meta_browser.result(item_id, receipt, owner))

    @server.tool(annotations=local)
    def meta_browser_feedback(item_id: str, evidence: dict) -> dict:
        """Save observed direct replies with honest thread coverage; never send a reply."""
        return invoke(lambda: meta_browser.feedback(item_id, evidence))

    @server.tool(annotations=local)
    def tiktok_browser_status() -> dict:
        """Read enrolled TikTok browser connections; never start or submit."""
        return invoke(tiktok_browser.status)

    @server.tool(annotations=local)
    def tiktok_browser_connect(connection: dict) -> dict:
        """Enroll TikTok browser metadata without logging in or enabling writes."""
        return invoke(
            lambda: tiktok_browser.connect(TikTokBrowserConnect.model_validate(connection))
        )

    @server.tool(annotations=local)
    def tiktok_browser_jobs() -> dict:
        """Read due TikTok desktop jobs and unresolved receipts without submitting."""
        return invoke(tiktok_browser.pending)

    @server.tool(annotations=local)
    def tiktok_browser_observe(account_id: str, proof: dict) -> dict:
        """Save current TikTok authenticated-menu and profile evidence, or a blocker."""
        return invoke(lambda: tiktok_browser.observe(account_id, proof))

    @server.tool(annotations=local)
    def tiktok_browser_begin(item_id: str, proof: dict) -> dict:
        """Claim one explicitly started TikTok comment/reply with current UI evidence."""
        return invoke(lambda: tiktok_browser.begin(item_id, proof))

    @server.tool(annotations=local)
    def tiktok_browser_guard(item_id: str, owner: str, proof: dict) -> dict:
        """Immediately before clicking, recheck fresh TikTok UI proof and consume one permit."""
        return invoke(lambda: tiktok_browser.guard(item_id, owner, proof))

    @server.tool(annotations=local)
    def tiktok_browser_result(item_id: str, receipt: dict, owner: str | None = None) -> dict:
        """Record an independently observed TikTok comment receipt; never resend."""
        return invoke(lambda: tiktok_browser.result(item_id, receipt, owner))

    @server.tool(annotations=local)
    def tiktok_browser_feedback(item_id: str, evidence: dict) -> dict:
        """Record observed direct replies and coverage without sending follow-ups."""
        return invoke(lambda: tiktok_browser.feedback(item_id, evidence))

    @server.tool(annotations=local)
    def tiktok_dm_collect(account_id: str, snapshot: dict) -> dict:
        """Save observed private inbox data locally; never start, accept a request, or send."""
        return invoke(lambda: tiktok_browser.messages.collect(account_id, snapshot))

    @server.tool(annotations=read)
    def tiktok_dm_inbox(account_id: str) -> dict:
        """Read this account's collected private conversations, outside the shared pool."""
        return invoke(lambda: tiktok_browser.messages.inbox(account_id))

    @server.tool(annotations=read)
    def tiktok_dm_messages(account_id: str, conversation_id: str, limit: int = 100) -> dict:
        """Read at most 200 locally collected private messages for a verified conversation."""
        return invoke(lambda: tiktok_browser.messages.messages(account_id, conversation_id, limit))

    @server.tool(annotations=local)
    def tiktok_dm_enable(account_id: str, enabled: bool = True) -> dict:
        """Configure DM task capability after inbox observation; never enable live writer or send."""
        return invoke(lambda: tiktok_browser.messages.enable(account_id, enabled))

    @server.tool(annotations=read)
    def system_status() -> dict:
        """Read MCP/platform readiness and execution switches without contacting platforms."""
        from .queue_store import JobQueue

        return invoke(
            lambda: {
                "mcp_server": "auto-promotion",
                "transport": "stdio",
                "runtime": runtime,
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

    @server.tool(annotations=local)
    def remove_x_local_account_daily_limits() -> dict:
        """Remove X account-daily limits for local execution on explicit instruction.

        All hourly, task, platform and global limits remain. Does not change cloud settings.
        """
        from .account_daily_local import enable

        return invoke(lambda: enable(engine))

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

    @server.tool(annotations=read)
    def engagement_statistics(limit: int = 50, offset: int = 0) -> dict:
        """Read saved views, likes and replies for our verified publications, with coverage and changes."""
        from .engagement import Engagement

        return invoke(lambda: Engagement(engine).snapshot(limit=limit, offset=offset))

    @server.tool(annotations=read)
    def engagement_history(item_id: str, limit: int = 100) -> dict:
        """Read the saved metric samples for one publication; never opens or sends to a platform."""
        from .engagement import Engagement

        return invoke(lambda: {"samples": Engagement(engine).history(item_id, limit=limit)})

    @server.tool(annotations=local)
    def create_task(name: str, account_ids: list[str], policy: dict) -> dict:
        """Create a draft without execution; preserve the user's explicit channel choice.

        Use policy.execution_channel=cloud/local for an explicit instruction and record
        its reference in execution_channel_source. Never silently leave that instruction
        as inherit or switch channels after an error. Cloud authorization and the actual
        browser execution node are separate. Creation never starts a task.
        """
        return invoke(
            lambda: engine.create_task(TaskInput(name=name, account_ids=account_ids, policy=policy))
        )

    @server.tool(annotations=remote_read)
    def search_x_content(
        account_id: str,
        query: str,
        lookback_hours: float = 12,
        timeout_seconds: int = 30,
        max_results: int = 100,
    ) -> dict:
        """Read one bounded X search page through the unified reader; never interact.

        Returns exact source times and observations pending context/priority review.
        Use this entry for live X discovery instead of temporary browser scripts.
        Rate limits and verification blocks persist; incomplete coverage stays explicit.
        """
        return invoke(
            lambda: engine.search_x_content(
                account_id,
                query,
                lookback_hours=lookback_hours,
                timeout_seconds=timeout_seconds,
                max_results=max_results,
            )
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
    def save_interaction_content(content: dict) -> dict:
        """Save immutable target-bound content without an account, device, model call or send."""
        from .content_queue import ContentDraft, ContentQueue

        return invoke(lambda: ContentQueue(engine).put(ContentDraft(**content)))

    @server.tool(annotations=read)
    def list_interaction_content(limit: int = 100) -> dict:
        """Read persisted content and its draft-task binding; never start execution."""
        from .content_queue import ContentQueue

        if not 1 <= limit <= 500:
            raise ToolError("ContentLimitOutOfRange")
        return invoke(lambda: {"items": ContentQueue(engine).list(limit)})

    @server.tool(annotations=local)
    def materialize_content_batch(batch: dict) -> dict:
        """Bind saved content to a phone draft idempotently; explicit start_task is still required."""
        from .content_queue import ContentBatch, ContentQueue

        return invoke(lambda: ContentQueue(engine).materialize(ContentBatch(**batch)))

    @server.tool(annotations=local)
    async def set_task_items(task_id: str, items: list[dict]) -> dict:
        """Save drafts only. TikTok desktop comment/reply may omit target: resolve tiktok_video_id from this task/account candidate evidence; no send permission."""
        return await asyncio.to_thread(invoke, lambda: engine.set_items(task_id, [ItemInput(**i) for i in items]))

    @server.tool(annotations=local)
    def authorize_deleted_x_reply_replacement(
        old_item_id: str, new_item_id: str, source: str, deletion_evidence: dict
    ) -> dict:
        """Only on explicit user instruction: authorize one corrected reply after verified deletion of the exact garbled old X reply. Preserve old attempts/outcomes; never starts a task or sends."""
        from .deleted_reply_replacement import authorize_replacement

        return invoke(
            lambda: authorize_replacement(
                engine, old_item_id, new_item_id, source=source, deletion_evidence=deletion_evidence
            )
        )

    @server.tool(annotations=local)
    def continue_unsubmitted_x_reply_replacement(
        previous_item_id: str, new_item_id: str, source: str
    ) -> dict:
        """Continue an explicitly approved deleted-reply correction only with an independently validated native final receipt proving no submission. Never reset attempts, restart unknowns or send."""
        from .deleted_reply_replacement import authorize_no_submit_retry

        return invoke(
            lambda: authorize_no_submit_retry(engine, previous_item_id, new_item_id, source=source)
        )

    @server.tool(annotations=local)
    async def prepare_task(task_id: str, phone_comment_target: dict[str, str] | None = None) -> dict:
        """Freeze the manifest. For a single Instagram phone comment, optionally verify the original post and draft with target_id, target_author and target_text; never submits."""
        def prepare():
            if phone_comment_target is None:
                return engine.prepare(task_id)
            task = engine.task(task_id)
            if (task["state"] != "draft" or task["policy"].get("execution_method") != "phone"
                    or len(task["items"]) != 1 or set(phone_comment_target) != {"target_id", "target_author", "target_text"}):
                raise Conflict("PhoneCommentPreparationScopeMismatch")
            item = task["items"][0]
            payload = item["payload"]
            if payload["platform"] != "instagram" or payload["action"] != "comment" or item["status"] != "not_attempted":
                raise Conflict("PhoneCommentPreparationScopeMismatch")
            service = getattr(engine.adapters, "phone_social", None)
            if service is None:
                raise Conflict("PhoneSocialMCPBindingRequired")
            task = engine.prepare(task_id)
            snapshot = service.prepare(item["id"], phone_comment_target, discard=True)
            raw = snapshot.get("result") or {}
            proof = raw.get("social") or {}
            task["phone_preparation"] = {
                "execution_id": snapshot.get("id"), "status": snapshot.get("status"),
                "code": raw.get("code"), "simulated": raw.get("simulated"),
                **{k: proof.get(k) for k in ("editor_verified", "discarded", "receipt_capable", "submission_started")},
            }
            return task
        return await asyncio.to_thread(invoke, prepare)

    @server.tool(annotations=outbound)
    def start_joins(task_id: str) -> dict:
        """Start qualified Telegram joins after an explicit user instruction; honor all stops."""

        def run():
            engine.start_task(task_id, source="codex", sending=False)
            return engine.run_joins(task_id)

        return invoke(run)

    @server.tool(annotations=outbound)
    async def start_task(task_id: str) -> dict:
        """On an explicit user instruction, start sending/interacting with this task now.

        Automatically load campaign items and freeze the exact manifest. No approval
        phrases are needed. Honor global stops, account cooldowns, quotas and schedules.
        Never call merely because the user asked to draft, analyze, plan or preview.
        """
        return await asyncio.to_thread(invoke, lambda: engine.start_and_run(task_id, source="codex"))

    @server.tool(annotations=remote_read)
    async def verify_task_item(item_id: str) -> dict:
        """Read platform receipts for unknown or scheduled results; never resend an item."""
        return await asyncio.to_thread(invoke, engine.reconcile, item_id)

    @server.tool(annotations=remote_read)
    def verify_telegram_membership(candidate_id: str) -> dict:
        """Read-only check of an attempted Telegram join or existing member's current permissions."""
        return invoke(engine.reconcile_join, candidate_id)

    @server.tool(annotations=local)
    def pause_task(task_id: str) -> dict:
        """Pause a local task; platform-accepted schedules require separate reconciliation."""
        return invoke(engine.change_state, task_id, "paused")

    if phone_rounds_enabled:
        from .phone_readiness import readiness as phone_settings
        from .phone_round_readiness import preflight as phone_preflight
        from .phone_rounds import PhoneRounds

        rounds = PhoneRounds(engine)

        @server.tool(annotations=remote_read)
        def phone_platform_status(platform: PhonePlatform) -> dict:
            """Read one phone channel's bindings, Fleet capabilities and stops."""
            return invoke(lambda: phone_settings(engine, platform=platform))

        @server.tool(annotations=local)
        def create_phone_round(task_ids: list[str], request_key: str) -> dict:
            """Group one to six distinct frozen phone tasks. Each platform may be selected independently."""
            return invoke(rounds.create, task_ids, request_key)

        @server.tool(annotations=read)
        def get_phone_round(round_id: str) -> dict:
            """Read original per-platform outcomes, timing and verified receipt counts."""
            return invoke(rounds.get, round_id)

        @server.tool(annotations=remote_read)
        def check_phone_round(round_id: str, platform: PhonePlatform | None = None) -> dict:
            """Check the selected platform or round without starting an interaction."""
            return invoke(lambda: phone_preflight(rounds, round_id, platforms={platform} if platform else None))

        @server.tool(annotations=outbound)
        def start_phone_round(round_id: str, platform: PhonePlatform | None = None) -> dict:
            """Only on an explicit instruction, start all selected originals or one specified platform."""
            return invoke(rounds.start, round_id, platform)

        @server.tool(annotations=local)
        def stop_phone_round(round_id: str, platform: PhonePlatform | None = None) -> dict:
            """Fence future submissions for one platform or the whole round. Keep original uncertain results."""
            return invoke(rounds.stop_platform, round_id, platform) if platform else invoke(rounds.stop, round_id)

    return server


def main():
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Auto Promotion MCP (stdio)")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path)
    parser.add_argument("--print-config", action="store_true")
    parser.add_argument("--phone-rounds", action="store_true")
    parser.add_argument("--phone-social-config", type=Path)
    parser.add_argument("--phone-reddit-config", type=Path)
    parser.add_argument("--no-env", action="store_true", help="Do not load the project .env")
    args = parser.parse_args()
    project = args.project_dir.resolve()
    if not project.is_dir():
        parser.error("Project directory does not exist")
    if args.print_config:
        print(json.dumps(client_config(project, db=args.db, phone_social_config=args.phone_social_config,
            phone_reddit_config=args.phone_reddit_config, phone_rounds=args.phone_rounds), ensure_ascii=False, indent=2))
        return
    os.chdir(project)
    if not args.no_env:
        load_environment(project / ".env")
    if args.db:
        os.environ["AP_DB"] = str(args.db.resolve())
    if not args.no_env:
        load_environment(database_path().parent / "cloud-node.env")
    from .phone_readiness import load_bindings
    social, reddit = load_bindings(args.phone_social_config, args.phone_reddit_config, parser)
    create_server(phone_rounds_enabled=args.phone_rounds, phone_social_config=social,
                  phone_reddit_config=reddit).run(transport="stdio")


if __name__ == "__main__":
    main()
