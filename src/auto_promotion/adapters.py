from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .config import PROJECTS
from .models import CAPABILITIES


class SimulationAdapter:
    """Explicitly synthetic external service, including a durable result reference."""

    def call(self, account, operation, payload):
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
    def __init__(self, workspace: Path, *, overrides=None, timeout=50):
        self.workspace = Path(workspace).resolve()
        self.overrides = overrides or {}
        self.timeout = timeout
        self.simulation = SimulationAdapter()

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
        root = self.workspace / PROJECTS[account["platform"]]
        python = Path(
            os.getenv(
                "AP_" + account["platform"].upper() + "_PYTHON", str(root / ".venv/bin/python")
            )
        ).resolve()
        if not python.exists():
            raise RuntimeError("backend_runtime_unavailable")
        env = os.environ.copy()
        env["AUTO_PROMOTION_WORKSPACE"] = str(self.workspace)
        env["PYTHONPATH"] = (
            str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        )
        # Fixed executable and script; no caller-supplied shell or arbitrary tool name.
        completed = subprocess.run(
            [str(python), "-m", "auto_promotion.provider_bridge"],
            input=json.dumps(
                {
                    "platform": account["platform"],
                    "account": account["backend_label"],
                    "operation": operation,
                    "payload": payload,
                }
            ),
            text=True,
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
                "project": name,
                "installed": (self.workspace / name).is_dir(),
                "transport": "MCP HTTP"
                if p == "telegram"
                else "Meta API"
                if p == "instagram"
                else "Browser + read provider",
                "capabilities": sorted(CAPABILITIES[p]),
                "live_configured": bool(
                    os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1"
                    and os.getenv("AP_" + p.upper() + "_OWNER") == "auto-promotion"
                ),
            }
            for p, name in PROJECTS.items()
        ]
