"""Small public callback receiver, separate from the private workbench."""

import hashlib
import json
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .backends.common import BackendError
from .core import Conflict
from .models import canonical
from .tiktok import TikTokManager, verify_signature


def create_gateway(engine):
    manager = TikTokManager(engine)
    callback = urlparse(manager.app()["redirect_uri"])
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get(callback.path)
    def oauth_callback(request: Request):
        # Use the configured public URL even when running behind a TLS proxy.
        try:
            manager.complete(manager.app()["redirect_uri"] + "?" + request.url.query)
        except (BackendError, Conflict):
            return PlainTextResponse(
                "TikTok authorization was not completed. Return to the workbench and start a new authorization.",
                status_code=400,
            )
        return PlainTextResponse("TikTok account connected. You can return to the workbench.")

    @app.post("/webhooks/tiktok")
    async def webhook(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 256 * 1024:
                return JSONResponse({"error": "body_too_large"}, status_code=413)
        secret = manager.app()["client_secret"]
        if not verify_signature(
            bytes(raw), request.headers.get("tiktok-signature", ""), secret, now=engine.clock()
        ):
            return JSONResponse({"error": "invalid_signature"}, status_code=403)
        try:
            event = json.loads(raw)
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                raise ValueError
            if str(event.get("client_key")) != manager.app()["identity"]:
                raise ValueError
            # Keep only envelope fields. Never trust a webhook as proof of publication.
            details = {
                k: event[k]
                for k in ("event", "create_time", "client_key", "user_openid")
                if k in event
            }
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid_event"}, status_code=400)
        identifier = hashlib.sha256(raw).hexdigest()
        with engine.store.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO tiktok_events VALUES(?,?,?,?)",
                (identifier, engine.clock(), event["event"][:160], canonical(details)),
            )
            db.execute("DELETE FROM meta WHERE key='tiktok_poll_at'")
            db.execute(
                "DELETE FROM tiktok_events WHERE received_at<?", (engine.clock() - 30 * 86400,)
            )
        return {"ok": True}

    return app
