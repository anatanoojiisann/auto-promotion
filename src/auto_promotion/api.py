from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from . import __version__
from .adapters import AdapterRegistry
from .backends.common import BackendError
from .campaigns import Campaigns
from .config import database_path, workspace_path
from .connections import ConnectionInput, ConnectionManager
from .core import Conflict, Engine, NotFound
from .dispatcher import AccountBusy
from .execution_diagnostics import exception_details
from .growth import Growth
from .growth_models import (
    ContentReview,
    ConversionImport,
    FeedInput,
    GrowthChannel,
    GrowthGoal,
    InboxUpdate,
    StoreMetricImport,
)
from .legacy import inventory
from .models import (
    AccountInput,
    AccountProfile,
    AccountUpdate,
    AssignmentUpdate,
    CampaignInput,
    CandidateInput,
    ContentCollectInput,
    ExecutionSettings,
    GenerateDraftsInput,
    ItemInput,
    ManualOutcome,
    OpportunityImport,
    PlanInput,
    PoolInput,
    TaskInput,
    TaskStateInput,
)
from .pools import Pools
from .statistics import ZONE, Statistics
from .store import Store, decode
from .task_tool_models import TemplateSave, TemplateTask
from .task_tools import TaskTools
from .tiktok import (
    TikTokAppInput,
    TikTokAuthorizeInput,
    TikTokCompleteInput,
    TikTokManager,
    TikTokReadInput,
    TikTokWebhookInput,
)
from .workers import WorkerRun, Workers


class Toggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class DiscoverInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str


