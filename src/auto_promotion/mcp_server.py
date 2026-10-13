"""Local stdio MCP server for the five bundled platform backends."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from .adapters import AdapterRegistry
from .config import database_path, load_environment, workspace_path
from .core import Conflict, Engine
from .models import CandidateInput, ItemInput, TaskInput
from .store import Store

Platform = Literal["telegram", "reddit", "instagram", "x", "discord"]


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
            "Manage Telegram, Reddit, Instagram, X and Discord through approved tasks. "
            "Use the local workbench for human approvals and account login. "
            "No tool grants approval, enables live writes or releases the global stop. "
            "Account health and tool availability do not prove publication permission."
        ),
        log_level="WARNING",
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    local = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    remote_read = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    outbound = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

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

    def require_approval(task_id, stage):
        task = engine.task(task_id)
        expected = task["policy_hash"] if stage == "discovery_join" else task["manifest_hash"]
        if not expected or not any(
            a["stage"] == stage and a["hash"] == expected for a in task["approvals"]
        ):
            raise Conflict("请先在本机工作台完成该阶段的精确审批")
        if task["state"] != "active":
            raise Conflict("任务未激活，请在本机工作台核对状态")

    @server.tool(annotations=read)
    def system_status() -> dict:
        """Read MCP/platform readiness and execution switches without contacting platforms."""
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
                "human_approval": "local_workbench",
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
                "tasks": engine.store.rows(
                    "SELECT id,name,mode,state,phase,created_at,updated_at "
                    "FROM tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            }
        )

    @server.tool(annotations=read)
    def get_task(task_id: str) -> dict:
        """Read policy, exact outbound manifest, approvals, candidates and task results."""
        return invoke(engine.task, task_id)

    @server.tool(annotations=local)
    def create_task(name: str, account_ids: list[str], policy: dict) -> dict:
        """Create a draft task. The human must approve its policy in the local workbench."""
        return invoke(
            lambda: engine.create_task(TaskInput(name=name, account_ids=account_ids, policy=policy))
        )

    @server.tool(annotations=remote_read)
    def discover_candidates(task_id: str, account_id: str) -> dict:
        """Search Telegram/Reddit/X using the task's already approved discovery policy."""
        return invoke(engine.discover, task_id, account_id)

    @server.tool(annotations=local)
    def record_candidate(task_id: str, candidate: dict) -> dict:
        """Apply the approved deterministic qualification rules to a candidate."""
        return invoke(lambda: engine.candidate(task_id, CandidateInput(**candidate)))

    @server.tool(annotations=local)
    def set_task_items(task_id: str, items: list[dict]) -> dict:
        """Save draft outbound items after policy approval; this does not approve or send."""
        return invoke(lambda: engine.set_items(task_id, [ItemInput(**i) for i in items]))

    @server.tool(annotations=local)
    def prepare_task(task_id: str) -> dict:
        """Freeze and preview the exact manifest for human approval in the workbench."""
        return invoke(engine.prepare, task_id)

    @server.tool(annotations=outbound)
    def run_approved_joins(task_id: str) -> dict:
        """Join only Telegram candidates allowed by an existing human discovery approval."""

        def run():
            require_approval(task_id, "discovery_join")
            return engine.run_joins(task_id)

        return invoke(run)

    @server.tool(annotations=outbound)
    def run_approved_task(task_id: str) -> dict:
        """Execute due items from the human-approved manifest, honoring all existing stops."""

        def run():
            require_approval(task_id, "outbound")
            return engine.run_once(task_id)

        return invoke(run)

    @server.tool(annotations=remote_read)
    def verify_task_item(item_id: str) -> dict:
        """Read platform receipts for unknown or scheduled results; never resend an item."""
        return invoke(engine.reconcile, item_id)

    @server.tool(annotations=remote_read)
    def verify_telegram_membership(candidate_id: str) -> dict:
        """Reconcile an uncertain Telegram join through a read-only membership lookup."""
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
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
