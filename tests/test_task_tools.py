import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import ValidationError

from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.api import create_app
from auto_promotion.core import Conflict, Engine, NotFound
from auto_promotion.models import AccountInput, ExecutionSettings, ItemInput, TaskInput, canonical
from auto_promotion.store import Store
from auto_promotion.task_tool_models import TemplateSave, TemplateTask
from auto_promotion.task_tools import TaskTools


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class CountingAdapter(SimulationAdapter):
    def __init__(self):
        self.calls = []

    def call(self, account, operation, payload):
        self.calls.append(operation)
        return super().call(account, operation, payload)


class TaskToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.provider = CountingAdapter()
        self.registry = AdapterRegistry(Path(self.tmp.name), overrides={"telegram": self.provider})
        self.engine = Engine(
            Store(Path(self.tmp.name) / "tools.db"), self.registry, clock=lambda: self.now
        )
        self.account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Example",
                backend_label="example",
                mode="simulation",
                min_interval=0,
            )
        )
        self.engine.check_account(self.account["id"])
        self.tools = TaskTools(self.engine)
        self.provider.calls.clear()

    def task(self, name="Source", items=True):
        task = self.engine.create_task(
            TaskInput(
                name=name,
                account_ids=[self.account["id"]],
                policy={
                    "queries": ["AI video"],
                    "starts_at": iso(self.now - 60),
                    "ends_at": iso(self.now + 3600),
                    "min_interval": 0,
                    "target_allowlist": ["@example"],
                },
            )
        )
        if items:
            task = self.engine.approve(task["id"], "discovery_join", task["discovery_phrase"])
            task = self.engine.set_items(
                task["id"],
                [
                    ItemInput(
                        account_id=self.account["id"],
                        action="send_message",
                        target="@example",
                        text="=1+2 <script>bad()</script>",
                        scheduled_at=iso(self.now + 30),
                    )
                ],
            )
            task = self.engine.prepare(task["id"])
            task = self.engine.approve(task["id"], "outbound", task["outbound_phrase"])
        return task

    def template(self, source):
        return self.tools.save_template(TemplateSave(source_task_id=source["id"], name="Reusable"))

    def reuse(self):
        return TemplateTask(
            name="New round",
            account_ids=[self.account["id"]],
            starts_at=iso(self.now + 7200),
            ends_at=iso(self.now + 10800),
        )

    def test_reuse_creates_fresh_draft_with_current_quotas_and_no_old_authorization(self):
        source = self.task()
        template = self.template(source)
        for key in ("starts_at", "ends_at", "target_allowlist", "quota_snapshot"):
            self.assertNotIn(key, template["settings"])
        self.engine.configure_execution(ExecutionSettings(global_limit={"daily": 7, "hourly": 2}))
        fresh = self.tools.create_from_template(template["id"], self.reuse())
        self.assertNotEqual(fresh["id"], source["id"])
        self.assertEqual((fresh["state"], fresh["phase"]), ("draft", "discovery"))
        self.assertEqual(fresh["approvals"], [])
        self.assertEqual(fresh["items"], [])
        self.assertEqual(fresh["candidates"], [])
        self.assertIsNone(fresh["manifest"])
        self.assertEqual(fresh["policy"]["target_allowlist"], [])
        self.assertEqual(fresh["policy"]["queries"], ["AI video"])
        self.assertEqual(fresh["policy"]["quota_snapshot"]["execution"]["global_limit"]["daily"], 7)
        self.assertEqual(self.engine.task(source["id"])["approvals"], source["approvals"])
        self.assertEqual(
            self.engine.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )
        self.assertEqual(self.provider.calls, [])
        with self.assertRaises(Conflict):
            self.engine.prepare(fresh["id"])

    def test_reuse_captures_new_accounts_and_explicit_new_targets(self):
        source = self.task()
        template = self.template(source)
        account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Other",
                backend_label="other",
                mode="simulation",
                min_interval=0,
            )
        )
        values = self.reuse().model_copy(
            update={"account_ids": [account["id"]], "target_allowlist": ["@new_target"]}
        )
        fresh = self.tools.create_from_template(template["id"], values)
        self.assertEqual(fresh["account_ids"], [account["id"]])
        self.assertEqual(fresh["policy"]["target_allowlist"], ["@new_target"])
        self.assertEqual(fresh["account_snapshot"][0]["label"], "Other")
        self.assertEqual(self.provider.calls, [])

    def test_template_modes_platforms_validation_and_restart(self):
        template = self.template(self.task(items=False))
        live = self.engine.add_account(
            AccountInput(platform="telegram", label="Live", backend_label="live")
        )
        wrong_platform = self.engine.add_account(
            AccountInput(
                platform="x", label="X", backend_label="x", mode="simulation", min_interval=0
            )
        )
        for account in (live, wrong_platform):
            with self.assertRaises(Conflict):
                self.tools.create_from_template(
                    template["id"], self.reuse().model_copy(update={"account_ids": [account["id"]]})
                )
        with self.assertRaises(ValidationError):
            TemplateTask(**self.reuse().model_dump(), approvals=[{"stage": "outbound"}])
        with self.assertRaises(ValidationError):
            TemplateTask(
                name="Bad",
                account_ids=[self.account["id"]],
                starts_at="2026-10-14T10:00:00",
                ends_at=iso(self.now),
            )
        with self.assertRaises(ValidationError):
            self.tools.create_from_template(
                template["id"], self.reuse().model_copy(update={"ends_at": iso(self.now)})
            )
        restarted = TaskTools(
            Engine(Store(self.engine.store.path), self.registry, clock=lambda: self.now)
        )
        self.assertEqual(restarted.templates()[0]["id"], template["id"])
        self.assertEqual(self.provider.calls, [])

    def test_export_scopes_all_records_and_omits_private_fields(self):
        source = self.task()
        other = self.task("Other private task")
        with self.engine.store.connect(write=True) as db:
            for index in range(180):
                Store.audit(
                    db,
                    "item.skip",
                    {
                        "item_id": source["items"][0]["id"],
                        "reason": "account_cooldown",
                        "token": "PRIVATE_TOKEN",
                        "cookie": "PRIVATE_COOKIE",
                        "account_id": {"secret": "PRIVATE_NESTED"},
                    },
                    source["id"],
                    self.now + index,
                )
            Store.audit(db, "private", {"reason": "OTHER_TASK_SECRET"}, other["id"], self.now)
            db.execute(
                "UPDATE items SET status='unknown',result=? WHERE task_id=?",
                (
                    canonical(
                        {
                            "verified": False,
                            "reason": "transport_outcome_unknown",
                            "session": "PRIVATE_SESSION",
                            "diagnostic_traceback": [
                                {"file": "C:/private/PRIVATE_PATH", "function": "send", "line": 12},
                                {"file": "core.py", "function": "run", "line": 50},
                            ],
                        }
                    ),
                    source["id"],
                ),
            )
        before = self.engine.task(source["id"])
        evidence = self.tools.evidence(source["id"])
        encoded = json.dumps(evidence)
        for secret in (
            "PRIVATE_TOKEN",
            "PRIVATE_COOKIE",
            "PRIVATE_NESTED",
            "PRIVATE_SESSION",
            "PRIVATE_PATH",
            "OTHER_TASK_SECRET",
            "Other private task",
        ):
            self.assertNotIn(secret, encoded)
        self.assertGreater(len(evidence["audit"]), 150)
        self.assertEqual(evidence["summary"]["statuses"], {"unknown": 1})
        self.assertEqual(evidence["summary"]["verified_publications"], 0)
        self.assertEqual(
            evidence["items"][0]["result"]["diagnostic_traceback"],
            [{"file": "core.py", "function": "run", "line": 50}],
        )
        self.assertEqual(self.engine.task(source["id"]), before)
        self.assertEqual(self.provider.calls, [])

    def test_review_and_zip_escape_content_and_include_verifiable_checksums(self):
        task = self.task()
        review = self.tools.review(task["id"])
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", review)
        self.assertNotIn("<script>", review)
        self.assertIn("Content-Security-Policy", review)
        self.assertIn(task["manifest_hash"], review)
        with zipfile.ZipFile(io.BytesIO(self.tools.export(task["id"]))) as archive:
            self.assertIn("audit.jsonl", archive.namelist())
            self.assertIn("candidates.json", archive.namelist())
            for line in archive.read("checksums.sha256").decode().splitlines():
                checksum, name = line.split("  ", 1)
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), checksum)
            self.assertIn("'=1+2", archive.read("results.csv").decode("utf-8-sig"))
        self.assertEqual(self.provider.calls, [])

    def test_empty_draft_and_invalid_export_requests(self):
        task = self.task(items=False)
        self.assertEqual(self.tools.evidence(task["id"])["summary"]["item_count"], 0)
        self.assertIn("草稿，尚未冻结", self.tools.review(task["id"]))
        with self.assertRaises(NotFound):
            self.tools.export("missing")
        with self.assertRaises(Conflict):
            self.tools.export(task["id"], "exe")

    def test_authenticated_api_can_reuse_and_download_without_calls_or_approvals(self):
        task = self.task()
        app = create_app(
            db_path=self.engine.store.path,
            registry=self.registry,
            clock=lambda: self.now,
            token="test-key",
        )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://testserver"
            ) as client:
                for path in (
                    "/api/task-templates",
                    f"/api/tasks/{task['id']}/export",
                    f"/api/tasks/{task['id']}/evidence",
                ):
                    self.assertEqual((await client.get(path)).status_code, 401)
                headers = {"Authorization": "Bearer test-key"}
                template = await client.post(
                    "/api/task-templates",
                    headers=headers,
                    json={"source_task_id": task["id"], "name": "API"},
                )
                self.assertEqual(template.status_code, 201)
                path = f"/api/task-templates/{template.json()['id']}/tasks"
                fresh = await client.post(path, headers=headers, json=self.reuse().model_dump())
                self.assertEqual(fresh.status_code, 201)
                self.assertEqual(fresh.json()["approvals"], [])
                blocked = await client.post(
                    path,
                    headers={**headers, "Origin": "https://other.example"},
                    json=self.reuse().model_dump(),
                )
                self.assertEqual(blocked.status_code, 403)
                download = await client.get(
                    f"/api/tasks/{task['id']}/export?format=zip", headers=headers
                )
                self.assertEqual(download.status_code, 200)
                self.assertIn("attachment", download.headers["content-disposition"])
                self.assertTrue(zipfile.is_zipfile(io.BytesIO(download.content)))
                invalid = await client.get(
                    f"/api/tasks/{task['id']}/export?format=exe", headers=headers
                )
                self.assertEqual(invalid.status_code, 409)

        asyncio.run(run())
        self.assertEqual(self.provider.calls, [])

    def test_cli_exports_arbitrary_task_and_preserves_existing_output(self):
        task = self.task(items=False)
        output = Path(self.tmp.name) / "evidence.json"
        args = [
            sys.executable,
            "-m",
            "auto_promotion.cli",
            "--db",
            self.engine.store.path,
            "task-export",
            "--task",
            task["id"],
            "--format",
            "json",
            "--output",
            str(output),
        ]
        env = {
            **os.environ,
            "AUTO_PROMOTION_LIVE_WRITES": "0",
            "AUTO_PROMOTION_WORKSPACE": self.tmp.name,
        }
        first = subprocess.run(args, cwd=self.tmp.name, env=env, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["task"]["id"], task["id"])
        before = output.read_bytes()
        second = subprocess.run(args, cwd=self.tmp.name, env=env, capture_output=True, text=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(output.read_bytes(), before)
