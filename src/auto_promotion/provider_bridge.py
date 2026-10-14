"""Private, fixed-operation bridge run in each existing backend's Python runtime.

stdout is a strict JSON allowlist. Provider stdout/stderr, tokens, credentials and
raw identity objects never reach the control plane. No arbitrary-code/tool API.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import PROJECTS, load_environment


def http(url, method="GET", body=None, headers=None):
    req = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    options = {}
    if url.startswith(("https://localhost:", "https://127.0.0.1:")) and os.getenv(
        "META_SOCIAL_SSL_CERTFILE"
    ):
        options["context"] = ssl.create_default_context(
            cafile=os.getenv("META_SOCIAL_SSL_CERTFILE")
        )
    with urlopen(req, timeout=15, **options) as response:
        return json.loads(response.read())


def assert_writer(platform):
    if (
        os.getenv("AUTO_PROMOTION_LIVE_WRITES") != "1"
        or os.getenv("AP_" + platform.upper() + "_OWNER") != "auto-promotion"
    ):
        raise RuntimeError("live_writer_not_commissioned")


def safe_error_code(error):
    """Only exception class and HTTP status, never messages or response bodies."""
    children = getattr(error, "exceptions", None)
    if children:
        return safe_error_code(children[0])
    if isinstance(error, HTTPError):
        return "HTTPError_" + str(error.code)
    if isinstance(error, URLError) and isinstance(error.reason, BaseException):
        return type(error.reason).__name__
    return type(error).__name__


def legacy_gate(platform, root, account, action):
    """Read only stop/limit metadata. Missing state blocks execution.

    Commissioning requires the legacy worker to be stopped. The control plane
    atomically adds its own claimed count to the legacy usage returned here.
    """
    env, default = (
        ("META_SOCIAL_DB_PATH", "var/meta-social.db")
        if platform == "instagram"
        else ("X_ENGAGEMENT_DB", "var/x-engagement.db")
    )
    path = Path(os.getenv(env, default))
    if not path.is_absolute():
        path = root / path
    today = datetime.now(timezone.utc).date().isoformat()
    now = time.time()
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if platform == "instagram":
            stop = db.execute(
                "SELECT value_json FROM system_settings WHERE key='global_stop'"
            ).fetchone()
            if not stop or json.loads(stop[0]).get("stopped", True):
                return {"allowed": False}
            limit = db.execute(
                "SELECT daily_limit,blocked_until_epoch FROM account_action_limits WHERE platform='instagram' AND account_id=? AND action_type='publish_content'",
                (account,),
            ).fetchone()
            used = db.execute(
                "SELECT count(*) FROM outbound_attempts a JOIN outbound_items i ON i.item_id=a.item_id WHERE i.platform='instagram' AND i.account_id=? AND i.action_type='publish_content' AND a.submitted=1 AND substr(a.created_at,1,10)=?",
                (account, today),
            ).fetchone()[0]
            blocked = not limit or limit["blocked_until_epoch"] > now
        else:
            stop = db.execute("SELECT stopped FROM system_control WHERE control_id=1").fetchone()
            if not stop or stop[0]:
                return {"allowed": False}
            limit = db.execute(
                "SELECT daily_limit FROM action_limits WHERE account_id=? AND action_type=?",
                (account, action),
            ).fetchone()
            used = db.execute(
                "SELECT count(*) FROM action_events e JOIN action_jobs j ON j.job_id=e.job_id WHERE j.account_id=? AND j.action_type=? AND e.event_type='claimed' AND substr(e.created_at,1,10)=?",
                (account, action, today),
            ).fetchone()[0]
            pacing = db.execute(
                "SELECT blocked_until_epoch,next_allowed_epoch FROM action_pacing WHERE account_id=?",
                (account,),
            ).fetchone()
            blocked = not limit or bool(pacing and max(pacing) > now)
        return {
            "allowed": not blocked and used < limit["daily_limit"],
            "daily_remaining": max(0, limit["daily_limit"] - used) if limit else 0,
        }


async def telegram(operation, account, payload):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    endpoint = os.getenv("AP_TELEGRAM_MCP_URL", "http://127.0.0.1:8765/mcp")
    async with streamablehttp_client(endpoint) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, args):
                result = await session.call_tool(name, {**args, "account": account})
                if result.isError:
                    raise RuntimeError("telegram_tool_failed")
                text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                try:
                    return json.loads(text)
                except ValueError:
                    return text

            if operation == "health":
                available = await session.list_tools()
                if "promotion_account_health" not in {t.name for t in available.tools}:
                    return {
                        "healthy": False,
                        "service_ready": True,
                        "identity_verified": False,
                        "reason": "telegram_service_tool_upgrade_required",
                    }
                return await call("promotion_account_health", {})
            if operation == "preflight":
                result = await call("promotion_account_health", {})
                return {"allowed": isinstance(result, dict) and result.get("healthy") is True}
            if operation == "verify_join":
                return await call("verify_promotion_membership", {"target": payload["target"]})
            if operation == "discover":
                records = []
                for query in payload["policy"]["queries"]:
                    result = await call(
                        "search_public_chats",
                        {"query": query, "limit": payload["policy"]["max_candidates"]},
                    )
                    values = (
                        result
                        if isinstance(result, list)
                        else result.get("chats", result.get("results", []))
                        if isinstance(result, dict)
                        else []
                    )
                    for row in values:
                        records.append(
                            {
                                "target": str(row.get("username") or row.get("id") or ""),
                                "title": str(row.get("title") or ""),
                                "text": str(row.get("about") or ""),
                                "language": row.get("language") or "unknown",
                                "members": row.get("participants_count") or row.get("members") or 0,
                                "activity": row.get("activity") or 0,
                                "confidence": row.get("confidence") or 0,
                                "is_public": bool(row.get("username")),
                            }
                        )
                return {
                    "candidates": records,
                    "source_note": "来源缺少语言或置信度时保守拒绝；可录入完整候选证据",
                }
            if operation == "join":
                assert_writer("telegram")
                return await call(
                    "join_public_promotion_group",
                    {"target": payload["target"], "min_interval": payload["min_interval"]},
                )
            item = payload["payload"]
            native = item["action"] in {"schedule_message", "schedule_file"}
            if operation == "execute":
                assert_writer("telegram")
                if native:
                    name = "send_scheduled_file" if item["media"] else "send_scheduled_message"
                    args = {"chat_id": item["target"], "schedule_date": item["scheduled_at"]}
                    if item["media"]:
                        args.update(file_path=item["media"][0]["path"], caption=item["text"])
                    else:
                        args["message"] = item["text"]
                    result = await call(name, args)
                    if not isinstance(result, dict):
                        return {"status": "unknown"}
                    ids = result.get("message_ids") or (
                        [result["message_id"]] if result.get("message_id") else []
                    )
                    lookup = await call("get_scheduled_messages", {"chat_id": item["target"]})
                    found = scheduled_ids(lookup)
                    return {
                        "status": "scheduled",
                        "scheduled": result.get("scheduled"),
                        "active": result.get("active"),
                        "verified": result.get("verified"),
                        "message_ids": ids,
                        "lookup_message_ids": found,
                        "external_reference": f"telegram:{item['target']}:{','.join(map(str, ids))}",
                    }
                if item["action"] == "send_file":
                    result = await call(
                        "send_file",
                        {
                            "chat_id": item["target"],
                            "file_path": item["media"][0]["path"],
                            "caption": item["text"],
                            "verify_receipt": True,
                        },
                    )
                elif item["action"] == "reply":
                    result = await call(
                        "reply_to_message",
                        {
                            "chat_id": item["target"],
                            "message_id": item["reply_to"],
                            "text": item["text"],
                            "verify_receipt": True,
                        },
                    )
                else:
                    result = await call(
                        "send_message",
                        {
                            "chat_id": item["target"],
                            "message": item["text"],
                            "verify_receipt": True,
                        },
                    )
                if not isinstance(result, dict):
                    return {
                        "status": "unknown",
                        "reason": "legacy_service_requires_receipt_upgrade",
                    }
                message_id = result.get("message_id")
                return {
                    "status": "confirmed" if result.get("verified") else "unknown",
                    "verified": bool(result.get("verified")),
                    "message_ids": [message_id] if message_id else [],
                    "external_reference": f"telegram:{item['target']}:{message_id}"
                    if message_id
                    else None,
                }
            if operation == "verify" and native:
                lookup = await call("get_scheduled_messages", {"chat_id": item["target"]})
                ids = payload["result"].get("message_ids", [])
                return {
                    "status": "scheduled",
                    "scheduled": True,
                    "active": bool(
                        ids and set(map(str, ids)) <= set(map(str, scheduled_ids(lookup)))
                    ),
                    "verified": bool(
                        ids and set(map(str, ids)) <= set(map(str, scheduled_ids(lookup)))
                    ),
                    "message_ids": ids,
                    "lookup_message_ids": scheduled_ids(lookup),
                    "external_reference": payload.get("external_reference"),
                }
            if operation == "verify":
                ids = payload["result"].get("message_ids", [])
                if not ids:
                    return {"verified": False}
                result = await call(
                    "verify_sent_message",
                    {
                        "chat_id": item["target"],
                        "message_id": int(ids[0]),
                        "expected_text": item["text"],
                        "reply_to": item.get("reply_to"),
                        "media": bool(item["media"]),
                    },
                )
                return {
                    "status": "confirmed",
                    "verified": isinstance(result, dict) and result.get("verified") is True,
                    "external_reference": payload.get("external_reference"),
                }
            return {"verified": False, "reason": "manual_readonly_reconciliation_required"}


def scheduled_ids(result):
    if isinstance(result, dict):
        result = result.get("messages", result.get("scheduled_messages", []))
    if isinstance(result, list):
        return [
            r.get("id", r.get("message_id"))
            for r in result
            if isinstance(r, dict) and (r.get("id") or r.get("message_id"))
        ]
    if isinstance(result, str):
        return re.findall(r"(?:ID|message_id)[:=]\s*(\d+)", result, re.I)
    return []


def reddit(operation, account, payload, root):
    sys.path.insert(0, str(root))
    if operation == "preflight":
        return {"allowed": True}  # Original writer reserves its persistent rate-limit slot.
    if operation == "health":
        endpoint = os.getenv("REDDIT_BROWSER_CDP_URL", "http://127.0.0.1:9223")
        http(endpoint + "/json/version")
        from reddit_browser import _parse_result, _run

        observed = _parse_result(
            _run(
                "const links = page.locator('header a[href^=\"/user/\"], shreddit-header a[href^=\"/user/\"]'); const hrefs = await links.evaluateAll(nodes => nodes.map(n => n.getAttribute('href'))); return JSON.stringify({handles: [...new Set(hrefs.map(h => h.split('/')[2]).filter(Boolean))]});"
            )
        )
        handles = observed.get("handles", [])
        verified = (
            len(handles) == 1 and handles[0].casefold() == account.removeprefix("u/").casefold()
        )
        return {
            "healthy": verified,
            "service_ready": True,
            "identity_verified": verified,
            "account_identity": handles[0].casefold() if verified else None,
            "reason": "account_read_verified"
            if verified
            else "browser_identity_requires_verification",
        }
    if operation == "discover":
        from mcp_bridge import call_reddit_read_tool

        results = []
        for query in payload["policy"]["queries"]:
            response = call_reddit_read_tool(
                "reddit_search", {"query": query, "limit": payload["policy"]["max_candidates"]}
            )
            rows = (
                response
                if isinstance(response, list)
                else response.get("posts", [])
                if isinstance(response, dict)
                else []
            )
            for row in rows:
                results.append(
                    {
                        "target": row.get("url")
                        or "https://www.reddit.com" + row.get("permalink", ""),
                        "title": row.get("title", ""),
                        "text": row.get("selftext", ""),
                        "language": row.get("language", "unknown"),
                        "activity": row.get("num_comments", 0),
                        "confidence": row.get("confidence", 0),
                        "is_public": True,
                    }
                )
        return {"candidates": results}
    if operation == "execute":
        assert_writer("reddit")
        from rate_limit import reserve_reddit_action_slot
        from reddit_browser import publish_post, publish_reply

        reserve_reddit_action_slot(account)
        item = payload["payload"]
        r = (
            publish_post(item["target"], item["title"], item["text"])
            if item["action"] == "post"
            else publish_reply(item["target"], item["text"])
        )
        return {
            "status": "confirmed" if r.get("verified") else "unknown",
            "verified": bool(r.get("verified")),
            "external_reference": r.get("externalReference"),
            "rate_limited": r.get("rateLimited", False),
            "retry_after_seconds": r.get("retryAfterSeconds"),
        }
    return {"verified": False, "reason": "manual_readonly_reconciliation_required"}


def x_platform(operation, account, payload, root):
    sys.path[:0] = [str(root / "src"), str(root / "services/x-read-service/src")]
    from x_engagement.actions.http_provider import XActionHttpProvider

    if operation == "preflight":
        return legacy_gate("x", root, account, payload["payload"]["action"])
    if operation == "health":
        if not os.getenv("X_ACTION_TOKEN"):
            return {
                "healthy": False,
                "service_ready": False,
                "identity_verified": False,
                "reason": "x_action_connection_not_configured",
            }
        health = XActionHttpProvider.from_env().health_check()
        identity = (
            http(
                os.getenv("X_ACTION_BASE_URL", "http://127.0.0.1:3000") + "/identity",
                headers={"Authorization": "Bearer " + os.getenv("X_ACTION_TOKEN", "")},
            )
            if health.healthy
            else {}
        )
        verified = (
            identity.get("identity_verified") is True
            and str(identity.get("handle", "")).casefold() == account.lstrip("@").casefold()
        )
        return {
            "healthy": verified,
            "service_ready": health.healthy,
            "identity_verified": verified,
            "account_identity": str(identity.get("handle", "")).casefold() if verified else None,
            "reason": "account_read_verified"
            if verified
            else "browser_identity_requires_verification",
        }
    if operation == "discover":
        from x_engagement.models import TopicConfig
        from x_engagement.providers.xtf import XtfReadProvider

        provider = XtfReadProvider()
        results = []
        for q in payload["policy"]["queries"]:
            topic = TopicConfig(
                id="auto-promotion",
                name=q,
                query=q,
                max_results=payload["policy"]["max_candidates"],
            )
            for row in provider.search_topic(topic):
                results.append(
                    {
                        "target": row.canonical_url,
                        "text": row.text,
                        "language": row.language or "unknown",
                        "activity": row.metrics.replies,
                        "confidence": 0.8,
                        "is_public": True,
                    }
                )
        return {"candidates": results}
    if operation == "execute":
        assert_writer("x")
        if not legacy_gate("x", root, account, payload["payload"]["action"])["allowed"]:
            return {"status": "failed_known", "reason": "legacy_execution_blocked"}
        from x_engagement.actions.models import ActionJob, ActionStatus, ActionType

        item = payload["payload"]
        job = ActionJob(
            job_id=payload["item_id"],
            account_id=account,
            target_post_id=item["target"].rstrip("/").split("/")[-1],
            target_url=item["target"],
            action_type=ActionType(item["action"]),
            status=ActionStatus.APPROVED,
            content=item["text"],
            media_paths=tuple(m["path"] for m in item["media"]),
            approved_by="auto-promotion",
            approval_version=1,
        )
        r = XActionHttpProvider.from_env().execute(job)
        return {
            "status": "confirmed" if r.succeeded and r.verified else "unknown",
            "verified": r.verified,
            "external_reference": r.external_reference,
            "rate_limited": r.rate_limited,
            "risk_warning": r.risk_warning,
            "retry_after_seconds": r.retry_after_seconds,
        }
    if operation == "verify":
        from x_engagement.providers.xtf import XtfReadProvider

        if not payload.get("external_reference"):
            return {"verified": False}
        item = XtfReadProvider().fetch_tweet(payload["external_reference"])
        match = (
            item.author_handle.casefold() == account.lstrip("@").casefold()
            and item.text == payload["payload"]["text"]
        )
        return {"status": "confirmed", "verified": match, "external_reference": item.canonical_url}
    return {"verified": False, "reason": "manual_readonly_reconciliation_required"}


def instagram(operation, account, payload, root):
    # Use existing authenticated service: its own grants remain authoritative for legacy tasks.
    default_base = (
        "https://localhost:8090"
        if os.getenv("META_SOCIAL_SSL_CERTFILE")
        else "http://127.0.0.1:8090"
    )
    base = os.getenv("AP_INSTAGRAM_URL", default_base)
    token = os.getenv("META_SOCIAL_OPERATOR_TOKEN", "")
    if not token:
        raise RuntimeError("meta_operator_connection_missing")
    headers = {"X-Meta-Social-Operator-Token": token}
    accounts = http(base + "/v1/meta/accounts", headers=headers)
    rows = accounts if isinstance(accounts, list) else accounts.get("accounts", [])
    found = next((r for r in rows if (r.get("username") or r.get("name")) == account), None)
    if not found:
        raise RuntimeError("instagram_account_not_found")
    key = found.get("account_key") or found.get("key")
    if operation == "preflight":
        if payload["payload"]["action"] == "manual_comment":
            return {"allowed": True}
        return legacy_gate("instagram", root, key, "publish_content")
    if operation == "health":
        r = http(base + f"/v1/meta/accounts/{key}/health", headers=headers)
        healthy = r.get("healthy") is True or r.get("status") in {"healthy", "connected"}
        return {
            "healthy": healthy,
            "service_ready": True,
            "identity_verified": healthy,
            "account_identity": str(key) if healthy else None,
            "reason": "meta_health_checked",
        }
    if operation == "execute":
        assert_writer("instagram")
        if not legacy_gate("instagram", root, key, "publish_content")["allowed"]:
            return {"status": "failed_known", "reason": "legacy_execution_blocked"}
        # Production publishing uses the existing adapter, with its commissioned account/scope checks.
        sys.path.insert(0, str(root / "src"))
        from meta_social.adapters import InstagramPublishingAdapter
        from meta_social.config import Settings
        from meta_social.meta_graph import InstagramGraphClient, RequestsJSONTransport
        from meta_social.models import ActionType, ItemStatus, MediaRef, OutboundItem, Platform
        from meta_social.repository import SQLiteRepository

        settings = Settings.from_env()
        repo = SQLiteRepository(settings.db_path)
        graph = InstagramGraphClient(
            transport=RequestsJSONTransport(timeout_seconds=settings.graph_timeout_seconds),
            api_version=settings.instagram_graph_api_version,
            graph_base_url=settings.instagram_graph_base_url,
            oauth_token_url=settings.instagram_oauth_token_url,
            app_id=settings.instagram_oauth_app_id,
            app_secret=settings.instagram_oauth_app_secret,
        )
        adapter = InstagramPublishingAdapter(
            repo,
            graph,
            settings.credential_encryption_key,
            settings.instagram_write_account_key,
            settings.instagram_write_username,
            live_enabled=settings.enable_live_writes,
        )
        p = payload["payload"]
        media = tuple(
            MediaRef(
                filename=m["filename"],
                sha256=m["sha256"],
                mime_type=m["mime_type"],
                size_bytes=Path(m["path"]).stat().st_size,
                source_url=m["source_url"],
                media_type=m["media_type"],
                is_ai_generated=m["is_ai_generated"],
            )
            for m in p["media"]
        )
        item = OutboundItem(
            item_id=payload["item_id"],
            task_id=payload["task_id"],
            platform=Platform.INSTAGRAM,
            account_id=key,
            target_id=p["target"],
            target_url="",
            action_type=ActionType.PUBLISH_CONTENT,
            status=ItemStatus.APPROVED,
            text=p["text"],
            media=media,
            target_owned=p["target_owned"],
        )
        r = adapter.execute(item)
        return {
            "status": "confirmed"
            if r.verified
            else "failed_known"
            if r.known_failure
            else "unknown",
            "verified": r.verified,
            "external_reference": r.external_reference,
            "rate_limited": r.rate_limited,
            "risk_warning": r.risk_warning,
            "retry_after_seconds": r.retry_after_seconds,
        }
    return {"verified": False, "reason": "manual_readonly_reconciliation_required"}


def main():
    request = json.load(sys.stdin)
    p = request["platform"]
    workspace = Path(os.environ["AUTO_PROMOTION_WORKSPACE"])
    root = workspace / PROJECTS[p]
    # Load credentials only in the isolated backend process; never print them.
    load_environment(root / ".env")
    op = request["operation"]
    if op not in {"health", "preflight", "discover", "execute", "verify", "join", "verify_join"}:
        raise ValueError("operation_not_allowlisted")
    if op in {"execute", "join"}:
        from .backends.common import verify_claim

        verify_claim(request)
    if p == "telegram":
        return asyncio.run(telegram(op, request["account"], request["payload"]))
    return {"reddit": reddit, "instagram": instagram, "x": x_platform}[p](
        op, request["account"], request["payload"], root
    )


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = main()
    except Exception as exc:
        result = {"bridge_error": safe_error_code(exc)}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