def create_app(
    *,
    db_path=None,
    registry=None,
    clock=time.time,
    worker=False,
    report_worker=None,
    token=None,
    pooled_worker=None,
):
    workspace = workspace_path()
    store = Store(db_path or database_path())
    registry = registry or AdapterRegistry(workspace)
    engine = Engine(
        store, registry, clock=clock, live_enabled=os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1"
    )
    connections = ConnectionManager(engine)
    statistics = Statistics(store, clock=clock)
    pools, campaigns = Pools(engine), Campaigns(engine)
    growth = Growth(engine)
    task_tools = TaskTools(engine)
    tiktok = TikTokManager(engine)
    report_worker = worker if report_worker is None else report_worker
    operator_token = token or os.getenv("AP_API_TOKEN")
    if not operator_token:
        secret_path = Path(store.path).parent / "operator.key"
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        operator_token = secret_path.read_text().strip()
    session_token = secrets.token_urlsafe(32)
    stop = asyncio.Event()
    sender = None
    from .pool_runtime import PoolScheduler
    from .queue_store import JobQueue

    pooled_worker = (
        os.getenv("AP_EXECUTION_MODE", "legacy") == "pooled"
        if pooled_worker is None
        else pooled_worker
    )
    pool_scheduler = PoolScheduler(engine) if worker and pooled_worker else None

    async def work_loop():
        while not stop.is_set():
            try:
                sender.phase("executing")
                await asyncio.to_thread(pool_scheduler.tick if pool_scheduler else engine.run_cycle)
            except Exception as exc:
                with store.connect(write=True) as db:
                    Store.audit(
                        db,
                        "worker.error",
                        {"reason": "worker_cycle_failed", **exception_details(exc)},
                    )
            sender.phase("waiting")
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass

    async def report_loop(callback, kind):
        while not stop.is_set():
            try:
                await asyncio.to_thread(callback)
            except Exception:
                with store.connect(write=True) as db:
                    Store.audit(
                        db, "statistics.error", {"reason": "report_cycle_failed", "kind": kind}
                    )
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app):
        nonlocal sender
        stop.clear()
        with ExitStack() as stack:
            jobs = []
            if worker:
                if pool_scheduler:
                    pool_scheduler.enable()
                sender = stack.enter_context(WorkerRun(store, live_enabled=engine.live_enabled))
                jobs.append(asyncio.create_task(work_loop()))
            if report_worker:
                for callback, kind in (
                    (tiktok.reconcile_due, "verify"),
                    (engine.collect_statistics, "statistics"),
                    (statistics.run_due, "reports"),
                    (growth.sync_inbox, "inbox"),
                    (growth.poll_feeds, "feeds"),
                ):
                    jobs.append(asyncio.create_task(report_loop(callback, kind)))
            try:
                yield
            finally:
                connections.close()
                stop.set()
                # Wait for a submitted operation to settle before recording sender exit.
                for job in jobs:
                    await job
                if pool_scheduler:
                    await asyncio.to_thread(pool_scheduler.close)
                if sender:
                    sender.reason = "service_shutdown"

    app = FastAPI(
        title="Auto Promotion API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.engine = engine
    app.state.store = store
    app.state.connections = connections
    app.state.statistics = statistics
    app.state.pools, app.state.campaigns = pools, campaigns
    app.state.tiktok = tiktok

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"error": "host_not_allowed"}, status_code=400)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.url.netloc:
                return JSONResponse({"error": "cross_origin_denied"}, status_code=403)
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            authenticated = hmac.compare_digest(bearer, operator_token) or hmac.compare_digest(
                request.cookies.get("ap_session", ""), session_token
            )
            bootstrap = request.url.path == "/api/session" and request.method == "POST"
            if bootstrap:
                peer = request.client.host if request.client else ""
                if (
                    peer not in {"127.0.0.1", "::1", "testclient"}
                    or request.headers.get("x-ap-client") != "local-ui"
                ):
                    return JSONResponse({"error": "local_session_only"}, status_code=403)
            elif not authenticated:
                return JSONResponse({"error": "authentication_required"}, status_code=401)
            if (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and not bearer
                and request.headers.get("x-ap-client") != "local-ui"
            ):
                return JSONResponse({"error": "csrf_header_required"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path != "/docs":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
            )
        return response

    @app.exception_handler(Conflict)
    async def workflow_error(request, exc):
        return JSONResponse(
            {"error": str(exc)}, status_code=404 if isinstance(exc, NotFound) else 409
        )

    @app.exception_handler(AccountBusy)
    async def account_busy(request, exc):
        return JSONResponse({"error": "账号正被其他操作使用，请稍后重试"}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path == "/api/connections" or request.url.path.startswith("/api/tiktok/"):
            # Pydantic errors can echo the full submitted token or extra sensitive fields.
            return JSONResponse({"error": "连接信息不完整或格式错误，请核对表单"}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(BackendError)
    async def backend_error(request, exc):
        return JSONResponse({"error": exc.safe_code}, status_code=409)

    @app.get("/api/tiktok/app")
    def tiktok_app_status():
        return tiktok.status()

    @app.put("/api/tiktok/app")
    def tiktok_app_save(value: TikTokAppInput):
        return tiktok.save_app(value)

    @app.post("/api/tiktok/authorize")
    def tiktok_authorize(value: TikTokAuthorizeInput):
        return tiktok.authorize(value)

    @app.post("/api/tiktok/webhooks")
    def tiktok_webhook_configure(value: TikTokWebhookInput):
        return tiktok.configure_webhook(value)

    @app.post("/api/tiktok/authorize/complete")
    def tiktok_complete(value: TikTokCompleteInput):
        return tiktok.complete(value.callback_url.get_secret_value())

    @app.post("/api/tiktok/accounts/{account_id}/{kind}")
    def tiktok_read(account_id: str, kind: str, value: TikTokReadInput):
        if kind not in {"videos", "comments", "insights"}:
            raise HTTPException(status_code=404)
        return tiktok.read(account_id, kind, value)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "worker": worker,
            "live_writes": engine.live_enabled,
            "report_worker": report_worker,
            "execution_mode": "pooled" if pooled_worker else "legacy",
        }

    @app.post("/api/session")
    def session():
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "ap_session", session_token, httponly=True, samesite="strict", max_age=86400
        )
        return response

    @app.get("/api/overview")
    def overview():
        return {
            "accounts": len(engine.accounts()),
            "tasks": store.rows("SELECT state,count(*) AS count FROM tasks GROUP BY state"),
            "items": store.rows("SELECT status,count(*) AS count FROM items GROUP BY status"),
            "global_stop": store.one("SELECT value FROM meta WHERE key='global_stop'")["value"]
            == "1",
            "live_writes": engine.live_enabled,
            "worker": worker,
            "storage": "SQLite · WAL / FULL",
            "timezone": "Asia/Shanghai",
            "report_worker": report_worker,
            "sender": Workers(store, clock).snapshot(live=engine.live_enabled),
            "execution_pools": JobQueue(engine).pools(),
        }

    @app.get("/api/accounts")
    def accounts():
        return engine.accounts()

    @app.get("/api/execution/settings")
    def execution_settings():
        return engine.execution_settings()

    @app.put("/api/execution/settings")
    def configure_execution(value: ExecutionSettings):
        return engine.configure_execution(value)

    @app.get("/api/accounts/{account_id}/profile")
    def account_profile(account_id: str):
        return pools.profile(account_id)

    @app.put("/api/accounts/{account_id}/profile")
    def update_profile(account_id: str, value: AccountProfile):
        return pools.set_profile(account_id, value)

    @app.get("/api/pools")
    def pool_list():
        return pools.list()

    @app.post("/api/pools", status_code=201)
    def create_pool(value: PoolInput):
        return pools.save(value)

    @app.put("/api/pools/{pool_id}")
    def update_pool(pool_id: str, value: PoolInput):
        return pools.save(value, pool_id)

    @app.get("/api/opportunities")
    def opportunity_list(offset: int = 0, limit: int = 100, platform: str | None = None):
        return campaigns.content(offset=offset, limit=limit, platform=platform)

    @app.post("/api/opportunities/import")
    def import_opportunities(value: OpportunityImport):
        return campaigns.import_content(value.items)

    @app.post("/api/accounts/{account_id}/content")
    def collect_content(account_id: str, value: ContentCollectInput):
        return campaigns.collect_telegram_content(account_id, value.target, value.limit)

    @app.get("/api/campaigns")
    def campaign_list():
        return campaigns.list()

    @app.get("/api/drafts/status")
    def draft_status():
        from .drafts import DraftService

        return DraftService().status()

    @app.post("/api/campaigns/{campaign_id}/drafts")
    def generate_drafts(campaign_id: str, value: GenerateDraftsInput):
        return campaigns.generate_drafts(campaign_id, value.assignment_ids)

    @app.post("/api/campaigns", status_code=201)
    def create_campaign(value: CampaignInput):
        return campaigns.create(value)

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: str, offset: int = 0, limit: int = 100):
        return campaigns.get(campaign_id, offset=offset, limit=limit)

    @app.post("/api/campaigns/{campaign_id}/plan")
    def plan_campaign(campaign_id: str, value: PlanInput):
        return campaigns.plan(campaign_id, value.expected_revision)

    @app.post("/api/campaigns/{campaign_id}/materialize")
    def materialize_campaign(campaign_id: str):
        return campaigns.materialize(campaign_id)

    @app.post("/api/campaigns/{campaign_id}/state")
    def campaign_state(campaign_id: str, value: TaskStateInput):
        return campaigns.change_state(campaign_id, value.state)

    @app.patch("/api/assignments/{assignment_id}")
    def edit_assignment(assignment_id: str, value: AssignmentUpdate):
        return campaigns.edit_assignment(assignment_id, value)

    @app.post("/api/tasks/{task_id}/populate")
    def populate_batch(task_id: str):
        return campaigns.populate(task_id)

    @app.get("/api/statistics")
    def statistics_snapshot(day: str | None = None):
        try:
            return statistics.snapshot(day=day or datetime.fromtimestamp(clock(), ZONE).date())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/growth/readiness")
    def growth_readiness():
        return growth.readiness(worker=worker, report_worker=report_worker)

    @app.get("/api/growth/goals")
    def growth_goals():
        return growth.goals()

    @app.post("/api/growth/goals", status_code=201)
    def create_growth_goal(value: GrowthGoal):
        return growth.save_goal(value)

    @app.put("/api/growth/goals/{goal_id}")
    def update_growth_goal(goal_id: str, value: GrowthGoal):
        return growth.save_goal(value, goal_id)

    @app.get("/api/growth/goals/{goal_id}")
    def growth_report(goal_id: str):
        return growth.report(goal_id)

    @app.post("/api/growth/goals/{goal_id}/channels", status_code=201)
    def create_growth_channel(goal_id: str, value: GrowthChannel):
        return growth.add_channel(goal_id, value)

    @app.post("/api/growth/goals/{goal_id}/events")
    def growth_events(goal_id: str, value: ConversionImport):
        return growth.import_events(goal_id, value.items)

    @app.post("/api/growth/goals/{goal_id}/store-metrics")
    def growth_metrics(goal_id: str, value: StoreMetricImport):
        return growth.import_metrics(goal_id, value.items)

    @app.get("/api/growth/inbox")
    def growth_inbox(offset: int = 0, limit: int = 100):
        return growth.inbox(offset, limit)

    @app.patch("/api/growth/inbox/{reply_id}")
    def update_growth_reply(reply_id: str, value: InboxUpdate):
        return growth.update_reply(reply_id, value)

    @app.post("/api/growth/inbox/{reply_id}/followup", status_code=201)
    def create_reply_followup(reply_id: str, value: TaskInput):
        return growth.create_followup(reply_id, value)

    @app.get("/api/growth/feeds")
    def growth_feeds():
        return growth.feeds()

    @app.post("/api/growth/feeds", status_code=201)
    def create_growth_feed(value: FeedInput):
        return growth.save_feed(value)

    @app.put("/api/growth/feeds/{feed_id}")
    def update_growth_feed(feed_id: str, value: FeedInput):
        if not any(f["id"] == feed_id for f in growth.feeds()):
            raise NotFound("采集来源不存在")
        return growth.save_feed(value, feed_id)

    @app.post("/api/growth/feeds/poll")
    def poll_growth_feeds():
        return growth.poll_feeds()

    @app.put("/api/opportunities/{opportunity_id}")
    def review_opportunity(opportunity_id: str, value: ContentReview):
        return growth.review_content(opportunity_id, value.payload, value.expected_revision)

    @app.get("/api/statistics/reports")
    def statistics_reports(limit: int = 30):
        try:
            return {"reports": statistics.reports(limit), "scheduler_running": report_worker}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/statistics/run")
    def statistics_run():
        return statistics.run_due()

    @app.post("/api/items/{item_id}/feedback")
    def item_feedback(item_id: str):
        return engine.collect_feedback(item_id)

    @app.post("/api/accounts", status_code=201)
    def add_account(value: AccountInput):
        return engine.add_account(value)

    @app.patch("/api/accounts/{account_id}")
    def update_account(account_id: str, value: AccountUpdate):
        return engine.update_account(account_id, value)

    @app.get("/api/connections")
    def connection_list():
        return connections.list()

    @app.post("/api/connections", status_code=202)
    def connect(value: ConnectionInput):
        return connections.start(value)

    @app.get("/api/connections/{connection_id}")
    def connection_status(connection_id: str):
        return connections.get(connection_id)

    @app.post("/api/connections/{connection_id}/cancel")
    def cancel_connection(connection_id: str):
        return connections.cancel(connection_id)

    @app.post("/api/accounts/{account_id}/check")
    def check(account_id: str):
        return engine.check_account(account_id)

    @app.get("/api/accounts/{account_id}/targets")
    def account_targets(account_id: str):
        return engine.account_targets(account_id)

    @app.post("/api/accounts/{account_id}/enabled")
    def account_enabled(account_id: str, value: Toggle):
        return engine.disable_account(account_id, not value.enabled)

    @app.get("/api/execution/pools")
    def execution_pools():
        return JobQueue(engine).pools()

    @app.get("/api/tasks")
    def tasks():
        return [
            {
                k: t[k]
                for k in (
                    "id",
                    "name",
                    "mode",
                    "state",
                    "phase",
                    "created_at",
                    "summary",
                    "account_snapshot",
                    "execution",
                    "readiness",
                )
            }
            for t in [
                engine.task(r["id"])
                for r in store.rows("SELECT id FROM tasks ORDER BY created_at DESC LIMIT 200")
            ]
        ]

    @app.post("/api/tasks", status_code=201)
    def create_task(value: TaskInput):
        return engine.create_task(value)

    @app.get("/api/task-templates")
    def task_templates():
        return {"templates": task_tools.templates()}

    @app.post("/api/task-templates", status_code=201)
    def save_task_template(value: TemplateSave):
        return task_tools.save_template(value)

    @app.post("/api/task-templates/{template_id}/tasks", status_code=201)
    def task_from_template(template_id: str, value: TemplateTask):
        return task_tools.create_from_template(template_id, value)

    @app.get("/api/tasks/{task_id}/evidence")
    def task_evidence(task_id: str):
        return task_tools.evidence(task_id)

    @app.get("/api/tasks/{task_id}/export")
    def export_task(task_id: str, format: str = "zip"):
        data = task_tools.export(task_id, format)
        types = {"zip": "application/zip", "json": "application/json", "html": "text/html"}
        filename = "".join(c for c in task_id if c.isascii() and (c.isalnum() or c in "-_"))
        return Response(
            data,
            media_type=types[format],
            headers={
                "Content-Disposition": f'attachment; filename="{filename}.{format}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        return engine.task(task_id)

    @app.post("/api/tasks/{task_id}/start")
    def start(task_id: str):
        return engine.start_and_run(task_id, source="client")

    @app.put("/api/tasks/{task_id}/items")
    def items(task_id: str, values: list[ItemInput]):
        return engine.set_items(task_id, values)

    @app.post("/api/tasks/{task_id}/prepare")
    def prepare(task_id: str):
        return engine.prepare(task_id)

    @app.post("/api/tasks/{task_id}/state")
    def state(task_id: str, value: TaskStateInput):
        return engine.change_state(task_id, value.state)

    @app.post("/api/tasks/{task_id}/run")
    def run(task_id: str):
        engine.task(task_id)
        with WorkerRun(store, task_id, live_enabled=engine.live_enabled) as sender:
            sender.phase("executing")
            return engine.run_once(task_id)

    @app.post("/api/tasks/{task_id}/discover")
    def discover(task_id: str, value: DiscoverInput):
        return engine.discover(task_id, value.account_id)

    @app.post("/api/tasks/{task_id}/candidates")
    def candidate(task_id: str, value: CandidateInput):
        return engine.candidate(task_id, value)

    @app.post("/api/tasks/{task_id}/join")
    def join(task_id: str):
        engine.start_task(task_id, source="client", sending=False)
        return engine.run_joins(task_id)

    @app.post("/api/candidates/{candidate_id}/reconcile")
    def reconcile_join(candidate_id: str):
        return engine.reconcile_join(candidate_id)

    @app.post("/api/items/{item_id}/reconcile")
    def reconcile(item_id: str):
        return engine.reconcile(item_id)

    @app.post("/api/items/{item_id}/manual-outcome")
    def manual(item_id: str, value: ManualOutcome):
        return engine.manual_outcome(item_id, value)

    @app.post("/api/control/stop")
    def stopped(value: Toggle):
        return engine.set_stop(value.enabled)

    @app.get("/api/services")
    def services():
        return registry.services()

    @app.get("/api/calls")
    def calls():
        return store.rows("SELECT * FROM calls ORDER BY id DESC LIMIT 100")

    @app.get("/api/audit")
    def audit(task_id: str | None = None):
        return [
            decode(r, "details")
            for r in store.rows(
                "SELECT * FROM audit"
                + (" WHERE task_id=?" if task_id else "")
                + " ORDER BY id DESC LIMIT 150",
                (task_id,) if task_id else (),
            )
        ]

    @app.get("/api/legacy/tasks")
    def legacy():
        return inventory(workspace)

    @app.post("/api/media")
    async def media(request: Request):
        filename = unquote(request.headers.get("x-filename", "upload.bin"))
        if Path(filename).name != filename or len(filename) > 160 or not filename.strip():
            raise Conflict("文件名无效")
        parts = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 50 * 1024 * 1024:
                raise Conflict("单个媒体最大 50 MB")
            parts.append(chunk)
        data = b"".join(parts)
        if not data:
            raise Conflict("文件不能为空")
        sha = hashlib.sha256(data).hexdigest()
        path = engine.media_root / (sha + "-" + filename)
        if not path.exists():
            path.write_bytes(data)
            path.chmod(0o600)
        return {
            "filename": filename,
            "path": str(path),
            "sha256": sha,
            "mime_type": request.headers.get("content-type", "application/octet-stream"),
            "source_url": "",
            "media_type": "",
            "is_ai_generated": False,
        }

    web = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    return app
