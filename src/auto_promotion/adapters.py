from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

from .backends import BACKENDS
from .config import PROJECTS
from .models import CAPABILITIES


class SimulationAdapter:
    """Explicitly synthetic external service, including a durable result reference."""

    def call(self, account, operation, payload):
        if operation == "targets" and account["platform"] == "discord":
            return {
                "targets": [
                    {
                        "id": "123456789012345678",
                        "guild_id": "234567890123456789",
                        "name": "ap-simulation",
                        "guild_name": "模拟服务器",
                        "can_attach": True,
                        "can_reply": True,
                    }
                ],
                "truncated": False,
            }
        if operation == "health":
            return {
                "healthy": True,
                "service_ready": True,
                "identity_verified": True,
                "reason": "simulation_only",
                "permissions": sorted(CAPABILITIES[account["platform"]]),
            }
        if operation == "discover":
            q = " ".join(payload["policy"]["queries"]) or "AI video"
            platform = account["platform"]
            target = {
                "telegram": "@ap_simulated_ai_group",
                "reddit": "https://www.reddit.com/r/artificial/comments/abc123/example/",
                "x": "https://x.com/example/status/1234567890",
            }.get(platform, "https://www.instagram.com/p/simulation/")
            return {
                "candidates": [
                    {
                        "target": target,
                        "title": q + " · 模拟候选",
                        "text": q,
                        "language": "en",
                        "members": 2000,
                        "activity": 20,
                        "confidence": 0.95,
                        "is_public": True,
                    }
                ],
                "source_note": "合成测试数据，未搜索外部平台",
            }
        if operation in {"join", "verify_join"}:
            return {
                "status": "joined",
                "verified": True,
                "external_reference": "simulation://membership/" + payload["candidate_id"],
            }
        if operation in {"execute", "verify"}:
            reference = "simulation://" + account["platform"] + "/" + payload["item_id"]
            if payload["payload"]["action"] in {"schedule_message", "schedule_file"}:
                return {
                    "status": "scheduled",
                    "verified": True,
                    "active": True,
                    "scheduled": True,
                    "message_ids": [payload["item_id"]],
                    "lookup_message_ids": [payload["item_id"]],
                    "external_reference": reference,
                }
            return {"status": "confirmed", "verified": True, "external_reference": reference}
        raise ValueError("unsupported_operation")


class AdapterRegistry:
    def __init__(self, workspace: Path, *, overrides=None, timeout=180):
        self.workspace = Path(workspace).resolve()
        self.overrides = overrides or {}
        self.timeout = timeout
        self.simulation = SimulationAdapter()
        self.database = None

    def commissioned(self, account):
        return account["mode"] == "simulation" or (
            os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1"
            and os.getenv("AP_" + account["platform"].upper() + "_OWNER") == "auto-promotion"
        )

    def call(self, account, operation, payload):
        if account["platform"] in self.overrides:
            return self.overrides[account["platform"]].call(account, operation, payload)
        if account["mode"] == "simulation":
            return self.simulation.call(account, operation, payload)
        legacy = os.getenv("AP_" + account["platform"].upper() + "_BACKEND", "builtin") == "legacy"
        if legacy and account["platform"] == "discord":
            raise RuntimeError("discord_legacy_backend_unsupported")
        root = self.workspace / PROJECTS[account["platform"]] if legacy else Path.cwd()
        python = Path(sys.executable)
        if legacy:
            runtime = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
            python = Path(
                os.getenv("AP_" + account["platform"].upper() + "_PYTHON", str(root / runtime))
            ).resolve()
        if not python.exists():
            raise RuntimeError("backend_runtime_unavailable")
        env = os.environ.copy()
        env["AUTO_PROMOTION_WORKSPACE"] = str(self.workspace)
        if self.database:
            env["AP_DB"] = str(self.database)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = (
            str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        )
        # Fixed executable and script; no caller-supplied shell or arbitrary tool name.
        completed = subprocess.run(
            [
                str(python),
                "-m",
                "auto_promotion.provider_bridge" if legacy else "auto_promotion.native_bridge",
            ],
            input=json.dumps(
                {
                    "platform": account["platform"],
                    "account": account["backend_label"],
                    "account_id": account["id"],
                    "database": self.database,
                    "operation": operation,
                    "payload": payload,
                }
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=root,
            env=env,
            timeout=self.timeout,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError("backend_process_failed")
        result = json.loads(completed.stdout)
        if result.get("bridge_error"):
            error = RuntimeError("backend_call_failed")
            code = result["bridge_error"]
            error.safe_code = (
                code
                if isinstance(code, str) and code.isidentifier() and len(code) < 80
                else "BackendError"
            )
            raise error
        return result

    def services(self):
        return [
            {
                "platform": p,
                "project": name
                if os.getenv("AP_" + p.upper() + "_BACKEND") == "legacy"
                else "auto_promotion.backends." + ("browser" if p in {"reddit", "x"} else p),
                "installed": (self.workspace / name).is_dir()
                if os.getenv("AP_" + p.upper() + "_BACKEND") == "legacy"
                else True,
                "transport": BACKENDS[p][0],
                "dependencies_ready": not BACKENDS[p][1] or find_spec(BACKENDS[p][1]) is not None,
                "backend": "legacy"
                if os.getenv("AP_" + p.upper() + "_BACKEND") == "legacy"
                else "builtin",
                "capabilities": sorted(CAPABILITIES[p]),
                "live_configured": bool(
                    os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1"
                    and os.getenv("AP_" + p.upper() + "_OWNER") == "auto-promotion"
                ),
            }
            for p, name in PROJECTS.items()
        ]
