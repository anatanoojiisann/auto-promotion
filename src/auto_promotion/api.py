from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from . import __version__
from .adapters import AdapterRegistry
from .config import database_path, workspace_path
from .core import Conflict, Engine, NotFound
from .legacy import inventory
from .models import (
    AccountInput,
    ApprovalInput,
    CandidateInput,
    ItemInput,
    ManualOutcome,
    TaskInput,
    TaskStateInput,
)
from .store import Store, decode


class Toggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class DiscoverInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str


def create_app(*, db_path=None, registry=None, clock=time.time, worker=False, token=None):
    workspace = workspace_path()
    store = Store(db_path or database_path())
    registry = registry or AdapterRegistry(workspace)
    engine = Engine(
        store, registry, clock=clock, live_enabled=os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1"
    )
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

    async def work_loop():
        while not stop.is_set():
            try:
                await asyncio.to_thread(engine.run_cycle)
            except Exception:
                with store.connect(write=True) as db:
                    Store.audit(db, "worker.error", {"reason": "worker_cycle_failed"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app):
        job = asyncio.create_task(work_loop()) if worker else None
        yield
        stop.set()
        if job:
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="Auto Promotion API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.engine = engine
    app.state.store = store

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

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "worker": worker,
            "live_writes": engine.live_enabled,
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
        }

    @app.get("/api/accounts")
    def accounts():
        return engine.accounts()

    @app.post("/api/accounts", status_code=201)
    def add_account(value: AccountInput):
        return engine.add_account(value)

    @app.post("/api/accounts/{account_id}/check")
    def check(account_id: str):
        return engine.check_account(account_id)

    @app.post("/api/accounts/{account_id}/enabled")
    def account_enabled(account_id: str, value: Toggle):
        return engine.disable_account(account_id, not value.enabled)

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

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        return engine.task(task_id)

    @app.post("/api/tasks/{task_id}/approve/{stage}")
    def approve(task_id: str, stage: str, value: ApprovalInput):
        return engine.approve(task_id, stage, value.phrase)

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
        return engine.run_once(task_id)

    @app.post("/api/tasks/{task_id}/discover")
    def discover(task_id: str, value: DiscoverInput):
        return engine.discover(task_id, value.account_id)

    @app.post("/api/tasks/{task_id}/candidates")
    def candidate(task_id: str, value: CandidateInput):
        return engine.candidate(task_id, value)

    @app.post("/api/tasks/{task_id}/join")
    def join(task_id: str):
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
