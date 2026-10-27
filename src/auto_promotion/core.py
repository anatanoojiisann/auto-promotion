from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import quotas
from .account_blocks import FIELDS as BLOCK_FIELDS
from .backends.common import (
    BackendError,
    automatic_action_allowed,
    automatic_actions,
    owned_x_target,
    read_profile,
    x_post_weight,
)
from .dispatcher import AccountBusy, AccountLanes
from .execution_diagnostics import FIELDS, admission_failure_reason, safe_code
from .models import (
    CAPABILITIES,
    AccountInput,
    AccountUpdate,
    CandidateInput,
    ExecutionSettings,
    ItemInput,
    TaskInput,
    canonical,
    digest,
    epoch,
)
from .readiness import DELIVERY_GRACE_SECONDS, REASONS, task_workflow, telegram_blocker
from .routing import route_for
from .runtime_identity import result_runtime
from .statistics import ZONE, Statistics, publication_time, record_publication
from .store import Store, decode
from .task_dependencies import publication_schedule
from .workers import Workers

RUNTIME = result_runtime("engine")


class Conflict(Exception):
    """An explicit, safe-to-display workflow failure."""


class NotFound(Conflict):
    pass


TERMINAL = {
    "confirmed",
    "scheduled",
    "failed_known",
    "unknown",
    "manual_attention",
    "missed",
    "cancelled",
}
RISK = re.compile(
    r"\b(?:nsfw|porn|adult|escort|dating|casino|gambling|scam)\b|18\+|色情|赌博|博彩|约炮|诈骗",
    re.I,
)
NON_ADULT = re.compile(r"(?<![\w-])non-adult(?![\w-])", re.I)


class Engine:
    def __init__(
        self, store: Store, adapters, *, clock=time.time, live_enabled=False, media_root=None
    ):
        self.store = store
        self.adapters = adapters
        self.adapters.database = store.path
        self.clock = clock
        self.live_enabled = live_enabled
        self.media_root = Path(media_root or Path(store.path).parent / "media").resolve()
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.lanes = AccountLanes(store)

    def execution_settings(self):
        with self.store.connect() as db:
            return quotas.settings(db)

    def configure_execution(self, value: ExecutionSettings):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta SET value=? WHERE key='execution_settings'",
                (canonical(value.model_dump()),),
            )
            Store.audit(db, "execution.settings", value.model_dump(), now=self.clock())
        from .cloud_control import queue_settings

        queue_settings(
            self,
            {
                k: v
                for k, v in value.model_dump().items()
                if k
                in {
                    "global_limit",
                    "max_workers",
                    "platform_workers",
                    "platform_limits",
                    "pool_workers",
                }
            },
        )
        return self.execution_settings()

    def account(self, account_id):
        row = self.store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not row:
            raise NotFound("账号不存在")
        from .account_blocks import recover_private_stop

        recover_private_stop(self.store, row, now=self.clock())
        value = decode(row, "health")
        from .health_checks import probe_key, state

        value["connection_state"] = state(value, self.clock())
        if (
            value["mode"] == "live"
            and value["connection_state"] == "healthy"
            and value["health"].get("probe_key")
            and value["health"]["probe_key"] != probe_key(value, self.store.path)
        ):
            value["connection_state"] = "unverified"
        value["capabilities"] = sorted(CAPABILITIES[value["platform"]])
        value["automatic_actions"] = automatic_actions(value)
        if value["platform"] == "x":
            try:
                identity = (
                    read_profile(
                        "x",
                        value["backend_label"],
                        accounts_dir=os.getenv(
                            "AP_ACCOUNTS_DIR", Path(self.store.path).parent / "accounts"
                        ),
                    )["identity"]
                    if value["mode"] == "live"
                    else "simulation"
                )
                value["owned_target_url"] = (
                    "https://x.com/" + identity
                    if isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", identity)
                    else ""
                )
            except BackendError:
                value["owned_target_url"] = ""
        if value["platform"] == "tiktok":
            try:
                value["owned_target_id"] = (
                    read_profile(
                        "tiktok",
                        value["backend_label"],
                        accounts_dir=os.getenv(
                            "AP_ACCOUNTS_DIR", Path(self.store.path).parent / "accounts"
                        ),
                    )["identity"]
                    if value["mode"] == "live"
                    else "simulation-tiktok"
                )
            except BackendError:
                value["owned_target_id"] = ""
        from .account_blocks import active_block

        with self.store.connect() as db:
            value["outbound_block"] = active_block(db, account_id, self.clock())
        value["writable"] = bool(
            not value["outbound_block"]
            and value["connection_state"] == "healthy"
            and not value["disabled"]
            and value["cooldown_until"] <= self.clock()
            and (value["mode"] == "simulation" or self.live_enabled)
            and self.adapters.commissioned(value)
            and (
                value["platform"] != "tiktok"
                or value["mode"] == "simulation"
                or value["health"].get("permissions")
            )
        )
        return value

    def accounts(self):
        return [
            self.account(r["id"])
            for r in self.store.rows("SELECT id FROM accounts ORDER BY platform,label")
        ]

    def add_account(self, value: AccountInput):
        if value.mode == "live" and value.min_interval < 120:
            raise Conflict("真实账号最小发送间隔为 120 秒")
        account_id = "acc-" + uuid.uuid4().hex[:12]
        with self.store.connect(write=True) as db:
            existing = db.execute(
                "SELECT id FROM accounts WHERE platform=? AND backend_label=? AND mode=?",
                (value.platform, value.backend_label, value.mode),
            ).fetchone()
            if existing:
                raise Conflict("该平台账号已存在")
            db.execute(
                "INSERT INTO accounts(id,platform,label,backend_label,mode,daily_limit,hourly_limit,min_interval) VALUES(?,?,?,?,?,?,?,?)",
                (
                    account_id,
                    value.platform,
                    value.label,
                    value.backend_label,
                    value.mode,
                    value.daily_limit,
                    value.hourly_limit,
                    value.min_interval,
                ),
            )
            Store.audit(
                db, "account.created", {"account_id": account_id, "platform": value.platform}
            )
        return self.account(account_id)

    def update_account(self, account_id, value: AccountUpdate):
        account = self.account(account_id)
        if account["mode"] == "live" and value.min_interval < 120:
            raise Conflict("真实账号最小发送间隔为 120 秒")
        changes = value.model_dump()
        if all(account[key] == val for key, val in changes.items()):
            return account
        daily_only = all(
            account[key] == val for key, val in changes.items() if key != "daily_limit"
        )
        with self.store.connect(write=True) as db:
            for task in db.execute(
                "SELECT id,account_ids,state,policy FROM tasks WHERE state NOT IN ('cancelled','completed')"
            ):
                if account_id in json.loads(task["account_ids"]):
                    # Old snapshots stay immutable; resuming them still requires
                    # a new task after this explicit account-budget change.
                    if daily_only and (
                        task["state"] == "paused"
                        or epoch(json.loads(task["policy"])["ends_at"]) <= self.clock()
                    ):
                        continue
                    raise Conflict("账号已被未结束任务使用；请先结束该任务并创建新任务")
            db.execute(
                "UPDATE accounts SET label=?,daily_limit=?,hourly_limit=?,min_interval=? WHERE id=?",
                (
                    value.label,
                    value.daily_limit,
                    value.hourly_limit,
                    value.min_interval,
                    account_id,
                ),
            )
            Store.audit(
                db,
                "account.updated",
                {
                    "account_id": account_id,
                    "before": {k: account[k] for k in changes},
                    "after": changes,
                },
            )
        return self.account(account_id)

    def call(self, account, operation, payload=None, task_id=None):
        from .target_checks import TargetChecks

        account = self.account(account["id"])
        route = route_for(account, (payload or {}).get("payload", {}))
        with self.lanes.acquire(account["id"], resource=route.resource):
            # Another caller can record a FloodWait/disable while this caller is
            # acquiring its lane. Never authorize a read with a stale account.
            account = self.account(account["id"])
            if route_for(account, (payload or {}).get("payload", {})) != route:
                raise Conflict("账号连接已变化，请重新核验")
            if account["platform"] == "x":
                from .read_cooldowns import x_read_until

                with self.store.connect() as db:
                    retry_at = x_read_until(db, account)
                if retry_at > self.clock():
                    return self._observe_telegram_read(
                        account,
                        operation,
                        task_id,
                        {
                            "healthy": False,
                            "service_ready": False,
                            "identity_verified": False,
                            "allowed": False,
                            "verified": False,
                            "coverage_complete": False,
                            "candidates": [],
                            "reason": "XReadCooldown",
                            "rate_limited": True,
                            "read_state": "rate_limited",
                            "retry_after_seconds": max(1, int(retry_at - self.clock())),
                            "account_retry_at": retry_at,
                            "cooldown_source": "persisted_x_read_cooldown",
                            "read_primary_request_count": 0,
                            "read_primary_response_count": 0,
                            "read_observation_source": "local_cooldown_no_request",
                        },
                    )
            if (
                account["platform"] == "telegram"
                and account["mode"] == "live"
                and (account["disabled"] or account["cooldown_until"] > self.clock())
            ):
                return self._observe_telegram_read(
                    account,
                    operation,
                    task_id,
                    {
                        "allowed": False,
                        "verified": False,
                        "reason": "AccountDisabled" if account["disabled"] else "AccountCooldown",
                        "account_retry_at": account["cooldown_until"],
                        "cooldown_source": "persisted_account_cooldown",
                        "read_state": "cooldown",
                        "read_rpc_used": 0,
                        "read_rpc_count_source": "local_cooldown_no_rpc",
                        "read_rpc_wire_attempts": None,
                        "read_rpc_internal_retries": None,
                    },
                )
            if operation in {"execute", "join"} and account["outbound_block"]:
                return {
                    "status": "failed_known",
                    "verified": False,
                    "confirmed_not_submitted": True,
                    "reason": "AccountOutboundBlocked",
                    "account_block_reason": account["outbound_block"]["reason"],
                }
            if account["platform"] == "telegram" and operation not in {"execute", "join"}:
                from .health_checks import probe_revision

                policy = self.telegram_read_policy(account["id"])
                payload = {
                    **(payload or {}),
                    "read_policy": policy["policy"],
                    "read_policy_context": {
                        "database": self.store.path,
                        "account_id": account["id"],
                    },
                    "read_connection_revision": probe_revision(account, self.store.path),
                }
            checks = TargetChecks(self, account, operation, payload or {})
            cached = checks.read()
            if cached is not None:
                return self._observe_telegram_read(account, operation, task_id, cached)
            try:
                result = self._call_unlocked(account, operation, payload, task_id)
            except Conflict as exc:
                if not checks.enabled:
                    raise
                result = checks.failure(getattr(exc, "safe_code", "TargetCheckUnavailable"))
            if account["platform"] == "telegram" and (
                result.get("rate_limited") or result.get("risk_warning")
            ):
                result = {
                    **result,
                    "account_retry_at": self.clock()
                    + max(1, int(result.get("retry_after_seconds") or 3600)),
                }
                self.apply_cooldown(account["id"], result)
            if account["platform"] == "x" and result.get("rate_limited"):
                from .read_cooldowns import record_x_read_cooldown

                with self.store.connect(write=True) as db:
                    retry_at = record_x_read_cooldown(db, account, result, self.clock())
                result = {
                    **result,
                    "account_retry_at": retry_at,
                    "cooldown_source": "x_read_rate_limit",
                }
            from .account_blocks import normalize_evidence, observe

            normalize_evidence(result, account["platform"])
            if any(key in result for key in BLOCK_FIELDS) or operation == "execute":
                with self.store.connect(write=True) as db:
                    observe(
                        db,
                        account["id"],
                        result,
                        now=self.clock(),
                        operation=operation,
                        payload=payload,
                        task_id=task_id,
                    )
            checks.save(result)
            return self._observe_telegram_read(account, operation, task_id, result)

    def _observe_telegram_read(self, account, operation, task_id, result):
        if account["platform"] == "x":
            from .read_cooldowns import observe_x_read

            try:
                observe_x_read(self.store, account["id"], task_id, operation, result, self.clock())
            except (sqlite3.Error, TypeError, ValueError, OverflowError):
                logging.getLogger(__name__).warning("Read diagnostics unavailable")
            return result
        if account["platform"] != "telegram":
            return result
        if "read_rpc_used" not in result:
            result = {
                **result,
                "read_rpc_used": None,
                "read_rpc_wire_attempts": None,
                "read_rpc_internal_retries": None,
                "read_rpc_count_source": "unavailable_legacy"
                if os.getenv("AP_TELEGRAM_BACKEND") == "legacy"
                else "unavailable",
            }
        from .telegram_reading import read_diagnostics

        observed = {
            **read_diagnostics(result),
            **{key: result[key] for key in BLOCK_FIELDS if key in result},
            **({"reason": safe_code(result["reason"])} if result.get("reason") else {}),
        }
        # Consumer tokens remain in their private durable stores, never in the
        # diagnostic stream. Only aggregate categories and safe scalar evidence.
        observed.pop("read_cursor_next", None)
        try:
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO telegram_read_observations(account_id,task_id,operation,observed_at,result) VALUES(?,?,?,?,?)",
                    (account["id"], task_id, operation, self.clock(), canonical(observed)),
                )
                db.execute(
                    "DELETE FROM telegram_read_observations WHERE account_id=? AND id NOT IN "
                    "(SELECT id FROM telegram_read_observations WHERE account_id=? ORDER BY id DESC LIMIT 1000)",
                    (account["id"], account["id"]),
                )
        except sqlite3.Error:
            logging.getLogger(__name__).warning("Telegram read diagnostics unavailable")
        return result

    def _call_unlocked(self, account, operation, payload=None, task_id=None):
        started = time.monotonic()
        provider_invoked = False
        try:
            if operation in {"execute", "join"} and not (
                account["mode"] == "simulation"
                and getattr(self.adapters, "offline_simulation", False)
            ):
                from .cloud_control import CloudControl

                context = getattr(self.adapters.context, "job", None)
                from .execution_channels import independent_active

                if context and context.get("task_channel"):
                    from .task_channels import guard_context

                    guard_context(self, context)
                local_independent = bool(
                    context
                    and context.get("channel") == "local"
                    and (context.get("task_channel") or independent_active(self))
                )
                cloud = None if local_independent else CloudControl.configured()
                required = self.store.one(
                    "SELECT value FROM meta WHERE key='cloud_control_required'"
                )
                if not cloud and required and required["value"] == "1" and not local_independent:
                    from .cloud_control import CloudUnavailable

                    raise CloudUnavailable("CloudControlConfigurationMissing")
                if context and context.get("channel") == "local":
                    from .execution_channels import guard_local

                    guard_local(self, context, cloud)
                elif cloud:
                    attempt_id = (payload or {}).get("attempt_id")
                    if operation == "join":
                        attempt = self.store.one(
                            "SELECT id FROM join_attempts WHERE candidate_id=?",
                            ((payload or {}).get("candidate_id"),),
                        )
                        attempt_id = attempt["id"] if attempt else None
                    cloud.guard(getattr(self.adapters.context, "job", None), attempt_id)
            provider_invoked = True
            result = {**RUNTIME, **self.adapters.call(account, operation, payload or {})}
        except Exception as exc:
            # Never expose provider exceptions: they may include tokens, URLs or browser state.
            outbound = (payload or {}).get("payload", {})
            owned_post_admission_failed = (
                not provider_invoked
                and operation == "execute"
                and account["platform"] == "x"
                and isinstance(outbound, dict)
                and outbound.get("action") == "post"
                and outbound.get("target_owned") is True
            )
            error_code = (
                admission_failure_reason(exc)
                if owned_post_admission_failed
                else getattr(exc, "safe_code", type(exc).__name__)
            )
            self._log_call(
                account["platform"],
                operation,
                "error",
                started,
                task_id,
                error_code,
                "execution_admission" if owned_post_admission_failed else None,
            )
            if owned_post_admission_failed:
                # Authorization cannot submit a post. No adapter was invoked, so
                # this new attempt has conclusive negative submission evidence.
                # Keep it terminal and counted; never retry it automatically.
                return {
                    "status": "failed_known",
                    "verified": False,
                    "reason": error_code,
                    "execution_step": "execution_admission",
                    "submission_started": False,
                    "confirmed_not_submitted": True,
                }
            error = Conflict("平台调用未完成；请查看 API 中心的错误类型并核验连接")
            error.safe_code = safe_code(
                getattr(exc, "safe_code", type(exc).__name__), "BackendError"
            )
            from .execution_diagnostics import exception_details

            details = exception_details(exc)
            error.read_diagnostics = details
            error.rpc_counts = details.get("rpc_counts")
            error.rpc_count_source = details.get("rpc_count_source")
            self._observe_telegram_read(
                account, operation, task_id, {"reason": error.safe_code, **details}
            )
            raise error from None
        outcome = result.get("status")
        status = outcome if outcome in {"unknown", "failed_known", "manual_attention"} else "ok"
        if (
            result.get("healthy") is False
            or result.get("allowed") is False
            or (operation == "discover" and result.get("coverage_complete") is False)
            or (operation == "feedback" and result.get("verified") is not True)
        ):
            status = "error"
        self._log_call(
            account["platform"],
            operation,
            status,
            started,
            task_id,
            safe_code(result.get("reason"), None) if status != "ok" else None,
            safe_code(result.get("execution_step"), None),
        )
        return result

    def _log_call(self, *args):
        # Diagnostic persistence must never discard an accepted provider result
        # or replace the original provider error. Do not repeat the platform call.
        try:
            self.store.log_call(*args)
        except sqlite3.Error as exc:
            logging.getLogger(__name__).warning("call audit unavailable: %s", type(exc).__name__)

    def check_account(self, account_id, *, force=False, recovery=False):
        from .health_checks import cached, probe_key

        account = self.account(account_id)
        if not recovery and account.get("outbound_block"):
            from .account_blocks import blocked_health
            from .health_checks import category

            stopped = blocked_health(account["outbound_block"])
            return {
                **account,
                "status": "unavailable",
                "connection_state": category(stopped["reason"]),
                "writable": False,
                "health": {**account["health"], **stopped, "probe_cached": True},
            }
        if account["disabled"] or account["cooldown_until"] > self.clock():
            return account
        if not recovery and cached(
            account, self.clock(), probe_key(account, self.store.path), force=force
        ):
            return {**account, "health": {**account["health"], "probe_cached": True}}
        try:
            route = route_for(account)
            with self.lanes.acquire(account_id, resource=route.resource):
                return self._check_account_locked(account_id, force=force, recovery=recovery)
        except AccountBusy:
            current = self.account(account_id)
            return {
                **current,
                "writable": False,
                "connection_state": "busy",
                "health": {**current["health"], "reason": "AccountBusy"},
            }
        except sqlite3.Error:
            # A local database preparation failure is not an authorization loss.
            # No retry of a provider call, and no attempt to write into a busy DB.
            return {
                **account,
                "writable": False,
                "connection_state": "temporary_unavailable",
                "health": {**account["health"], "reason": "LocalDatabaseUnavailable"},
            }

    def _check_account_locked(self, account_id, *, force=False, recovery=False):
        from .account_checks import normalize_health
        from .health_checks import cached, category, probe_key, probe_revision

        account = self.account(account_id)
        now = self.clock()
        if account["disabled"] or account["cooldown_until"] > now:
            return account
        revision = probe_revision(account, self.store.path)
        key = probe_key(account, self.store.path, revision=revision)
        previous = account["health"]
        if not recovery and cached(account, now, key, force=force):
            return {**account, "health": {**previous, "probe_cached": True}}
        try:
            payload = None
            if recovery and account.get("outbound_block"):
                payload = {
                    "account_block_recovery_generation": account["outbound_block"]["generation"]
                }
            result = (
                self.call(account, "health", payload)
                if payload
                else self.call(account, "health")
            )
        except AccountBusy:
            return {**account, "writable": False, "connection_state": "busy"}
        except Conflict as exc:
            result = {
                "healthy": False,
                "reason": safe_code(getattr(exc, "safe_code", None), "connection_unavailable"),
            }
        if result.get("reason") == "AccountBusy":
            return {
                **account,
                "writable": False,
                "connection_state": "busy",
                "health": {**account["health"], "reason": "AccountBusy"},
            }
        result = normalize_health(result)
        # Health results are an allowlist; raw provider responses are not persisted.
        health = {
            key: result[key]
            for key in (
                "healthy",
                "service_ready",
                "identity_verified",
                "permissions",
                "reason",
                "execution_step",
                "exception_type",
            )
            if key in result
        }
        for field in ("reason", "execution_step", "exception_type"):
            if field in health:
                health[field] = safe_code(health[field], "unverified")
        observed_identity = result.get("account_identity")
        with self.store.connect(write=True) as db:
            current = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if (
                not current
                or (current["disabled"] and not result.get("risk_warning"))
                or (current["cooldown_until"] > self.clock() and not result.get("account_retry_at"))
                or probe_key(dict(current), self.store.path) != key
            ):
                # An old result cannot verify a changed connection/configuration.
                observed = self.account(account_id)
                return {
                    **observed,
                    "writable": False,
                    "connection_state": "unverified",
                    "health": {**observed["health"], "reason": "AccountConfigurationChanged"},
                }
            identity_hash = current["identity_hash"]
            if health.get("identity_verified") and observed_identity:
                observed_hash = digest([account["platform"], str(observed_identity)])
                if identity_hash and identity_hash != observed_hash:
                    health.update(
                        healthy=False, identity_verified=False, reason="AccountIdentityChanged"
                    )
                elif not identity_hash:
                    other = db.execute(
                        "SELECT id FROM accounts WHERE platform=? AND mode=? AND identity_hash=? AND id<>?",
                        (account["platform"], account["mode"], observed_hash, account_id),
                    ).fetchone()
                    if other:
                        health.update(
                            healthy=False,
                            identity_verified=False,
                            reason="DuplicatePlatformIdentity",
                        )
                    else:
                        identity_hash = observed_hash
            elif identity_hash and health.get("identity_verified"):
                health.update(
                    healthy=False, identity_verified=False, reason="AccountIdentityUnavailable"
                )
            status = (
                "healthy"
                if health.get("healthy") and health.get("identity_verified")
                else "unavailable"
            )
            failures = (
                0
                if status == "healthy"
                else min(5, int(previous.get("consecutive_failures", 0)) + 1)
            )
            health.update(
                probe_state="healthy" if status == "healthy" else category(health.get("reason")),
                consecutive_failures=failures,
                next_check_at=now
                + (15 if status == "healthy" else min(300, 30 * 2 ** (failures - 1))),
                # Bind the returned identity to the ORIGINAL connection revision,
                # never relabel an old result with freshly read metadata.
                probe_key=probe_key(
                    {**account, "identity_hash": identity_hash}, self.store.path, revision=revision
                ),
            )
            if status == "healthy":
                health["last_verified_at"] = now
            elif previous.get("last_verified_at"):
                health["last_verified_at"] = previous["last_verified_at"]
            if probe_revision(account, self.store.path) != revision:
                return {
                    **self.account(account_id),
                    "writable": False,
                    "connection_state": "unverified",
                    "health": {**previous, "reason": "AccountConfigurationChanged"},
                }
            db.execute(
                "UPDATE accounts SET status=?,health=?,checked_at=?,identity_hash=? WHERE id=?",
                (status, canonical(health), self.clock(), identity_hash, account_id),
            )
            if result.get("rate_limited") or result.get("risk_warning"):
                db.execute(
                    "UPDATE accounts SET cooldown_until=max(cooldown_until,?),disabled=max(disabled,?) WHERE id=?",
                    (
                        result.get("account_retry_at")
                        or self.clock() + max(1, int(result.get("retry_after_seconds") or 3600)),
                        int(bool(result.get("risk_warning"))),
                        account_id,
                    ),
                )
            Store.audit(
                db,
                "account.checked",
                {
                    "account_id": account_id,
                    "status": status,
                    "connection_key": health["probe_key"],
                    "probe": "performed",
                    "reason": safe_code(health.get("reason"), "unverified"),
                    "probe_state": health["probe_state"],
                    "next_check_at": health["next_check_at"],
                },
            )
        return self.account(account_id)

    def telegram_read_policy(self, account_id):
        from .telegram_reading import read_policy_context

        account = self.account(account_id)
        if account["platform"] != "telegram":
            raise Conflict("读取设置仅适用于 Telegram 账号")
        return {
            "account_id": account_id,
            **read_policy_context({"database": self.store.path, "account_id": account_id}),
        }

    def configure_telegram_reads(self, account_id, value, *, expected_revision=None):
        self.telegram_read_policy(account_id)
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT revision FROM telegram_read_settings WHERE account_id=?", (account_id,)
            ).fetchone()
            revision = row[0] if row else 0
            if expected_revision != revision and not (expected_revision is None and revision == 0):
                raise Conflict("读取设置已变化，请刷新后重试")
            db.execute(
                "INSERT INTO telegram_read_settings VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                "payload=excluded.payload,revision=excluded.revision,updated_at=excluded.updated_at",
                (account_id, canonical(value.model_dump()), revision + 1, self.clock()),
            )
            Store.audit(
                db,
                "telegram.read_settings_updated",
                {"account_id": account_id, "revision": revision + 1},
                now=self.clock(),
            )
        return self.telegram_read_policy(account_id)

    def check_account_block(self, account_id):
        """Explicit read-only probe; does not clear a stop or modify any item."""
        from .account_blocks import active_block

        account = self.account(account_id)
        with self.lanes.acquire(account_id, resource=route_for(account, {}).resource):
            with self.store.connect() as db:
                block = active_block(db, account_id, self.clock())
            if not block:
                raise Conflict("账号没有待处理的外发暂停")
            account = self.account(account_id)
            original_identity = account["identity_hash"]
            checked = self.check_account(account_id, force=True, recovery=True)
            healthy = bool(
                original_identity
                and checked["identity_hash"] == original_identity
                and checked["connection_state"] == "healthy"
                and checked["health"].get("identity_verified")
                and not checked["disabled"]
                and checked["cooldown_until"] <= self.clock()
            )
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE account_blocks SET health_checked_at=?,health_ok=?,health_identity_hash=? "
                    "WHERE account_id=? AND state='active' AND generation=?",
                    (
                        self.clock(),
                        int(healthy),
                        original_identity,
                        account_id,
                        block["generation"],
                    ),
                )
        return self.account(account_id)

    def resume_account_block(self, account_id, generation):
        """A fresh matching probe and explicit generation are mandatory."""
        from .account_blocks import active_block
        from .health_checks import probe_key

        account = self.account(account_id)
        with self.lanes.acquire(account_id, resource=route_for(account, {}).resource):
            now = self.clock()
            with self.store.connect(write=True) as db:
                block = active_block(db, account_id, now)
                row = db.execute(
                    "SELECT * FROM account_blocks WHERE account_id=?", (account_id,)
                ).fetchone()
                current = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
                if (
                    not block
                    or str(block["generation"]) != str(generation)
                    or not row["health_ok"]
                    or row["health_checked_at"] is None
                    or not 0 <= now - row["health_checked_at"] <= 300
                    or not row["health_identity_hash"]
                    or row["health_identity_hash"] != current["identity_hash"]
                    or current["disabled"]
                    or current["cooldown_until"] > now
                    or current["status"] != "healthy"
                    or json.loads(current["health"]).get("probe_key")
                    != probe_key(dict(current), self.store.path)
                ):
                    raise Conflict("请先完成当前账号身份健康检查，再显式恢复")
                db.execute(
                    "UPDATE account_blocks SET state='cleared',generation=generation+1,updated_at=? WHERE account_id=?",
                    (now, account_id),
                )
                db.execute("DELETE FROM account_block_events WHERE account_id=?", (account_id,))
                Store.audit(
                    db,
                    "account.outbound_resumed",
                    {"account_id": account_id, "generation": row["generation"] + 1},
                    now=now,
                )
        return self.account(account_id)

    def disable_account(self, account_id, disabled):
        self.account(account_id)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET disabled=? WHERE id=?", (int(disabled), account_id))
            Store.audit(db, "account.disabled", {"account_id": account_id, "disabled": disabled})
        return self.account(account_id)

    def account_targets(self, account_id):
        account = self.account(account_id)
        if account["platform"] != "discord":
            raise Conflict("此平台暂不提供频道目录")
        if account["cooldown_until"] > self.clock():
            raise Conflict("账号仍在冷却，请稍后读取频道")
        result = self.call(account, "targets")
        self.apply_cooldown(account_id, result)
        if result.get("rate_limited"):
            raise Conflict("Discord 请求受到限流，已记录冷却时间")
        return {"targets": result.get("targets", []), "truncated": bool(result.get("truncated"))}

    def apply_cooldown(self, account_id, result):
        if result.get("rate_limited") or result.get("risk_warning"):
            until = result.get("account_retry_at") or self.clock() + max(
                1, int(result.get("retry_after_seconds") or 60)
            )
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE accounts SET cooldown_until=max(cooldown_until,?) WHERE id=?",
                    (until, account_id),
                )
                if result.get("risk_warning"):
                    db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (account_id,))
                Store.audit(db, "account.cooldown", {"account_id": account_id, "until": until})

    def create_task(self, value: TaskInput, *, interaction_claim=None):
        if len(set(value.account_ids)) != len(value.account_ids):
            raise Conflict("任务账号不能重复")
        accounts = [self.account(a) for a in value.account_ids]
        if any(a["health"].get("reason") == "DuplicatePlatformIdentity" for a in accounts):
            raise Conflict("任务包含重复绑定的平台身份，请使用已核验的原账号")
        if len({a["mode"] for a in accounts}) != 1:
            raise Conflict("模拟账号和真实账号必须分开创建任务")
        if accounts[0]["mode"] == "live" and value.policy.min_interval < 120:
            raise Conflict("真实任务最小发送间隔为 120 秒")
        if value.policy.max_joins and not any(a["platform"] == "telegram" for a in accounts):
            raise Conflict("只有 Telegram 支持入群")
        if value.policy.allow_owned_x_posts and any(a["platform"] != "x" for a in accounts):
            raise Conflict("自有 X 原创发布任务只能选择 X 账号")
        if (
            value.policy.allow_owned_x_posts
            and accounts[0]["mode"] == "live"
            and value.policy.execution_channel != "cloud"
        ):
            raise Conflict("X 原创发布需要明确选择在线队列")
        if value.policy.outbound_after_task_ids:
            if len(accounts) != 1 or not value.policy.allow_owned_x_posts:
                raise Conflict("完成互动后发布需要一个账号及自有 X 原创发布选项")
            for source_id in value.policy.outbound_after_task_ids:
                source = self.store.one("SELECT * FROM tasks WHERE id=?", (source_id,))
                if (
                    not source
                    or source["mode"] != accounts[0]["mode"]
                    or accounts[0]["id"] not in json.loads(source["account_ids"])
                ):
                    raise Conflict("前置互动任务不存在、账号不匹配或混用了模拟与真实任务")
        snapshot = [
            {
                k: a[k]
                for k in (
                    "id",
                    "platform",
                    "label",
                    "backend_label",
                    "mode",
                    "daily_limit",
                    "hourly_limit",
                    "min_interval",
                )
            }
            for a in accounts
        ]
        policy = value.policy.model_dump()
        # Only the server can capture limits; callers cannot pre-authorize a future increase.
        policy["quota_snapshot"] = {"execution": self.execution_settings()}
        envelope = {"accounts": snapshot, "policy": policy}
        task_id = "AP-" + uuid.uuid4().hex[:14].upper()
        now = self.clock()
        with self.store.connect(write=True) as db:
            if interaction_claim:
                candidate = db.execute(
                    "SELECT * FROM hf_candidates WHERE id=?", (interaction_claim["candidate_id"],)
                ).fetchone()
                lease = db.execute(
                    "SELECT 1 FROM execution_jobs WHERE id=? AND owner=? AND state='leased' AND lease_until>?",
                    (interaction_claim["job_id"], interaction_claim["job_owner"], now),
                ).fetchone()
                if (
                    not lease
                    or not candidate
                    or candidate["version"] != interaction_claim["version"]
                    or candidate["state"] != "READY"
                ):
                    raise Conflict("interaction_sender_claim_stale")
                if candidate["task_id"]:
                    return self.task(candidate["task_id"])
            db.execute(
                "INSERT INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    value.name,
                    accounts[0]["mode"],
                    canonical(value.account_ids),
                    canonical(snapshot),
                    canonical(policy),
                    digest(envelope),
                    now,
                    now,
                ),
            )
            if interaction_claim:
                db.execute(
                    "UPDATE hf_candidates SET task_id=? WHERE id=?",
                    (task_id, interaction_claim["candidate_id"]),
                )
            Store.audit(db, "task.created", {"policy_hash": digest(envelope)}, task_id, now)
        return self.task(task_id)

    def task(self, task_id):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise NotFound("任务不存在")
        result = decode(row, "policy", "account_ids", "account_snapshot", "manifest")
        result["approvals"] = self.store.rows(
            "SELECT stage,hash,phrase,actor,approved_at FROM approvals WHERE task_id=? ORDER BY approved_at",
            (task_id,),
        )
        result["items"] = [
            decode(r, "payload", "result")
            for r in self.store.rows(
                "SELECT * FROM items WHERE task_id=? ORDER BY position", (task_id,)
            )
        ]
        result["candidates"] = [
            decode(r, "payload", "reasons", "result")
            for r in self.store.rows(
                "SELECT * FROM candidates WHERE task_id=? ORDER BY id", (task_id,)
            )
        ]
        result["summary"] = {
            s: sum(i["status"] == s for i in result["items"])
            for s in {i["status"] for i in result["items"]}
        }
        from .delivery_summary import summarize

        result["delivery_summary"] = summarize(result["items"], result["mode"])
        result["execution"] = Workers(self.store, self.clock).snapshot(
            task_id, live=result["mode"] == "live"
        )
        from .queue_store import JobQueue

        result["execution"]["pools"] = JobQueue(self).pools(task_id)
        result["readiness"] = self.readiness(result)
        from .task_channels import snapshot

        result["channel"] = snapshot(self, result)
        reply = self.store.one(
            "SELECT r.event_id,r.platform,r.account_id,i.payload FROM reply_inbox r "
            "JOIN items i ON i.id=r.item_id WHERE r.followup_task_id=? LIMIT 1",
            (task_id,),
        )
        if reply:
            message_id = reply["event_id"].rsplit(":", 1)[-1]
            result["reply_context"] = {
                "target": json.loads(reply["payload"])["target"],
                "account_id": reply["account_id"],
                "reply_to": int(message_id)
                if reply["platform"] == "telegram" and message_id.isdigit()
                else None,
                "discord_reply_to": message_id if reply["platform"] == "discord" else "",
            }
        return result

    def readiness(self, task):
        """Report every local prerequisite without checking a real account or sending."""
        from .account_checks import summary

        accounts = [summary(self.account(a), self.clock()) for a in task["account_ids"]]
        blockers = []
        if task["state"] != "active":
            blockers.append("task_not_active")
        if task["phase"] != "ready":
            blockers.append("task_not_started")
        if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
            blockers.append("global_stop")
        if task["mode"] == "live" and not self.live_enabled:
            blockers.append("live_writes_disabled")
        if self.clock() >= epoch(task["policy"]["ends_at"]):
            blockers.append("delivery_window_elapsed")
        items = []
        with self.store.connect() as db:
            raw_task = db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            _, dependency_reason = publication_schedule(db, raw_task, self.clock())
            for item in task["items"]:
                if item["status"] != "not_attempted":
                    continue
                from .interaction_intents import intent_blocker

                sender = db.execute(
                    "SELECT * FROM accounts WHERE id=?", (item["account_id"],)
                ).fetchone()
                blocked = (
                    intent_blocker(db, sender, raw_task, item["payload"], item["id"])
                    if sender
                    else None
                )
                from .account_blocks import active_block

                account_reason = None
                if sender:
                    if sender["disabled"]:
                        account_reason = "account_disabled"
                    elif active_block(db, sender["id"], self.clock()):
                        account_reason = "account_outbound_blocked"
                    elif sender["cooldown_until"] > self.clock():
                        account_reason = "account_cooldown"
                    elif sender["status"] != "healthy":
                        account_reason = "account_unhealthy"
                reason = (
                    account_reason
                    or dependency_reason
                    or (blocked["reason"] if blocked else None)
                    or telegram_blocker(db, raw_task, item["payload"], item["due_at"])
                )
                if reason:
                    items.append(
                        {
                            "item_id": item["id"],
                            "reason": reason,
                            "message": REASONS[reason],
                            **(blocked if blocked and reason == blocked["reason"] else {}),
                        }
                    )
        return {
            "accounts": accounts,
            "accounts_ready": all(a["ready"] for a in accounts),
            "blockers": [{"reason": r, "message": REASONS[r]} for r in blockers],
            "items": items,
            "workflow": task_workflow(task, blockers, items, self.clock()),
            "sender_status": task["execution"]["status"],
            "live_writes_enabled": self.live_enabled,
        }

    def _integrity(self, db, task, stage):
        policy = json.loads(task["policy"])
        snapshot = json.loads(task["account_snapshot"])
        if digest({"accounts": snapshot, "policy": policy}) != task["policy_hash"]:
            raise Conflict("发现政策已发生变化，请创建新任务")
        for saved in snapshot:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (saved["id"],)).fetchone()
            if row is None or any(row[k] != v for k, v in saved.items()):
                raise Conflict("账号配置已变化，请创建新任务")
        if stage == "outbound":
            expected = task["manifest_hash"]
            if not task["manifest"] or digest(json.loads(task["manifest"])) != expected:
                raise Conflict("外发清单哈希不匹配")
            rows = db.execute(
                "SELECT payload,payload_hash FROM items WHERE task_id=? ORDER BY position",
                (task["id"],),
            ).fetchall()
            payloads = [json.loads(r["payload"]) for r in rows]
            manifest = json.loads(task["manifest"])
            if payloads != manifest["items"] or any(
                digest(p) != r["payload_hash"] for p, r in zip(payloads, rows)
            ):
                raise Conflict("执行项与已启动清单不一致")
        return policy

    def _check_start_accounts(self, task, *, refresh=True):
        """Check before taking the task's write transaction or reserving any work."""
        from .account_checks import summary

        checks = []
        for account_id in task["account_ids"]:
            account = self.account(account_id)
            if account["mode"] == "live" or account["status"] == "unchecked":
                # Explicit starts refresh positive observations. Failed probes
                # retain their bounded backoff; cooldowns never trigger a probe.
                account = self.check_account(account_id, force=refresh)
            checks.append(summary(account, self.clock()))
        with self.store.connect(write=True) as db:
            Store.audit(db, "task.account_check", {"accounts": checks}, task["id"], self.clock())
        blocked = [c for c in checks if not c["ready"]]
        if blocked:
            raise Conflict(
                "启动前账号检查未通过："
                + "；".join(c["label"] + "：" + c["message"] for c in blocked)
            )

    def _validate_start_accounts(self, task):
        from .account_checks import summary

        for account_id in json.loads(task["account_ids"]):
            check = summary(self.account(account_id), self.clock())
            if not check["ready"]:
                raise Conflict("启动前账号状态已变化：" + check["label"] + "：" + check["message"])

    def _already_active_start(
        self, task_id, *, sending=True, source="client", include_starting=False
    ):
        """Idempotency is local state, never a cached platform authorization."""
        if source == "scheduler":
            return False
        with self.store.connect() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise NotFound("任务不存在")
            if task["state"] != "active" or not (
                task["phase"] == "ready"
                or include_starting
                and task["phase"] == "starting"
                or not sending
                and task["phase"] in {"discovered", "starting"}
            ):
                return False
            self._integrity(db, task, "outbound" if task["phase"] == "ready" else "discovery_join")
            from .health_checks import AUTH, probe_key

            local_task = {
                "account_ids": json.loads(task["account_ids"]),
                "policy": json.loads(task["policy"]),
            }
            for row in db.execute("SELECT payload FROM items WHERE task_id=?", (task_id,)):
                payload = {
                    key: value
                    for key, value in json.loads(row["payload"]).items()
                    if key not in {"position", "platform", "account_label"}
                }
                account = self.account(payload["account_id"])
                if account["health"].get("reason") in AUTH or (
                    account["mode"] == "live"
                    and account["health"].get("probe_key")
                    and account["health"]["probe_key"] != probe_key(account, self.store.path)
                ):
                    raise Conflict("账号身份或连接已变化，请重新核验")
                self.validate_item(local_task, ItemInput(**payload), account=account)
            if self.clock() >= epoch(json.loads(task["policy"])["ends_at"]):
                raise Conflict("任务执行窗口已过期，请创建新任务")
            if task["campaign_id"]:
                campaign = db.execute(
                    "SELECT state FROM campaigns WHERE id=?", (task["campaign_id"],)
                ).fetchone()
                if not campaign or campaign[0] in {"paused", "cancelled", "completed"}:
                    raise Conflict("活动已暂停或结束，请先恢复活动")
            return True

    def _duplicate_start_response(self, task_id):
        task = self.task(task_id)
        runtime = self.store.one("SELECT value FROM meta WHERE key='execution_runtime'")
        task["execution_result"] = {
            "joins": [],
            "processed": [],
            "already_started": True,
            "queued": bool(runtime and runtime["value"] == "pooled"),
        }
        return task

    def _start_stop_revision(self, task_id, db=None):
        query = "SELECT COALESCE(MAX(id),0) n FROM audit WHERE task_id=? AND kind IN ('task.paused','task.cancelled')"
        row = (
            db.execute(query, (task_id,)).fetchone()
            if db is not None
            else self.store.one(query, (task_id,))
        )
        return row[0] if db is not None else row["n"]

    def start_task(
        self,
        task_id,
        *,
        source="client",
        actor="operator",
        sending=True,
        _transition=None,
        _expected_stop_revision=None,
    ):
        """Persist an explicit start; no approval phrases or approval rows are required."""
        stop_revision = (
            self._start_stop_revision(task_id)
            if _expected_stop_revision is None
            else _expected_stop_revision
        )
        if self._already_active_start(task_id, sending=sending, source=source):
            if _transition is not None:
                _transition["changed"] = False
            return self.task(task_id)
        task = self.task(task_id)
        if task["state"] in {"cancelled", "completed"}:
            raise Conflict("任务已结束")
        if self.clock() >= epoch(task["policy"]["ends_at"]):
            raise Conflict("任务执行窗口已过期，请创建新任务")
        initial_items = [
            (item["id"], item["payload_hash"], digest(item["payload"])) for item in task["items"]
        ]
        initial_policy = task["policy_hash"]
        initial_manifest = task["manifest_hash"]
        for item in task["items"]:
            payload = {
                k: v
                for k, v in item["payload"].items()
                if k not in {"position", "platform", "account_label"}
            }
            self.validate_item(task, ItemInput(**payload))
        initial_state = task["state"]
        self._check_start_accounts(task, refresh=source != "scheduler")
        with self.store.connect(write=True) as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if self._start_stop_revision(task_id, db) != stop_revision or (
                task["state"] == "paused" and initial_state != "paused"
            ):
                raise Conflict("任务在开始请求期间已暂停或结束，请刷新后再操作")
            if source == "scheduler" and (task["state"] != "active" or task["phase"] != "starting"):
                raise Conflict("任务已暂停或启动状态已变化")
            if task["state"] in {"cancelled", "completed"}:
                raise Conflict("任务已结束")
            self._integrity(db, task, "discovery_join")
            current_items = [
                (row["id"], row["payload_hash"], digest(json.loads(row["payload"])))
                for row in db.execute(
                    "SELECT id,payload_hash,payload FROM items WHERE task_id=? ORDER BY position",
                    (task_id,),
                )
            ]
            if (
                current_items != initial_items
                or task["policy_hash"] != initial_policy
                or (initial_manifest and task["manifest_hash"] != initial_manifest)
            ):
                raise Conflict("开始请求期间清单或配置已变化，请刷新后再开始")
            self._validate_start_accounts(task)
            from .interaction_intents import reserve_task_intents

            reserve_task_intents(db, task, self.clock())
            if task["campaign_id"]:
                campaign = db.execute(
                    "SELECT state FROM campaigns WHERE id=?", (task["campaign_id"],)
                ).fetchone()
                if not campaign or campaign[0] in {"paused", "cancelled", "completed"}:
                    raise Conflict("活动已暂停或结束，请先恢复活动")
            if task["state"] == "active" and (
                task["phase"] == "ready"
                or (
                    task["phase"] == "starting"
                    and (not sending or _transition is not None and source != "scheduler")
                )
            ):
                if _transition is not None:
                    _transition["changed"] = False
                if task["phase"] == "ready":
                    self._integrity(db, task, "outbound")
            else:
                if _transition is not None:
                    _transition["changed"] = True
                if self.clock() >= epoch(json.loads(task["policy"])["ends_at"]):
                    raise Conflict("任务执行窗口已过期，请创建新任务")
                rows = db.execute(
                    "SELECT * FROM items WHERE task_id=? ORDER BY position", (task_id,)
                ).fetchall()
                phase = "discovered"
                dependency_missed = False
                if rows and sending:
                    _, dependency_reason = publication_schedule(db, task, self.clock())
                    if dependency_reason in {
                        "interaction_schedule_elapsed",
                        "interaction_schedule_outside_window",
                    }:
                        db.execute(
                            "UPDATE items SET status='missed',result=? WHERE task_id=? "
                            "AND status='not_attempted' AND attempt_id IS NULL",
                            (canonical({"reason": dependency_reason, "verified": False}), task_id),
                        )
                        phase, dependency_missed = "starting", True
                    elif dependency_reason or not self._execution_service_ready(db, task):
                        phase = "starting"
                    else:
                        self._resolve_task_schedule(db, task)
                        self._resolve_membership_schedule(db, task)
                        rows = db.execute(
                            "SELECT * FROM items WHERE task_id=? ORDER BY position", (task_id,)
                        ).fetchall()
                        self._validate_outbound_readiness(db, task, check_membership=False)
                        waiting = any(
                            telegram_blocker(db, task, json.loads(r["payload"]), r["due_at"])
                            for r in rows
                        )
                        if waiting:
                            phase = "starting"
                        else:
                            self._freeze_manifest(db, task)
                            phase = "ready"
                db.execute(
                    "UPDATE tasks SET state='active',phase=?,updated_at=? WHERE id=?",
                    (phase, self.clock(), task_id),
                )
                if dependency_missed:
                    db.execute("UPDATE tasks SET phase='ready' WHERE id=?", (task_id,))
                    self._mark_completed(db, task_id)
                if task["state"] != "active" or task["phase"] != phase:
                    current = db.execute(
                        "SELECT manifest_hash FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()
                    Store.audit(
                        db,
                        "task.started",
                        {
                            "source": source,
                            "actor": actor,
                            "phase": phase,
                            "policy_hash": task["policy_hash"],
                            "manifest_hash": current[0],
                        },
                        task_id,
                        self.clock(),
                    )
        return self.task(task_id)

    def _resolve_task_schedule(self, db, task):
        policy = json.loads(task["policy"])
        if not policy.get("outbound_after_task_ids") or task["manifest_hash"]:
            return
        due, reason = publication_schedule(db, task, self.clock())
        if reason:
            raise Conflict(REASONS[reason])
        rows = db.execute("SELECT * FROM items WHERE task_id=?", (task["id"],)).fetchall()
        if len(rows) != 1 or json.loads(rows[0]["payload"])["action"] != "post":
            raise Conflict("完成互动后发布的任务需要且只允许一条原创推文")
        row = rows[0]
        if row["attempt_id"] or row["status"] != "not_attempted":
            raise Conflict("已尝试的项目不能重新计算发布时间")
        if row["due_at"] == due:
            return
        payload = json.loads(row["payload"])
        payload["scheduled_at"] = datetime.fromtimestamp(due, timezone.utc).isoformat()
        db.execute(
            "UPDATE items SET payload=?,payload_hash=?,due_at=? WHERE id=?",
            (canonical(payload), digest(payload), due, row["id"]),
        )
        Store.audit(
            db,
            "item.interaction_schedule_resolved",
            {
                "item_id": row["id"],
                "source_task_ids": policy["outbound_after_task_ids"],
                "completed_at": due - policy["outbound_after_tasks_seconds"],
                "due_at": due,
            },
            task["id"],
            self.clock(),
        )

    def _execution_service_ready(self, db, task):
        policy = json.loads(task["policy"])
        if policy.get("outbound_after_service_recovery_seconds") is None:
            return True
        required = db.execute(
            "SELECT value FROM meta WHERE key='cloud_control_required'"
        ).fetchone()
        if not required or required[0] != "1":
            return True
        health = db.execute("SELECT value FROM meta WHERE key='cloud_control_health'").fetchone()
        return bool(health and json.loads(health[0]).get("state") == "connected")

    def _resolve_membership_schedule(self, db, task):
        """Resolve an explicitly requested relative time before freezing the manifest."""
        policy = json.loads(task["policy"])
        delay = policy.get("outbound_after_membership_seconds")
        if delay is None or task["manifest_hash"]:
            return
        previous_due = None
        last_by_account = {}
        for item in db.execute(
            "SELECT * FROM items WHERE task_id=? ORDER BY position", (task["id"],)
        ).fetchall():
            payload = json.loads(item["payload"])
            if payload["platform"] != "telegram" or item["attempt_id"]:
                continue
            candidate = db.execute(
                "SELECT * FROM candidates WHERE task_id=? AND account_id=? AND lower(target)=lower(?) "
                "ORDER BY CASE WHEN decision='qualified' THEN 1 ELSE 0 END,rowid DESC LIMIT 1",
                (task["id"], payload["account_id"], payload["target"]),
            ).fetchone()
            if not candidate and not policy["max_joins"]:
                candidate = db.execute(
                    "SELECT * FROM candidates WHERE account_id=? AND lower(target)=lower(?) "
                    "ORDER BY rowid DESC LIMIT 1",
                    (payload["account_id"], payload["target"]),
                ).fetchone()
            if not candidate or candidate["state"] not in {"joined", "already_joined"}:
                continue
            receipt = json.loads(candidate["result"])
            if (
                receipt.get("verified") is not True
                or not receipt.get("external_reference")
                or receipt.get("can_send") is not True
            ):
                continue
            try:
                # Backend receipts already include the mandatory first 3600 seconds.
                eligible = epoch(receipt["outbound_eligible_at_utc"]) + delay - 3600
            except (KeyError, TypeError, ValueError):
                continue
            due = max(item["due_at"], eligible, epoch(policy["starts_at"]), self.clock() + 30)
            account = db.execute(
                "SELECT min_interval,cooldown_until,next_at FROM accounts WHERE id=?",
                (item["account_id"],),
            ).fetchone()
            recovery_delay = policy.get("outbound_after_service_recovery_seconds")
            if recovery_delay is not None:
                base = max(
                    self.clock(),
                    epoch(policy["starts_at"]),
                    account["cooldown_until"],
                    account["next_at"],
                )
                due = max(due, base + recovery_delay)
            gap = max(policy["min_interval"], account["min_interval"])
            if previous_due is not None:
                due = max(
                    due, previous_due + (gap if policy.get("pacing_scope", "task") == "task" else 0)
                )
            if item["account_id"] in last_by_account:
                due = max(due, last_by_account[item["account_id"]] + gap)
            previous_due, last_by_account[item["account_id"]] = due, due
            if due == item["due_at"]:
                continue
            payload["scheduled_at"] = datetime.fromtimestamp(due, timezone.utc).isoformat()
            db.execute(
                "UPDATE items SET payload=?,payload_hash=?,due_at=? WHERE id=? AND status='not_attempted' AND attempt_id IS NULL",
                (canonical(payload), digest(payload), due, item["id"]),
            )
            Store.audit(
                db,
                "item.membership_schedule_resolved",
                {
                    "item_id": item["id"],
                    "candidate_id": candidate["id"],
                    "previous_due_at": item["due_at"],
                    "due_at": due,
                    "delay_seconds": delay,
                },
                task["id"],
                self.clock(),
            )

    def _resume_membership_dependents(self, account_id, target):
        """Continue separately started groups without waiting for the rest of a batch."""
        tasks = self.store.rows(
            "SELECT DISTINCT t.id FROM tasks t JOIN items i ON i.task_id=t.id "
            "WHERE t.state='active' AND t.phase='starting' AND i.account_id=? "
            "AND lower(json_extract(i.payload,'$.target'))=lower(?) "
            "AND json_extract(t.policy,'$.outbound_after_membership_seconds') IS NOT NULL",
            (account_id, target),
        )
        for task in tasks:
            try:
                self.start_task(task["id"], source="scheduler")
            except Conflict:
                # Write permissions, stop and timing gates remain in force.
                pass

    def start_and_run(self, task_id, *, source="client"):
        """Shared client/Codex entry point for an explicit user execution instruction."""
        from .campaigns import Campaigns
        from .workers import WorkerRun

        stop_revision = self._start_stop_revision(task_id)
        if self._already_active_start(task_id, source=source, include_starting=True):
            return self._duplicate_start_response(task_id)
        task = self.task(task_id)
        if task.get("campaign_id"):
            Campaigns(self).populate(task_id)
        transition = {}
        self.start_task(
            task_id, source=source, _transition=transition, _expected_stop_revision=stop_revision
        )
        if transition.get("changed") is False:
            return self._duplicate_start_response(task_id)
        runtime = self.store.one("SELECT value FROM meta WHERE key='execution_runtime'")
        if runtime and runtime["value"] == "pooled":
            from .queue_store import JobQueue

            JobQueue(self).sync(task_id)
            task = self.task(task_id)
            task["execution_result"] = {"joins": [], "processed": [], "queued": True}
            return task
        with WorkerRun(self.store, task_id, live_enabled=self.live_enabled) as sender:
            sender.phase("executing")
            joined = self.run_joins(task_id)
            current = self.task(task_id)
            if current["phase"] == "starting" and current["state"] == "active":
                self.start_task(task_id, source="scheduler")
            result = self.run_once(task_id)
        task = self.task(task_id)
        task["execution_result"] = {"joins": joined["processed"], **result}
        return task

    def validate_item(self, task, value: ItemInput, *, account=None, opportunity=None):
        if value.account_id not in task["account_ids"]:
            raise Conflict("账号不在该任务范围内")
        account = account or self.account(value.account_id)
        if (
            account["mode"] == "live"
            and (
                account["platform"] == "reddit"
                or (
                    account["platform"] == "x"
                    and not automatic_action_allowed(account, value.action, task["policy"])
                )
            )
            and value.execution_mode != "manual"
        ):
            raise Conflict("此平台当前仅开放人工互动清单，请选择人工执行方式")
        if value.opportunity_id:
            from .planner import content_key, execution_mode

            source = opportunity or self.store.one(
                "SELECT * FROM opportunities WHERE id=?", (value.opportunity_id,)
            )
            if not source or source["platform"] != account["platform"]:
                raise Conflict("共享内容不存在或平台不匹配")
            original = (
                source["payload"]
                if isinstance(source["payload"], dict)
                else json.loads(source["payload"])
            )
            if (
                account["platform"] in {"telegram", "discord"}
                and value.target != original["target"]
            ):
                raise Conflict("执行目标与共享内容不一致")
            observed = {
                **original,
                "target": value.target,
                "reply_to": value.reply_to,
                "discord_reply_to": value.discord_reply_to,
                "tiktok_video_id": value.tiktok_video_id,
                "tiktok_comment_id": value.tiktok_comment_id,
            }
            if (
                value.content_key != source["content_key"]
                or content_key(observed) != value.content_key
            ):
                raise Conflict("执行目标与共享内容不一致")
            mode, reason = execution_mode(account, original)
            if mode == "disabled" or value.execution_mode != mode:
                raise Conflict(reason)
        if value.action not in CAPABILITIES[account["platform"]] or value.action in {
            "discover",
            "join",
        }:
            raise Conflict("该平台不支持此外发动作")
        policy = task["policy"]
        if not epoch(policy["starts_at"]) <= epoch(value.scheduled_at) < epoch(policy["ends_at"]):
            raise Conflict("计划时间超出任务执行窗口")
        if policy["target_allowlist"] and value.target not in policy["target_allowlist"]:
            raise Conflict("目标不在任务的目标边界内")
        if RISK.search(value.target):
            raise Conflict("目标包含被禁止的类别信号")
        no_text_actions = {"like", "repost"}
        if account["platform"] == "tiktok":
            from .backends.tiktok import NO_TEXT_ACTIONS

            no_text_actions |= NO_TEXT_ACTIONS
        if value.action not in no_text_actions and not (value.text.strip() or value.media):
            raise Conflict("此动作需要正文或媒体")
        if value.action in {"like", "repost"} and (value.text or value.media or value.title):
            raise Conflict("点赞和转发不能附带正文或媒体")
        platform = account["platform"]
        if platform != "tiktok" and (
            value.tiktok_video_id or value.tiktok_comment_id or value.tiktok_options
        ):
            raise Conflict("TikTok 字段仅用于 TikTok")
        if platform == "tiktok":
            from .backends.common import BackendError
            from .backends.tiktok import validate_item

            try:
                validate_item(value.model_dump())
            except (BackendError, ValueError) as exc:
                raise Conflict(getattr(exc, "safe_code", "TikTok 发布选项无效")) from None
            for entry in value.media:
                self.verify_media(entry.model_dump())
                ceiling = 20 * 1024 * 1024 if entry.media_type == "IMAGE" else 1024 * 1024 * 1024
                if Path(entry.path).stat().st_size > ceiling:
                    raise Conflict("TikTok 图片最多 20 MiB，视频最多 1 GiB；网页上传仍最多 50 MiB")
        if platform != "discord" and value.discord_reply_to:
            raise Conflict("Discord 原消息 ID 仅用于 Discord")
        if platform == "discord":
            from .backends.common import BackendError
            from .backends.discord import MAX_FILE_BYTES, MAX_TOTAL_BYTES, snowflake

            try:
                snowflake(value.target)
                if value.action == "reply":
                    snowflake(value.discord_reply_to)
            except BackendError:
                raise Conflict("Discord 需要有效的频道 ID；回复还需要原消息 ID") from None
            if value.reply_to or value.title or value.recipient_id:
                raise Conflict("Discord 请使用频道 ID 和 Discord 原消息 ID，不支持标题或私信")
            if len(value.text) > 2000:
                raise Conflict("Discord 正文最多 2000 字符")
            if value.action == "send_file" and not value.media:
                raise Conflict("Discord 发送文件需要媒体")
            if value.action == "send_message" and value.media:
                raise Conflict("请使用 Discord 发送文件或回复动作上传媒体")
            if value.action != "reply" and value.discord_reply_to:
                raise Conflict("Discord 原消息 ID 仅用于回复")
            total = 0
            for media in value.media:
                self.verify_media(media.model_dump())
                size = Path(media.path).stat().st_size
                total += size
                if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise Conflict("Discord 单文件最多 10 MiB，一条消息附件合计最多 25 MiB")
                if any(c in media.filename for c in '\r\n"\\'):
                    raise Conflict("Discord 文件名不能包含换行、引号或反斜杠")
        if platform == "x":
            parsed = urlparse(value.target)
            if value.action == "post":
                identity = urlparse(account.get("owned_target_url", "")).path.lstrip("/")
                if (
                    not policy.get("allow_owned_x_posts")
                    or not value.target_owned
                    or not owned_x_target(value.target, identity)
                ):
                    raise Conflict("X 原创发布需要本任务明确启用，并使用当前账号的自有主页链接")
                if value.title or value.reply_to or value.recipient_id:
                    raise Conflict("X 原创推文不使用标题、回复或收件人字段")
                if any(m.mime_type not in {"image/png", "image/jpeg"} for m in value.media):
                    raise Conflict("X 原创配图仅支持 PNG 或 JPEG")
            elif (
                parsed.scheme != "https"
                or parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
                or parsed.netloc != parsed.hostname
                or parsed.query
                or parsed.fragment
                or parsed.params
                or not re.fullmatch(r"/(?:[A-Za-z0-9_]+|i/web)/status/[0-9]+/?", parsed.path)
            ):
                raise Conflict("X 目标必须是明确的帖子链接")
            if (
                (x_post_weight(value.text) if value.action == "post" else len(value.text)) > 280
                or len(value.media) > 4
                or (value.media and value.action not in {"reply", "post"})
            ):
                raise Conflict("X 正文最多 280 加权字符；回复与原创推文最多 4 个媒体")
        if platform == "reddit":
            if value.media:
                raise Conflict("Reddit 当前执行器仅支持文字")
            if value.action == "post" and (
                not value.title.strip()
                or not re.fullmatch(r"(?:r/)?[A-Za-z0-9_]{2,21}", value.target)
            ):
                raise Conflict("Reddit 帖子需要标题和社区名称")
            if value.action == "reply":
                u = urlparse(value.target)
                if (
                    u.scheme != "https"
                    or u.hostname not in {"reddit.com", "www.reddit.com", "old.reddit.com"}
                    or "/comments/" not in u.path
                ):
                    raise Conflict("Reddit 回复需要帖子链接")
        if platform == "instagram":
            if value.action == "publish" and (not value.target_owned or not value.media):
                raise Conflict("Instagram 发布必须使用自有账号并提供媒体")
            if value.action == "manual_comment" and (value.media or not value.text.strip()):
                raise Conflict("人工评论需要精确正文，不支持媒体")
            if (
                value.action == "publish"
                and account["mode"] == "live"
                and any(
                    not m.source_url or not urlparse(m.source_url).hostname for m in value.media
                )
            ):
                raise Conflict("Instagram 媒体需要可访问的 HTTPS 来源地址")
        if platform == "telegram":
            if value.action == "send_text":
                if (
                    value.reply_to
                    or value.media
                    or not value.text.strip()
                    or len(value.text) > 4096
                ):
                    raise Conflict("Explicit Telegram text cannot contain a reply or media")
                if policy.get("execution_channel") != "cloud":
                    raise Conflict("Explicit Telegram text requires the cloud device workflow")
            if value.action in {"send_file", "schedule_file"} and len(value.media) != 1:
                raise Conflict("此 Telegram 动作需要一个媒体文件")
            if value.action in {"send_message", "schedule_message", "reply"} and value.media:
                raise Conflict("请选择发送文件动作")
            if value.action == "reply" and not value.reply_to:
                raise Conflict("Telegram 回复需要原消息 ID")
        for media in value.media:
            self.verify_media(media.model_dump())
        return account

    def _validate_outbound_readiness(self, db, task, *, check_membership=True):
        now = self.clock()
        if now >= epoch(json.loads(task["policy"])["ends_at"]):
            raise Conflict("任务执行窗口已过期，请创建新任务")
        for row in db.execute(
            "SELECT * FROM items WHERE task_id=? ORDER BY position", (task["id"],)
        ):
            payload = json.loads(row["payload"])
            native = payload["action"] in {"schedule_message", "schedule_file"}
            if now - row["due_at"] > DELIVERY_GRACE_SECONDS or (native and row["due_at"] <= now):
                raise Conflict("清单包含已过期的计划时间，请创建新任务")
            reason = (
                telegram_blocker(db, task, payload, row["due_at"]) if check_membership else None
            )
            if reason:
                raise Conflict(f"{payload['target']}：{REASONS[reason]}")

    def verify_media(self, media):
        if Path(media["filename"]).name != media["filename"]:
            raise Conflict("媒体文件名不能包含目录")
        if not media["path"]:
            raise Conflict("请先上传媒体并绑定文件哈希")
        path = Path(media["path"]).resolve()
        if not path.is_relative_to(self.media_root) or not path.is_file():
            raise Conflict("媒体不在项目允许目录中")
        if hashlib.sha256(path.read_bytes()).hexdigest() != media["sha256"]:
            raise Conflict("媒体内容已变化，请创建新任务")
        if media.get("source_url") and urlparse(media["source_url"]).scheme != "https":
            raise Conflict("媒体来源地址必须使用 HTTPS")

    def set_items(self, task_id, values):
        task = self.task(task_id)
        if not values or len(values) > 200:
            raise Conflict("每批需要 1–200 个执行项")
        if task["policy"].get("user_daily_limit") == 3 and (
            len(values) != 1 or values[0].action != "post" or not values[0].target_owned
        ):
            raise Conflict("三次尝试额度需要且只允许一条自有账号原创推文")
        if task["policy"].get("outbound_after_task_ids") and (
            len(values) != 1 or values[0].action != "post"
        ):
            raise Conflict("完成互动后发布的任务需要且只允许一条原创推文")
        previous_due = 0
        last_by_account = {}
        payloads = []
        for index, value in enumerate(values):
            account = self.validate_item(task, value)
            due = epoch(value.scheduled_at)
            if due < previous_due:
                raise Conflict("执行项必须按计划时间排序")
            if (
                index
                and task["policy"].get("pacing_scope", "task") == "task"
                and due - previous_due < task["policy"]["min_interval"]
            ):
                raise Conflict("执行项之间短于任务最小间隔")
            gap = max(task["policy"]["min_interval"], account["min_interval"])
            if (
                value.account_id in last_by_account
                and due - last_by_account[value.account_id] < gap
            ):
                raise Conflict("计划间隔短于账号或任务限额")
            previous_due = due
            last_by_account[value.account_id] = due
            payloads.append(
                {
                    **value.model_dump(),
                    "position": index,
                    "platform": account["platform"],
                    "account_label": account["label"],
                }
            )
        if len(
            {
                digest({k: v for k, v in p.items() if k not in {"position", "scheduled_at"}})
                for p in payloads
            }
        ) != len(payloads):
            raise Conflict("清单含重复动作")
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._integrity(db, row, "discovery_join")
            if (
                row["manifest_hash"]
                or row["phase"] == "starting"
                or row["state"] in {"cancelled", "completed"}
            ):
                raise Conflict("清单已经冻结；变更内容请创建新任务")
            if row["campaign_id"] and any(not p.get("opportunity_id") for p in payloads):
                raise Conflict("活动执行项必须关联已分配的共享内容")
            db.execute("UPDATE campaign_assignments SET item_id=NULL WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM items WHERE task_id=?", (task_id,))
            for payload in payloads:
                db.execute(
                    "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        "item-" + uuid.uuid4().hex[:16],
                        task_id,
                        payload["account_id"],
                        payload["position"],
                        canonical(payload),
                        digest(payload),
                        epoch(payload["scheduled_at"]),
                    ),
                )
                if payload.get("opportunity_id"):
                    assignment = db.execute(
                        "SELECT id,account_id FROM campaign_assignments WHERE task_id=? AND opportunity_id=?",
                        (task_id, payload["opportunity_id"]),
                    ).fetchone()
                    if not assignment or assignment["account_id"] != payload["account_id"]:
                        raise Conflict("执行项与活动分配不一致")
                    db.execute(
                        "UPDATE campaign_assignments SET item_id=(SELECT id FROM items WHERE task_id=? AND position=?) WHERE id=?",
                        (task_id, payload["position"], assignment["id"]),
                    )
            Store.audit(db, "items.saved", {"count": len(payloads)}, task_id, self.clock())
        return self.task(task_id)

    def prepare(self, task_id):
        task = self.task(task_id)
        if not task["items"]:
            raise Conflict("请先保存执行项")
        for item in task["items"]:
            payload = {
                k: v
                for k, v in item["payload"].items()
                if k not in {"position", "platform", "account_label"}
            }
            self.validate_item(task, ItemInput(**payload))
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._integrity(db, row, "discovery_join")
            if row["state"] in {"cancelled", "completed"}:
                raise Conflict("任务已结束")
            self._freeze_manifest(db, row)
        return self.task(task_id)

    def _freeze_manifest(self, db, row):
        if row["manifest_hash"]:
            self._integrity(db, row, "outbound")
            return
        self._resolve_task_schedule(db, row)
        self._validate_outbound_readiness(db, row)
        quotas.reserve(db, row)
        items = [
            json.loads(r["payload"])
            for r in db.execute(
                "SELECT payload FROM items WHERE task_id=? ORDER BY position", (row["id"],)
            )
        ]
        manifest = {
            "task_id": row["id"],
            "accounts": json.loads(row["account_snapshot"]),
            "policy": json.loads(row["policy"]),
            "items": items,
        }
        if row["campaign_id"]:
            manifest["campaign_id"] = row["campaign_id"]
            manifest["profiles"] = {
                r["account_id"]: json.loads(r["profile_snapshot"])
                for r in db.execute(
                    "SELECT account_id,profile_snapshot FROM campaign_assignments WHERE task_id=?",
                    (row["id"],),
                )
            }
        h = digest(manifest)
        db.execute(
            "UPDATE tasks SET manifest=?,manifest_hash=?,phase='review',updated_at=? WHERE id=?",
            (canonical(manifest), h, self.clock(), row["id"]),
        )
        Store.audit(
            db, "manifest.frozen", {"hash": h, "count": len(items)}, row["id"], self.clock()
        )

    def change_state(self, task_id, state):
        task = self.task(task_id)
        if state == "active":
            if task["state"] in {"cancelled", "completed"}:
                raise Conflict("已结束任务不可恢复")
            self._check_start_accounts(task)
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row["state"] in {"cancelled", "completed"}:
                raise Conflict("已结束任务不可恢复")
            if state == "active":
                self._integrity(db, row, "discovery_join")
                self._validate_start_accounts(row)
            db.execute(
                "UPDATE tasks SET state=?,updated_at=? WHERE id=?", (state, self.clock(), task_id)
            )
            if state == "cancelled":
                db.execute(
                    "UPDATE items SET status='cancelled' WHERE task_id=? AND status='not_attempted'",
                    (task_id,),
                )
                db.execute(
                    "DELETE FROM quota_reservations WHERE task_id=? AND state='reserved'",
                    (task_id,),
                )
                db.execute(
                    "DELETE FROM content_contacts WHERE assignment_id IN (SELECT ca.id FROM campaign_assignments ca LEFT JOIN items i ON i.id=ca.item_id WHERE ca.task_id=? AND (i.id IS NULL OR i.attempt_id IS NULL))",
                    (task_id,),
                )
            Store.audit(
                db,
                "task." + state,
                {"native_schedules": "already submitted schedules require platform reconciliation"},
                task_id,
                self.clock(),
            )
        return self.task(task_id)

    def set_stop(self, stopped):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta SET value=? WHERE key='global_stop'", ("1" if stopped else "0",)
            )
            Store.audit(db, "global_stop", {"stopped": stopped})
        from .cloud_control import queue_settings

        queue_settings(self, {"global_stop": stopped})
        return {
            "global_stop": stopped,
            "native_schedule_note": "已提交的平台排期不受本地停止开关撤回",
        }

    def recover(self, lease_seconds=300):
        from .final_receipts import binding, proof

        cutoff = self.clock() - lease_seconds
        # Private SQLite I/O must not hold the shared database's writer lock.
        proofs = []
        for row in self.store.rows(
            "SELECT * FROM items WHERE attempt_id IS NOT NULL AND "
            "((status='executing' AND claimed_at<?) OR status='unknown')",
            (cutoff,),
        ):
            evidence = proof(self.store, row)
            if evidence:
                proofs.append(evidence)
        with self.store.connect(write=True) as db:
            for claim, expected, result in proofs:
                account = db.execute(
                    "SELECT * FROM accounts WHERE id=?", (expected["account_id"],)
                ).fetchone()
                row = db.execute("SELECT * FROM items WHERE id=?", (claim["item_id"],)).fetchone()
                job = db.execute(
                    "SELECT * FROM execution_jobs WHERE id=?", (expected["job_id"],)
                ).fetchone()
                if (
                    not account
                    or not row
                    or not job
                    or row["status"] not in {"executing", "unknown"}
                ):
                    continue
                if job["state"] == "leased" and job["lease_until"] > self.clock():
                    continue
                if (
                    binding(
                        db,
                        claim,
                        account["id"],
                        account["platform"],
                        account["backend_label"],
                        job["id"],
                    )
                    != expected
                ):
                    continue
                self.finish(claim, result, _db=db)
                db.execute(
                    "UPDATE execution_jobs SET state=?,owner=NULL,lease_until=0,updated_at=? "
                    "WHERE id=?",
                    (result["status"], self.clock(), job["id"]),
                )
                Store.audit(
                    db,
                    "attempt.private_receipt_reconciled",
                    {
                        "item_id": claim["item_id"],
                        "attempt_id": claim["attempt_id"],
                        "hash_scope": expected["hash_scope"],
                    },
                    claim["task_id"],
                    self.clock(),
                )
            rows = db.execute(
                "SELECT id,task_id,attempt_id,result FROM items WHERE status='executing' AND claimed_at<? "
                "AND NOT EXISTS(SELECT 1 FROM execution_jobs j WHERE j.kind='send' "
                "AND j.reference_id=items.id AND j.state='leased' AND j.lease_until>?)",
                (cutoff, self.clock()),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE items SET status='unknown',result=? WHERE id=?",
                    (
                        canonical(
                            {
                                **json.loads(row["result"]),
                                "reason": "worker_lease_expired",
                                "status": "unknown",
                                "verified": False,
                            }
                        ),
                        row["id"],
                    ),
                )
                db.execute(
                    "UPDATE attempts SET outcome='unknown',finished_at=? WHERE id=?",
                    (self.clock(), row["attempt_id"]),
                )
                Store.audit(
                    db,
                    "attempt.recovered_unknown",
                    {"item_id": row["id"]},
                    row["task_id"],
                    self.clock(),
                )
            joins = db.execute(
                "SELECT id,candidate_id FROM join_attempts WHERE outcome='claimed' AND started_at<?",
                (self.clock() - lease_seconds,),
            ).fetchall()
            for row in joins:
                db.execute(
                    "UPDATE join_attempts SET outcome='unknown',finished_at=? WHERE id=?",
                    (self.clock(), row["id"]),
                )
                db.execute(
                    "UPDATE candidates SET state='unknown',result=? WHERE id=?",
                    (canonical({"reason": "worker_lease_expired"}), row["candidate_id"]),
                )
        return len(rows)

    def _skip(self, item_id, reason, db=None, *, candidate=False):
        if db is None:
            with self.store.connect(write=True) as connection:
                return self._skip(item_id, reason, connection, candidate=candidate)
        table, state = ("candidates", "state") if candidate else ("items", "status")
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if not row or row[state] != "not_attempted":
            return None
        result = json.loads(row["result"])
        if result.get("skip_reason") == reason:
            return None
        result.update(
            skip_reason=reason, skip_message=REASONS[reason], skip_recorded_at=self.clock()
        )
        db.execute(f"UPDATE {table} SET result=? WHERE id=?", (canonical(result), item_id))
        Store.audit(
            db,
            "candidate.skipped" if candidate else "item.skipped",
            {"candidate_id" if candidate else "item_id": item_id, "reason": reason},
            row["task_id"],
            self.clock(),
        )
        return None

    def _claim(self, item_id, backend_gate=None):
        now = self.clock()
        prepared = self.store.one("SELECT payload,payload_hash FROM items WHERE id=?", (item_id,))
        if not prepared:
            return None
        # File hashing can be slow. Do it without holding SQLite's only writer;
        # the provider checks the same frozen hashes again before submission.
        for media in json.loads(prepared["payload"])["media"]:
            self.verify_media(media)
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row or row["status"] != "not_attempted":
                return None
            if (
                row["payload_hash"] != prepared["payload_hash"]
                or row["payload"] != prepared["payload"]
            ):
                raise Conflict("执行项在检查期间发生变化")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if task["state"] != "active":
                return self._skip(item_id, "task_not_active", db)
            if task["campaign_id"]:
                campaign = db.execute(
                    "SELECT state FROM campaigns WHERE id=?", (task["campaign_id"],)
                ).fetchone()
                if not campaign or campaign[0] in {"paused", "cancelled"}:
                    return self._skip(item_id, "task_not_active", db)
            if task["phase"] != "ready":
                return self._skip(item_id, "task_not_started", db)
            policy = self._integrity(db, task, "outbound")
            if db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0] != "0":
                return self._skip(item_id, "global_stop", db)
            payload = json.loads(row["payload"])
            account = db.execute(
                "SELECT * FROM accounts WHERE id=?", (row["account_id"],)
            ).fetchone()
            account = dict(account)
            account["automatic_actions"] = automatic_actions(account)
            if (
                account["mode"] == "live"
                and (
                    account["platform"] == "reddit"
                    or (
                        account["platform"] == "x"
                        and not automatic_action_allowed(account, payload["action"], policy)
                    )
                )
                and payload.get("execution_mode", "automatic") != "manual"
            ):
                return self._skip(item_id, "platform_policy_blocked", db)
            if payload.get("opportunity_id"):
                from .planner import execution_mode

                source = db.execute(
                    "SELECT payload FROM opportunities WHERE id=?", (payload["opportunity_id"],)
                ).fetchone()
                if not source or execution_mode(dict(account), json.loads(source[0]))[
                    0
                ] != payload.get("execution_mode", "automatic"):
                    return self._skip(item_id, "target_cannot_send", db)
                content = json.loads(source[0])
                if content.get("expires_at") and now >= epoch(content["expires_at"]):
                    return self._skip(item_id, "delivery_window_elapsed", db)
            if account["disabled"]:
                return self._skip(item_id, "account_disabled", db)
            if account["status"] != "healthy":
                return self._skip(item_id, "account_unhealthy", db)
            from .account_blocks import active_block

            if active_block(db, account["id"], now):
                return self._skip(item_id, "account_outbound_blocked", db)
            if account["cooldown_until"] > now:
                return self._skip(item_id, "account_cooldown", db)
            if account["next_at"] > now or (
                policy.get("pacing_scope", "task") == "task" and task["next_at"] > now
            ):
                return self._skip(item_id, "pacing_wait", db)
            if (
                account["mode"] == "live"
                and payload.get("execution_mode", "automatic") == "automatic"
            ):
                if not self.live_enabled:
                    return self._skip(item_id, "live_writes_disabled", db)
                if not self.adapters.commissioned(account):
                    return self._skip(item_id, "backend_not_commissioned", db)
                if not backend_gate or backend_gate.get("allowed") is not True:
                    return self._skip(item_id, "backend_gate_blocked", db)
            # Native schedules are submitted ahead of delivery; other actions are claimed when due.
            native = payload["action"] in {"schedule_message", "schedule_file"}
            if now < epoch(policy["starts_at"]) or (not native and row["due_at"] > now):
                return self._skip(item_id, "scheduled_time_pending", db)
            if (
                now >= epoch(policy["ends_at"])
                or (
                    now - row["due_at"] > DELIVERY_GRACE_SECONDS
                    and not (row["phase"] == "starting" and policy.get("outbound_after_task_ids"))
                )
                or (native and row["due_at"] <= now)
            ):
                db.execute(
                    "UPDATE items SET status='missed',result=? WHERE id=?",
                    (canonical({"reason": "delivery_window_elapsed"}), item_id),
                )
                return None
            if policy.get("ordering_mode", "strict") == "independent_accounts":
                earlier = db.execute(
                    "SELECT status FROM items WHERE task_id=? AND account_id=? AND position<?",
                    (task["id"], account["id"], row["position"]),
                ).fetchall()
            else:
                earlier = db.execute(
                    "SELECT status FROM items WHERE task_id=? AND position<?",
                    (task["id"], row["position"]),
                ).fetchall()
            if any(r["status"] not in TERMINAL for r in earlier):
                return self._skip(item_id, "earlier_item_pending", db)
            target_key = digest([account["platform"], payload["target"].casefold()])
            user_key = digest(
                [account["platform"], payload["recipient_id"] or payload["target"].casefold()]
            )
            day = now - now % 86400
            mode = account["mode"]
            if backend_gate and "daily_remaining" in backend_gate:
                used = db.execute(
                    "SELECT count(*) FROM activity WHERE account_id=? AND started_at>=?",
                    (account["id"], day),
                ).fetchone()[0]
                if used >= int(backend_gate["daily_remaining"]):
                    return self._skip(item_id, "activity_limit", db)
            quota_reason = quotas.check_claim(
                db, account, task, payload, row["due_at"], now, item_id
            )
            if quota_reason:
                return self._skip(item_id, quota_reason, db)
            reason = telegram_blocker(db, task, payload, now)
            if reason:
                return self._skip(item_id, reason, db)
            from .interaction_intents import candidate_guard, claim_intent

            reason = candidate_guard(db, item_id, now)
            if not reason:
                reason = claim_intent(db, account, task, payload, item_id, now)
            if reason:
                return self._skip(item_id, reason, db)
            attempt = "attempt-" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at) VALUES(?,?,?,?,?,?,?,?)",
                (attempt, item_id, account["id"], task["id"], target_key, user_key, mode, now),
            )
            db.execute(
                "UPDATE items SET status='executing',attempt_id=?,claimed_at=?,result='{}' WHERE id=?",
                (attempt, now, item_id),
            )
            db.execute(
                "UPDATE hf_candidates SET state='PUBLISHING',verification_started_at=COALESCE(verification_started_at,?) WHERE item_id=?",
                (now, item_id),
            )
            db.execute("UPDATE quota_reservations SET state='consumed' WHERE item_id=?", (item_id,))
            db.execute(
                "UPDATE accounts SET next_at=? WHERE id=?",
                (now + max(account["min_interval"], policy["min_interval"]), account["id"]),
            )
            db.execute(
                "UPDATE tasks SET next_at=? WHERE id=?",
                (now + policy["min_interval"], task["id"]),
            )
            Store.audit(
                db, "attempt.claimed", {"item_id": item_id, "attempt_id": attempt}, task["id"], now
            )
            return {
                "item_id": item_id,
                "task_id": task["id"],
                "attempt_id": attempt,
                "payload": payload,
                "manifest_hash": task["manifest_hash"],
            }

    def _queue_pooled_task(self, task_id):
        runtime = self.store.one("SELECT value FROM meta WHERE key='execution_runtime'")
        if getattr(self.adapters.context, "job", None):
            return False
        explicit = False
        if task_id:
            task = self.store.one("SELECT policy,account_ids FROM tasks WHERE id=?", (task_id,))
            explicit = bool(
                task and json.loads(task["policy"]).get("execution_channel", "inherit") != "inherit"
            )
            if task and not explicit:
                accounts = json.loads(task["account_ids"])
                explicit = bool(
                    self.store.one(
                        "SELECT 1 FROM account_task_channels WHERE account_id IN ("
                        + ",".join("?" for _ in accounts)
                        + ") LIMIT 1",
                        tuple(accounts),
                    )
                )
        if not explicit and (not runtime or runtime["value"] != "pooled"):
            return False
        from .queue_store import JobQueue

        JobQueue(self).sync(task_id)
        return True

    def run_once(self, task_id=None):
        if self._queue_pooled_task(task_id):
            return {"processed": [], "queued": True}
        self.recover()
        if task_id:
            self.task(task_id)
        stopped = self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0"
        sql = "SELECT i.id,i.account_id,i.task_id,i.payload,i.due_at,t.policy,t.mode,t.next_at,t.state,t.phase FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.status='not_attempted' AND t.state NOT IN ('cancelled','completed')"
        if not getattr(self.adapters.context, "job", None):
            sql += " AND COALESCE(json_extract(t.policy,'$.execution_channel'),'inherit')='inherit'"
            sql += " AND NOT EXISTS(SELECT 1 FROM account_task_channels ch WHERE ch.account_id=i.account_id)"
        params = ()
        if task_id:
            sql += " AND i.task_id=?"
            params = (task_id,)
        rows = self._dispatchable_rows(
            self.store.rows(sql + " ORDER BY due_at,i.position", params), stopped
        )
        groups = {}
        strict = {}
        for row in rows:
            if json.loads(row["policy"]).get("ordering_mode", "strict") == "strict":
                strict.setdefault(row["task_id"], []).append(row)
            else:
                groups.setdefault(row["account_id"], []).append(row)
        limits = self.execution_settings()
        processed = []
        with ThreadPoolExecutor(
            max_workers=limits["max_workers"], thread_name_prefix="account-sender"
        ) as executor:
            futures = [
                executor.submit(self._run_account_group, group, stopped, limits)
                for group in groups.values()
            ]
            futures.extend(
                executor.submit(self._run_strict_rows, group, stopped, limits)
                for group in strict.values()
            )
            for future in futures:
                processed.extend(future.result())
        with self.store.connect(write=True) as db:
            self._mark_completed(db)
        return {"processed": processed}

    def _run_strict_rows(self, rows, stopped, limits):
        processed = []
        for row in rows:
            processed.extend(self._run_account_group([row], stopped, limits))
        return processed

    def _dispatchable_rows(self, rows, stopped):
        """Keep local waits out of shared account lanes; claims recheck every gate."""
        ready = []
        for row in rows:
            policy = json.loads(row["policy"])
            payload = json.loads(row["payload"])
            now = self.clock()
            native = payload["action"] in {"schedule_message", "schedule_file"}
            reason = None
            if row["state"] != "active":
                reason = "task_not_active"
            elif (
                now >= epoch(policy["ends_at"])
                or now - row["due_at"] > DELIVERY_GRACE_SECONDS
                or (native and row["due_at"] <= now)
            ):
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE items SET status='missed',result=? WHERE id=? AND status='not_attempted'",
                        (canonical({"reason": "delivery_window_elapsed"}), row["id"]),
                    )
                continue
            elif row["phase"] != "ready":
                reason = "task_not_started"
            elif stopped:
                reason = "global_stop"
            elif (
                row["mode"] == "live"
                and payload.get("execution_mode", "automatic") != "manual"
                and not self.live_enabled
            ):
                reason = "live_writes_disabled"
            elif now < epoch(policy["starts_at"]) or (not native and row["due_at"] > now):
                reason = "scheduled_time_pending"
            elif policy.get("pacing_scope", "task") == "task" and row["next_at"] > now:
                reason = "pacing_wait"
            else:
                account = self.account(row["account_id"])
                if account["disabled"]:
                    reason = "account_disabled"
                elif account["cooldown_until"] > now:
                    reason = "account_cooldown"
                elif account["next_at"] > now:
                    reason = "pacing_wait"
                else:
                    with self.store.connect() as db:
                        task = db.execute(
                            "SELECT * FROM tasks WHERE id=?", (row["task_id"],)
                        ).fetchone()
                        reason = telegram_blocker(db, task, payload, now)
            if reason:
                self._skip(row["id"], reason)
            else:
                ready.append(row)
        return ready

    def _run_account_group(self, rows, stopped, limits):
        rows = self._dispatchable_rows(rows, stopped)
        if not rows:
            return []
        try:
            account = self.account(rows[0]["account_id"])
            route = route_for(account, json.loads(rows[0]["payload"]))
            with self.lanes.acquire(rows[0]["account_id"], limits, resource=route.resource):
                return self._run_account_rows(rows, stopped)["processed"]
        except AccountBusy:
            for row in rows:
                self._skip(row["id"], "account_busy")
            return []

    def _run_account_rows(self, rows, stopped):
        processed = []
        checked = {}
        for row in rows:
            if row["state"] != "active":
                self._skip(row["id"], "task_not_active")
                continue
            if row["phase"] != "ready":
                self._skip(
                    row["id"],
                    "task_not_started",
                )
                continue
            if stopped:
                self._skip(row["id"], "global_stop")
                continue
            manual = json.loads(row["payload"]).get("execution_mode", "automatic") == "manual"
            if row["mode"] == "live" and not manual and not self.live_enabled:
                self._skip(row["id"], "live_writes_disabled")
                continue
            if (
                json.loads(row["policy"]).get("pacing_scope", "task") == "task"
                and row["next_at"] > self.clock()
            ):
                self._skip(row["id"], "pacing_wait")
                continue
            policy = json.loads(row["policy"])
            native = json.loads(row["payload"])["action"] in {"schedule_message", "schedule_file"}
            if (
                self.clock() >= epoch(policy["ends_at"])
                or self.clock() - row["due_at"] > DELIVERY_GRACE_SECONDS
                or (native and row["due_at"] <= self.clock())
            ):
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE items SET status='missed',result=? WHERE id=? AND status='not_attempted'",
                        (canonical({"reason": "delivery_window_elapsed"}), row["id"]),
                    )
                continue
            if self.clock() < epoch(policy["starts_at"]) or (
                not native and row["due_at"] > self.clock()
            ):
                self._skip(row["id"], "scheduled_time_pending")
                continue
            try:
                with self.store.connect() as db:
                    task = db.execute(
                        "SELECT * FROM tasks WHERE id=?", (row["task_id"],)
                    ).fetchone()
                    self._integrity(db, task, "outbound")
                    reason = telegram_blocker(db, task, json.loads(row["payload"]), self.clock())
                if reason:
                    self._skip(row["id"], reason)
                    continue
            except Conflict as exc:
                self._skip(row["id"], "manifest_integrity_failed")
                processed.append({"item_id": row["id"], "status": "blocked", "reason": str(exc)})
                continue
            account = self.account(row["account_id"])
            if not manual and not self.adapters.commissioned(account):
                self._skip(row["id"], "backend_not_commissioned")
                continue
            if account["disabled"] or account["cooldown_until"] > self.clock():
                self._skip(
                    row["id"], "account_disabled" if account["disabled"] else "account_cooldown"
                )
                continue
            from .interaction_intents import intent_blocker

            with self.store.connect() as db:
                blocked = intent_blocker(db, account, task, json.loads(row["payload"]), row["id"])
            if blocked:
                self._skip(row["id"], blocked["reason"])
                processed.append({"item_id": row["id"], "status": "blocked", **blocked})
                continue
            from .target_checks import TargetChecks

            deferred = TargetChecks(
                self, account, "preflight", {"payload": json.loads(row["payload"])}
            ).read()
            if not manual and deferred:
                self._skip(row["id"], "backend_gate_blocked")
                continue
            if not manual and account["id"] not in checked:
                # A fresh read-only health check before a live claim; simulation needs one too.
                checked[account["id"]] = self.check_account(account["id"], force=True)
            if not manual and checked[account["id"]]["connection_state"] != "healthy":
                self._skip(row["id"], "account_unhealthy")
                continue
            account = self.account(row["account_id"])
            if account["connection_state"] != "healthy" or account["cooldown_until"] > self.clock():
                self._skip(
                    row["id"],
                    "account_cooldown"
                    if account["cooldown_until"] > self.clock()
                    else "account_unhealthy",
                )
                continue
            try:
                gate = (
                    self.call(
                        account,
                        "preflight",
                        {"payload": json.loads(row["payload"])},
                        row["task_id"],
                    )
                    if account["mode"] == "live" and not manual
                    else None
                )
                if gate:
                    self.apply_cooldown(account["id"], gate)
                if gate and gate.get("allowed") is not True:
                    self._skip(row["id"], "backend_gate_blocked")
                    processed.append(
                        {
                            "item_id": row["id"],
                            "status": "blocked",
                            "reason": "原平台停止、冷却或限额尚未允许执行",
                        }
                    )
                    continue
                claim = self._claim(row["id"], gate)
            except Conflict as exc:
                self._skip(row["id"], "manifest_integrity_failed")
                processed.append({"item_id": row["id"], "status": "blocked", "reason": str(exc)})
                continue
            if not claim:
                continue
            account = self.account(row["account_id"])
            if manual or claim["payload"]["action"] == "manual_comment":
                result = {"status": "manual_attention", "reason": "manual_publication_required"}
            else:
                # Stop/pause can race a claim. Recheck before entering the adapter.
                current = self.task(claim["task_id"])
                stopped = (
                    self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0"
                )
                if stopped or current["state"] != "active":
                    result = {"status": "failed_known", "reason": "stopped_before_submission"}
                else:
                    try:
                        result = self.call(account, "execute", claim, claim["task_id"])
                    except Conflict:
                        result = {"status": "unknown", "reason": "transport_outcome_unknown"}
            normalized = self.finish(claim, result)
            processed.append({"item_id": row["id"], **normalized})
        with self.store.connect(write=True) as db:
            self._mark_completed(db)
        return {"processed": processed}

    def _mark_completed(self, db, task_id=None):
        db.execute(
            "DELETE FROM quota_reservations WHERE state='reserved' AND item_id IN (SELECT id FROM items WHERE status IN ('cancelled','missed') AND attempt_id IS NULL)"
        )
        db.execute(
            "DELETE FROM content_contacts WHERE assignment_id IN (SELECT ca.id FROM campaign_assignments ca JOIN items i ON i.id=ca.item_id WHERE i.status IN ('cancelled','missed') AND i.attempt_id IS NULL)"
        )
        scope = " AND id=? AND state='active'" if task_id else " AND state='active'"
        params = (self.clock(), task_id) if task_id else (self.clock(),)
        db.execute(
            "UPDATE tasks SET state='completed',updated_at=? "
            "WHERE state NOT IN ('cancelled','completed') AND phase='ready' "
            "AND EXISTS(SELECT 1 FROM items WHERE task_id=tasks.id) "
            "AND NOT EXISTS(SELECT 1 FROM items WHERE task_id=tasks.id "
            "AND status NOT IN ('confirmed','failed_known','missed','cancelled'))" + scope,
            params,
        )
        db.execute(
            "UPDATE campaigns SET state='completed',updated_at=? WHERE state='materialized' AND EXISTS(SELECT 1 FROM tasks WHERE campaign_id=campaigns.id) AND NOT EXISTS(SELECT 1 FROM tasks WHERE campaign_id=campaigns.id AND state NOT IN ('completed','cancelled'))",
            (self.clock(),),
        )

    def finish(self, claim, result, *, _db=None):
        status = result.get("status", "unknown")
        reference = result.get("external_reference")
        if status == "confirmed" and not (result.get("verified") and reference):
            status = "unknown"
        if status == "scheduled":
            ids = set(map(str, result.get("message_ids", [])))
            found = set(map(str, result.get("lookup_message_ids", [])))
            if not (
                reference
                and ids
                and ids <= found
                and all(result.get(k) is True for k in ("scheduled", "active", "verified"))
            ):
                status = "unknown"
        if status not in TERMINAL:
            status = "unknown"
        safe = {
            k: result[k]
            for k in (
                "verified",
                "confirmed_not_submitted",
                "scheduled",
                "active",
                "message_ids",
                "lookup_message_ids",
                "reason",
                "external_reference",
                "published_at",
                "scheduled_message_ids",
                "publish_id",
                "post_ids",
                "comment_id",
                "platform_status",
            )
            + FIELDS
            + BLOCK_FIELDS
            if k in result
        }
        safe["status"] = status
        now = self.clock()
        if "published_at" in safe:
            safe["published_at"] = publication_time(safe["published_at"], now)
        with (
            contextlib.nullcontext(_db) if _db is not None else self.store.connect(write=True)
        ) as db:
            row = db.execute(
                "SELECT * FROM items WHERE id=? AND attempt_id=?",
                (claim["item_id"], claim["attempt_id"]),
            ).fetchone()
            if not row:
                raise Conflict("执行所有者不匹配")
            if row["status"] not in {"executing", "unknown"}:
                return json.loads(row["result"])
            previous = json.loads(row["result"])
            for key in FIELDS:
                if key not in safe and key in previous:
                    safe[key] = previous[key]
            if not reference and row["external_reference"]:
                reference = row["external_reference"]
                safe["external_reference"] = reference
            db.execute(
                "UPDATE items SET status=?,external_reference=?,result=? WHERE id=?",
                (status, reference, canonical(safe), claim["item_id"]),
            )
            db.execute(
                "UPDATE attempts SET outcome=?,finished_at=? WHERE id=?",
                (status, now, claim["attempt_id"]),
            )
            if status == "confirmed":
                record_publication(db, claim["item_id"], safe, now)
            if result.get("rate_limited") or result.get("risk_warning"):
                cooldown = now + max(1, int(result.get("retry_after_seconds") or 3600))
                db.execute(
                    "UPDATE accounts SET cooldown_until=max(cooldown_until,?) WHERE id=?",
                    (cooldown, row["account_id"]),
                )
                if result.get("risk_warning"):
                    db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (row["account_id"],))
                Store.audit(
                    db,
                    "account.cooldown",
                    {"account_id": row["account_id"], "until": cooldown},
                    claim["task_id"],
                    now,
                )
            Store.audit(
                db,
                "attempt." + status,
                {
                    "item_id": claim["item_id"],
                    "external_reference": reference,
                    **{
                        k: safe[k]
                        for k in (
                            "reason",
                            "execution_step",
                            "submission_started",
                            "exception_type",
                        )
                        if k in safe
                    },
                },
                claim["task_id"],
                now,
            )
            self._mark_completed(db, claim["task_id"])
        return safe

    def reconcile(self, item_id, *, read_scan_rpc_limit=None):
        row = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not row:
            raise NotFound("执行项不存在")
        if row["status"] not in {"unknown", "scheduled"}:
            raise Conflict("只有未知结果或平台排期需要核对")
        payload = {
            "item_id": item_id,
            "task_id": row["task_id"],
            "attempt_id": row["attempt_id"],
            "attempt_started_at": row["claimed_at"],
            "payload": json.loads(row["payload"]),
            "external_reference": row["external_reference"],
            "result": json.loads(row["result"]),
        }
        account = self.account(row["account_id"])
        if (
            account["platform"] in {"telegram", "discord", "tiktok"}
            and account["cooldown_until"] > self.clock()
        ):
            raise Conflict("账号仍在冷却，请稍后核对")
        if read_scan_rpc_limit is not None:
            payload["read_scan_rpc_limit"] = read_scan_rpc_limit
        result = self.call(account, "verify", payload, row["task_id"])
        from .telegram_reading import read_diagnostics

        reads = read_diagnostics(result)
        self.apply_cooldown(account["id"], result)
        if not result.get("verified"):
            if account["platform"] == "tiktok":
                progress = {
                    k: result[k]
                    for k in ("publish_id", "post_ids", "comment_id", "platform_status", "reason")
                    if k in result
                }
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE items SET result=? WHERE id=? AND status='unknown'",
                        (canonical({**json.loads(row["result"]), **progress}), item_id),
                    )
                return {"status": row["status"], **progress}
            return {"status": row["status"], "reason": "not_conclusively_verified", **reads}
        # Reconciliation never turns an item back into a retryable state.
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='unknown' WHERE id=? AND status='scheduled'", (item_id,)
            )
        return {**self.finish(payload, result), **reads}

    def collect_feedback(self, item_id, *, read_scan_rpc_limit=None):
        row = self.store.one("SELECT account_id FROM items WHERE id=?", (item_id,))
        if not row:
            raise Conflict("回复采集需要已核验的发布结果")
        account = self.account(row["account_id"])
        with self.lanes.acquire(
            account["id"], resource=route_for(account, {}).resource
        ) as lane_owner:
            return self._collect_feedback_locked(
                item_id, read_scan_rpc_limit=read_scan_rpc_limit, lane_owner=lane_owner
            )

    def _collect_feedback_locked(self, item_id, *, read_scan_rpc_limit=None, lane_owner=None):
        row = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not row or row["status"] != "confirmed":
            raise Conflict("回复采集需要已核验的发布结果")
        account = self.account(row["account_id"])
        if account["mode"] != "live" or account["platform"] != "telegram":
            raise Conflict("当前回复采集仅接入真实 Telegram 互动")
        if account["cooldown_until"] > self.clock():
            raise Conflict("账号仍在冷却，请稍后采集回复")
        check = self.store.one("SELECT * FROM feedback_checks WHERE item_id=?", (item_id,))
        statistics = Statistics(self.store, clock=self.clock)
        binding = self.store.one(
            "SELECT * FROM telegram_read_cursors WHERE account_id=? AND identity_key='feedback' AND scope_key=?",
            (account["id"], item_id),
        )
        binding_value = json.loads(binding["cursor"]) if binding else None
        if binding_value and binding_value["identity_hash"] != account.get("identity_hash"):
            raise Conflict("TelegramFeedbackIdentityChanged")
        try:
            result = self.call(
                account,
                "feedback",
                {
                    "item_id": item_id,
                    "payload": json.loads(row["payload"]),
                    "result": json.loads(row["result"]),
                    "feedback_cursor": binding_value["cursor"] if binding_value else 0,
                    **({"read_peer_id": binding_value["peer_id"]} if binding_value else {}),
                    **(
                        {"read_scan_rpc_limit": read_scan_rpc_limit}
                        if read_scan_rpc_limit is not None
                        else {}
                    ),
                },
                row["task_id"],
            )
            self.apply_cooldown(account["id"], result)
            if result.get("verified") is not True:
                statistics.mark_feedback(
                    item_id,
                    self.clock(),
                    "failed",
                    error_code=result.get("reason", "feedback_unverified"),
                )
                return {
                    "item_id": item_id,
                    "status": "failed",
                    "reason": safe_code(result.get("reason"), "feedback_unverified"),
                    "read_rpc_used": result.get("read_rpc_used"),
                }
            if "read_identity_verified" in result and (
                result.get("read_identity_verified") is not True
                or (
                    account.get("identity_hash")
                    and result.get("read_identity_hash") != account["identity_hash"]
                )
            ):
                raise Conflict("TelegramFeedbackIdentityChanged")
            count = statistics.save_feedback_page(
                item_id,
                result,
                expected_cursor=check["last_message_id"] if check else 0,
                expected_identity=account.get("identity_hash"),
                expected_binding=binding,
                lane_owner=lane_owner,
            )
            return {
                "item_id": item_id,
                "status": result["feedback_status"],
                "new_replies": count,
                "read_rpc_used": result.get("read_rpc_used"),
                "read_complete": result.get(
                    "read_complete", result["feedback_status"] == "complete"
                ),
                **({"reason": safe_code(result["reason"])} if result.get("reason") else {}),
            }
        except Exception as exc:
            statistics.mark_feedback(
                item_id,
                self.clock(),
                "failed",
                error_code=getattr(exc, "safe_code", type(exc).__name__),
            )
            raise

    def collect_statistics(self, *, max_items=100):
        """Resume fair read-only rounds; persist completion only after every page."""
        if (
            not isinstance(max_items, int)
            or isinstance(max_items, bool)
            or not 1 <= max_items <= 1000
        ):
            raise ValueError("InvalidStatisticsReadLimit")
        now = self.clock()
        local = datetime.fromtimestamp(now, ZONE)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        slot = midnight + max(h for h in (0, 3, 6, 9, 12, 15, 18, 20, 21) if h <= local.hour) * 3600
        owner = uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            if db.execute(
                "SELECT 1 FROM telegram_statistics_rounds WHERE owner IS NOT NULL AND lease_until>?",
                (now,),
            ).fetchone():
                return {"processed": [], "complete": False, "reason": "statistics_collection_busy"}
            row = db.execute(
                "SELECT * FROM telegram_statistics_rounds WHERE slot=?", (slot,)
            ).fetchone()
            if not row:
                items = [
                    r[0]
                    for r in db.execute(
                        "SELECT i.id FROM items i JOIN accounts a ON a.id=i.account_id JOIN tasks t ON t.id=i.task_id "
                        "WHERE a.platform='telegram' AND a.mode='live' AND t.mode='live' "
                        "AND i.status IN ('confirmed','scheduled','unknown') ORDER BY i.due_at,i.id"
                    )
                ]
                db.execute(
                    "INSERT INTO telegram_statistics_rounds(slot,items,updated_at) VALUES(?,?,?)",
                    (slot, canonical(items), now),
                )
                db.executemany(
                    "INSERT INTO telegram_statistics_progress(slot,item_id) VALUES(?,?)",
                    [(slot, item) for item in items],
                )
            db.execute(
                "UPDATE telegram_statistics_rounds SET owner=?,lease_until=?,updated_at=? WHERE slot=?",
                (owner, now + 600, now, slot),
            )
            # retry_at rotates failed/partial work behind untouched work, including
            # newer rounds. A permanently unavailable target cannot starve others.
            pending = [
                dict(r)
                for r in db.execute(
                    "SELECT p.slot,p.item_id,i.account_id,i.status,i.due_at FROM telegram_statistics_progress p "
                    "JOIN items i ON i.id=p.item_id WHERE p.complete=0 AND p.retry_at<=? AND p.slot<=? "
                    "ORDER BY p.retry_at,p.slot,i.due_at,p.item_id",
                    (now, slot),
                )
            ]
        processed, seen, calls, rpc_used = [], set(), {}, {}
        try:
            for entry in pending:
                if len(processed) >= max_items:
                    break
                item_id, account_id = entry["item_id"], entry["account_id"]
                if item_id in seen:
                    continue
                seen.add(item_id)
                account = self.account(account_id)
                policy = self.telegram_read_policy(account_id)["policy"]
                if calls.get(account_id, 0) >= policy["groups_per_scan"]:
                    continue
                used = rpc_used.get(account_id, 0)
                if used is None or used >= policy["scan_rpc_limit"]:
                    continue
                if account["disabled"] or account["cooldown_until"] > self.clock():
                    with self.store.connect(write=True) as db:
                        db.execute(
                            "UPDATE telegram_statistics_progress SET retry_at=? WHERE item_id=? AND complete=0",
                            (max(account["cooldown_until"], self.clock() + 60), item_id),
                        )
                    continue
                result, completed = None, False
                try:
                    if (
                        entry["status"] in {"scheduled", "unknown"}
                        and entry["due_at"] <= self.clock()
                    ):
                        verification = self.reconcile(
                            item_id, read_scan_rpc_limit=policy["scan_rpc_limit"] - used
                        )
                        amount = verification.get("read_rpc_used")
                        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                            result = {
                                "item_id": item_id,
                                "status": "pending_verification",
                                "read_rpc_used": None,
                            }
                        else:
                            used += amount
                            rpc_used[account_id] = used
                    current = self.store.one("SELECT status FROM items WHERE id=?", (item_id,))
                    if (
                        result is None
                        and used < policy["scan_rpc_limit"]
                        and current
                        and current["status"] == "confirmed"
                    ):
                        result = self.collect_feedback(
                            item_id, read_scan_rpc_limit=policy["scan_rpc_limit"] - used
                        )
                        completed = (
                            result["status"] == "complete"
                            and result.get("read_complete") is not False
                        )
                    elif result is None:
                        result = {
                            "item_id": item_id,
                            "status": "pending_verification",
                            "read_rpc_used": 0,
                        }
                except Exception as exc:
                    result = {
                        "item_id": item_id,
                        "status": "unavailable",
                        "reason": safe_code(getattr(exc, "safe_code", type(exc).__name__)),
                    }
                calls[account_id] = calls.get(account_id, 0) + 1
                amount = result.get("read_rpc_used")
                rpc_used[account_id] = (
                    used + amount
                    if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0
                    else None
                )
                processed.append(result)
                with self.store.connect(write=True) as db:
                    lease = db.execute(
                        "SELECT owner FROM telegram_statistics_rounds WHERE slot=?", (slot,)
                    ).fetchone()
                    if not lease or lease[0] != owner:
                        break
                    db.execute(
                        "UPDATE telegram_statistics_progress SET complete=?,retry_at=? WHERE item_id=? AND slot<=? AND complete=0",
                        (
                            int(completed),
                            self.clock() + (1 if result.get("status") == "partial" else 60),
                            item_id,
                            slot,
                        ),
                    )
                    db.execute(
                        "UPDATE telegram_statistics_rounds SET lease_until=?,next_position=next_position+1 WHERE slot=? AND owner=?",
                        (self.clock() + 600, slot, owner),
                    )
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE telegram_statistics_rounds SET complete=1 WHERE slot<=? AND NOT EXISTS "
                    "(SELECT 1 FROM telegram_statistics_progress p WHERE p.slot=telegram_statistics_rounds.slot AND p.complete=0)",
                    (slot,),
                )
                done = bool(
                    db.execute(
                        "SELECT complete FROM telegram_statistics_rounds WHERE slot=?", (slot,)
                    ).fetchone()[0]
                )
                if done:
                    db.execute(
                        "INSERT INTO meta VALUES('statistics_collection_slot',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(slot),),
                    )
            return {
                "processed": processed,
                "complete": done,
                "slot": slot,
                "read_rpc_used_by_account": rpc_used,
            }
        finally:
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE telegram_statistics_rounds SET owner=NULL,lease_until=0 WHERE slot=? AND owner=?",
                    (slot, owner),
                )

    def manual_outcome(self, item_id, value):
        row = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not row or row["status"] != "manual_attention":
            raise Conflict("该项目不在人工处理状态")
        payload = json.loads(row["payload"])
        if payload["action"] != "manual_comment" and payload.get("execution_mode") != "manual":
            raise Conflict("该动作不支持人工登记")
        hosts = {
            "instagram": {"www.instagram.com", "instagram.com"},
            "x": {"x.com", "twitter.com", "www.x.com", "www.twitter.com"},
            "reddit": {"www.reddit.com", "reddit.com", "old.reddit.com"},
            "telegram": {"t.me"},
            "discord": {"discord.com"},
            "tiktok": {"www.tiktok.com", "tiktok.com"},
        }
        reference = urlparse(value.external_reference)
        exact = value.observed_text == payload["text"]
        status = (
            "confirmed"
            if exact
            and value.verified
            and reference.scheme == "https"
            and reference.hostname in hosts[payload["platform"]]
            else "unknown"
        )
        result = {
            "status": status,
            "verified": status == "confirmed",
            "external_reference": value.external_reference,
            "reason": "manual_result_recorded",
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status=?,external_reference=?,result=? WHERE id=?",
                (status, value.external_reference, canonical(result), item_id),
            )
            db.execute(
                "UPDATE attempts SET outcome=?,finished_at=? WHERE id=?",
                (status, self.clock(), row["attempt_id"]),
            )
            Store.audit(
                db,
                "manual.outcome",
                {"item_id": item_id, "exact_text": exact, "status": status},
                row["task_id"],
                self.clock(),
            )
            self._mark_completed(db, row["task_id"])
        return result

    def candidate(self, task_id, value: CandidateInput, *, _db=None):
        task = self.task(task_id)
        if value.account_id not in task["account_ids"]:
            raise Conflict("候选账号超出任务范围")
        p = task["policy"]
        # Only the literal annotation "non-adult" is negative evidence. Titles,
        # targets and any other adult/NSFW signals still pass through the risk gate.
        content = (
            value.title + " " + NON_ADULT.sub("", value.text) + " " + value.target
        ).casefold()
        reasons = []
        if RISK.search(content):
            reasons.append("blocked_category")
        if not value.is_public:
            reasons.append("not_public")
        if value.confidence < p["min_confidence"]:
            reasons.append("low_confidence")
        if value.language not in p["languages"]:
            reasons.append("language")
        if value.members < p["min_members"] or value.activity < p["min_activity"]:
            reasons.append("size_or_activity")
        if any(k.casefold() not in content for k in p["required_keywords"]):
            reasons.append("required_keywords")
        if any(k.casefold() in content for k in p["blocked_keywords"]):
            reasons.append("blocked_keywords")
        if p["queries"] and not any(
            any(w in content for w in q.casefold().split()) for q in p["queries"]
        ):
            reasons.append("unrelated_topic")
        if p["target_allowlist"] and value.target not in p["target_allowlist"]:
            reasons.append("target_boundary")
        decision = "rejected" if reasons else "qualified"
        with (
            contextlib.nullcontext(_db) if _db is not None else self.store.connect(write=True)
        ) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._integrity(db, row, "discovery_join")
            if row["state"] in {"paused", "cancelled", "completed"}:
                raise Conflict("任务已暂停或结束")
            existing = db.execute(
                "SELECT * FROM candidates WHERE task_id=? AND account_id=? AND target=?",
                (task_id, value.account_id, value.target),
            ).fetchone()
            payload = canonical(value.model_dump())
            if existing:
                if existing["payload"] == payload:
                    return {
                        "decision": existing["decision"],
                        "reasons": json.loads(existing["reasons"]),
                    }
                if row["manifest_hash"] or existing["state"] != "not_attempted":
                    raise Conflict("已冻结清单或已尝试入群的候选不可更正")
                if db.execute(
                    "SELECT 1 FROM join_attempts WHERE candidate_id=?", (existing["id"],)
                ).fetchone():
                    raise Conflict("已尝试入群的候选不可更正")
                db.execute(
                    "UPDATE candidates SET payload=?,decision=?,reasons=? WHERE id=?",
                    (payload, decision, canonical(reasons), existing["id"]),
                )
                Store.audit(
                    db,
                    "candidate.corrected",
                    {
                        "candidate_id": existing["id"],
                        "before_hash": digest(json.loads(existing["payload"])),
                        "after_hash": digest(value.model_dump()),
                        "decision": decision,
                        "reasons": reasons,
                    },
                    task_id,
                    self.clock(),
                )
                return {"decision": decision, "reasons": reasons}
            if (
                db.execute(
                    "SELECT count(*) FROM candidates WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                >= p["max_candidates"]
            ):
                raise Conflict("已达到候选上限")
            db.execute(
                "INSERT OR IGNORE INTO candidates(id,task_id,account_id,target,payload,decision,reasons) VALUES(?,?,?,?,?,?,?)",
                (
                    "candidate-" + uuid.uuid4().hex[:12],
                    task_id,
                    value.account_id,
                    value.target,
                    payload,
                    decision,
                    canonical(reasons),
                ),
            )
            Store.audit(
                db,
                "candidate." + decision,
                {"target": value.target, "reasons": reasons},
                task_id,
                self.clock(),
            )
        return {"decision": decision, "reasons": reasons}

    def discover(self, task_id, account_id):
        task = self.task(task_id)
        if account_id not in task["account_ids"]:
            raise Conflict("账号不在任务内")
        account = self.account(account_id)
        if "discover" not in CAPABILITIES[account["platform"]]:
            raise Conflict("该平台请通过候选录入接入")
        from .health_checks import probe_revision
        from .telegram_reading import read_diagnostics

        with self.lanes.acquire(account_id, resource=route_for(account, {}).resource) as lane_owner:
            account, task = self.account(account_id), self.task(task_id)
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                self._integrity(db, row, "discovery_join")
                if row["state"] in {"paused", "cancelled", "completed"}:
                    raise Conflict("任务已暂停或结束")
            revision = probe_revision(account, self.store.path)
            identity_key = digest([account_id, account.get("identity_hash"), revision])
            scope = digest(["telegram_discovery_v1", task_id, task["policy"]])
            previous = (
                self.store.one(
                    "SELECT * FROM telegram_read_cursors WHERE account_id=? AND identity_key=? AND scope_key=?",
                    (account_id, identity_key, scope),
                )
                if account["platform"] == "telegram"
                else None
            )
            if previous is None and account["platform"] == "telegram":
                old = self.store.one(
                    "SELECT cursor FROM telegram_read_cursors WHERE account_id=? AND scope_key=? ORDER BY updated_at DESC LIMIT 1",
                    (account_id, scope),
                )
                if old and not json.loads(old["cursor"]).get("complete", False):
                    return {
                        "decisions": [],
                        "read_complete": False,
                        "read_page_complete": False,
                        "read_state": "scope_changed",
                        "reason": "TelegramDiscoveryScopeChanged",
                        "source_note": "连接或身份已变化；旧扫描保留。请创建新任务重新扫描群目录。",
                    }
            if len(task["candidates"]) >= task["policy"]["max_candidates"]:
                return {
                    "decisions": [],
                    "read_complete": False,
                    "read_state": "candidate_limit",
                    "source_note": "已达到任务候选上限；后续目录尚未扫描。",
                }
            payload = {"policy": task["policy"]}
            if previous:
                payload["read_cursor"] = json.loads(previous["cursor"])
            result = self.call(account, "discover", payload, task_id)
            values = [
                CandidateInput(**{**value, "account_id": account_id})
                for value in result.get("candidates", [])
            ]
            if len(values) > task["policy"]["max_candidates"]:
                raise Conflict("DiscoveryPageTooLarge")
            live_telegram = account["mode"] == "live" and account["platform"] == "telegram"
            if live_telegram and "read_identity_verified" in result:
                if result.get("read_identity_verified") is not True or (
                    account.get("identity_hash")
                    and result.get("read_identity_hash") != account["identity_hash"]
                ):
                    raise Conflict("TelegramReadIdentityChanged")
                if result.get("read_page_complete") is not True:
                    return {
                        "decisions": [],
                        "source_note": result.get("source_note", ""),
                        **read_diagnostics(result),
                    }
                if not isinstance(result.get("read_cursor_next"), dict):
                    raise Conflict("TelegramReadCursorUnavailable")
            with self.store.connect(write=True) as db:
                lane = db.execute(
                    "SELECT owner,expires_at FROM account_lanes WHERE account_id=?", (account_id,)
                ).fetchone()
                if not lane or lane["owner"] != lane_owner or lane["expires_at"] <= time.time():
                    raise Conflict("TelegramReadLaneLost")
                current = dict(
                    db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
                )
                current_task = db.execute(
                    "SELECT policy FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if (
                    current["identity_hash"] != account.get("identity_hash")
                    or probe_revision(current, self.store.path) != revision
                    or json.loads(current_task[0]) != task["policy"]
                ):
                    raise Conflict("TelegramReadScopeChanged")
                if account["platform"] == "telegram":
                    old = db.execute(
                        "SELECT revision FROM telegram_read_cursors WHERE account_id=? AND identity_key=? AND scope_key=?",
                        (account_id, identity_key, scope),
                    ).fetchone()
                    if (old[0] if old else None) != (previous["revision"] if previous else None):
                        raise Conflict("TelegramReadCursorChanged")
                decisions = [self.candidate(task_id, value, _db=db) for value in values]
                if result.get("read_cursor_next") is not None and account["platform"] == "telegram":
                    db.execute(
                        "INSERT INTO telegram_read_cursors VALUES(?,?,?,?,1,?) ON CONFLICT(account_id,identity_key,scope_key) DO UPDATE SET cursor=excluded.cursor,revision=telegram_read_cursors.revision+1,updated_at=excluded.updated_at",
                        (
                            account_id,
                            identity_key,
                            scope,
                            canonical(result["read_cursor_next"]),
                            self.clock(),
                        ),
                    )
            return {
                "decisions": decisions,
                "source_note": result.get("source_note", ""),
                **read_diagnostics(result),
            }

    def run_joins(self, task_id):
        task = self.task(task_id)
        if self._queue_pooled_task(task_id):
            return {"processed": [], "queued": True}
        processed = []
        if not task["policy"]["max_joins"]:
            return {"processed": processed}
        for account_id in task["account_ids"]:
            task = self.task(task_id)
            # Future windows, cooldowns and completed candidates need no account lane.
            pending = [
                c
                for c in task["candidates"]
                if c["account_id"] == account_id and c["state"] == "not_attempted"
            ]
            account = self.account(account_id)
            if account["platform"] != "telegram":
                continue
            eligible = False
            for candidate in pending:
                reason = self._join_blocker(task, candidate, account)
                if reason:
                    self._skip(candidate["id"], reason, candidate=True)
                else:
                    eligible = True
            if not eligible:
                continue
            try:
                with self.lanes.acquire(account_id, self.execution_settings()):
                    processed.extend(self._run_joins(task_id, account_id)["processed"])
            except AccountBusy:
                continue
        return {"processed": processed}

    def _join_blocker(self, task, candidate, account):
        if candidate["decision"] != "qualified":
            return "candidate_rejected"
        if task["state"] != "active":
            return "task_not_active"
        if task["phase"] not in {"discovered", "starting", "ready"}:
            return "task_not_started"
        if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
            return "global_stop"
        if account["mode"] == "live" and not self.live_enabled:
            return "live_writes_disabled"
        if not self.adapters.commissioned(account):
            return "backend_not_commissioned"
        now = self.clock()
        if now >= epoch(task["policy"]["ends_at"]):
            return "delivery_window_elapsed"
        if now < epoch(task["policy"]["starts_at"]):
            return "scheduled_time_pending"
        if account["disabled"]:
            return "account_disabled"
        if account["cooldown_until"] > now:
            return "account_cooldown"
        if account["next_at"] > now or task["next_at"] > now:
            return "pacing_wait"
        return None

    def _run_joins(self, task_id, account_id, candidate_id=None):
        self.recover()
        task = self.task(task_id)
        processed = []
        if not task["policy"]["max_joins"]:
            return {"processed": []}
        for candidate in task["candidates"]:
            if candidate_id and candidate["id"] != candidate_id:
                continue
            if candidate["account_id"] != account_id:
                continue
            if candidate["state"] != "not_attempted":
                continue
            if candidate["decision"] != "qualified":
                self._skip(candidate["id"], "candidate_rejected", candidate=True)
                continue
            account = self.account(candidate["account_id"])
            if account["platform"] != "telegram":
                continue
            if account["mode"] == "live" and not self.live_enabled:
                self._skip(candidate["id"], "live_writes_disabled", candidate=True)
                continue
            if not self.adapters.commissioned(account):
                self._skip(candidate["id"], "backend_not_commissioned", candidate=True)
                continue
            if task["state"] != "active":
                self._skip(candidate["id"], "task_not_active", candidate=True)
                continue
            if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
                self._skip(candidate["id"], "global_stop", candidate=True)
                continue
            if (
                not epoch(task["policy"]["starts_at"])
                <= self.clock()
                < epoch(task["policy"]["ends_at"])
            ):
                self._skip(
                    candidate["id"],
                    "scheduled_time_pending"
                    if self.clock() < epoch(task["policy"]["starts_at"])
                    else "delivery_window_elapsed",
                    candidate=True,
                )
                continue
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                self._integrity(db, row, "discovery_join")
            # Avoid an unnecessary live identity check while this task is waiting
            # for its persisted per-account interval or platform cooldown.
            if (
                account["next_at"] > self.clock()
                or task["next_at"] > self.clock()
                or account["cooldown_until"] > self.clock()
            ):
                self._skip(
                    candidate["id"],
                    "account_cooldown"
                    if account["cooldown_until"] > self.clock()
                    else "pacing_wait",
                    candidate=True,
                )
                continue
            probe = self.check_account(account["id"], force=True)
            if probe["connection_state"] != "healthy":
                self._skip(candidate["id"], "account_unhealthy", candidate=True)
                continue
            now = self.clock()
            with self.store.connect(write=True) as db:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                policy = self._integrity(db, row, "discovery_join")
                a = db.execute("SELECT * FROM accounts WHERE id=?", (account["id"],)).fetchone()
                c = db.execute("SELECT * FROM candidates WHERE id=?", (candidate["id"],)).fetchone()
                from .health_checks import probe_key

                if probe["health"].get("probe_key") != probe_key(dict(a), self.store.path):
                    self._skip(candidate["id"], "account_unhealthy", db, candidate=True)
                    continue
                if (
                    row["state"] != "active"
                    or c["state"] != "not_attempted"
                    or c["decision"] != "qualified"
                ):
                    self._skip(candidate["id"], "task_not_active", db, candidate=True)
                    continue
                if (
                    db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0]
                    != "0"
                ):
                    self._skip(candidate["id"], "global_stop", db, candidate=True)
                    break
                if not epoch(policy["starts_at"]) <= now < epoch(policy["ends_at"]):
                    self._skip(candidate["id"], "delivery_window_elapsed", db, candidate=True)
                    continue
                if (
                    a["disabled"]
                    or a["status"] != "healthy"
                    or a["cooldown_until"] > now
                    or a["next_at"] > now
                    or row["next_at"] > now
                ):
                    reason = (
                        "account_disabled"
                        if a["disabled"]
                        else "account_unhealthy"
                        if a["status"] != "healthy"
                        else "account_cooldown"
                        if a["cooldown_until"] > now
                        else "pacing_wait"
                    )
                    self._skip(candidate["id"], reason, db, candidate=True)
                    continue
                if (
                    db.execute(
                        "SELECT count(*) FROM join_attempts WHERE task_id=?", (task_id,)
                    ).fetchone()[0]
                    >= policy["max_joins"]
                ):
                    self._skip(candidate["id"], "join_limit", db, candidate=True)
                    break
                quota_reason = quotas.check_claim(
                    db, a, row, {"target": c["target"], "recipient_id": ""}, now, now, None
                )
                if quota_reason:
                    self._skip(candidate["id"], quota_reason, db, candidate=True)
                    continue
                attempt = "join-" + uuid.uuid4().hex
                key = digest(["telegram", c["target"].casefold()])
                # Prevent another task from attempting a target already attempted by this account.
                if db.execute(
                    "SELECT 1 FROM join_attempts WHERE account_id=? AND target_key=?",
                    (a["id"], key),
                ).fetchone():
                    self._skip(candidate["id"], "join_already_attempted", db, candidate=True)
                    continue
                db.execute(
                    "INSERT INTO join_attempts(id,candidate_id,account_id,task_id,target_key,user_key,mode,started_at) VALUES(?,?,?,?,?,?,?,?)",
                    (attempt, c["id"], a["id"], task_id, key, key, a["mode"], now),
                )
                db.execute("UPDATE candidates SET state='executing' WHERE id=?", (c["id"],))
                db.execute(
                    "UPDATE accounts SET next_at=? WHERE id=?",
                    (
                        now
                        + max(
                            policy["min_interval"],
                            a["min_interval"],
                            90 if a["mode"] == "live" else 0,
                        ),
                        a["id"],
                    ),
                )
                db.execute(
                    "UPDATE tasks SET next_at=? WHERE id=?",
                    (now + policy["min_interval"], task_id),
                )
                Store.audit(db, "join.claimed", {"candidate_id": c["id"]}, task_id, now)
            try:
                if (
                    self.task(task_id)["state"] != "active"
                    or self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"]
                    != "0"
                ):
                    result = {"status": "failed_known", "reason": "stopped_before_submission"}
                else:
                    result = self.call(
                        account,
                        "join",
                        {
                            "candidate_id": candidate["id"],
                            "target": candidate["target"],
                            "min_interval": policy["min_interval"],
                        },
                        task_id,
                    )
            except Conflict:
                result = {"status": "unknown", "reason": "transport_outcome_unknown"}
            status = result.get("status", "unknown")
            if status not in {
                "joined",
                "already_joined",
                "pending_admin_approval",
                "failed_known",
                "unknown",
            }:
                status = "unknown"
            if status in {"joined", "already_joined"} and not result.get("verified"):
                status = "unknown"
            if status in {"joined", "already_joined"} and account["mode"] == "simulation":
                result.setdefault(
                    "outbound_eligible_at_utc",
                    datetime.fromtimestamp(self.clock() + 3600, timezone.utc).isoformat(),
                )
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE candidates SET state=?,result=? WHERE id=?",
                    (status, canonical(result), candidate["id"]),
                )
                db.execute(
                    "UPDATE join_attempts SET outcome=?,finished_at=? WHERE id=?",
                    (status, self.clock(), attempt),
                )
                if result.get("rate_limited") or result.get("risk_warning"):
                    db.execute(
                        "UPDATE accounts SET cooldown_until=max(cooldown_until,?),disabled=max(disabled,?) WHERE id=?",
                        (
                            self.clock() + max(1, int(result.get("retry_after_seconds") or 3600)),
                            int(bool(result.get("risk_warning"))),
                            account["id"],
                        ),
                    )
                Store.audit(
                    db, "join." + status, {"candidate_id": candidate["id"]}, task_id, self.clock()
                )
            processed.append({"candidate_id": candidate["id"], "status": status})
        return {"processed": processed}

    def run_reference_job(self, job):
        """Execute one persisted reference through the existing publication safeguards."""
        if job["kind"] == "join":
            account = self.account(job["account_id"])
            with self.lanes.acquire(account["id"], self.execution_settings()):
                result = self._run_joins(job["task_id"], account["id"], job["reference_id"])
            task = self.task(job["task_id"])
            if task["state"] == "active" and task["phase"] == "starting":
                self.start_task(task["id"], source="scheduler")
            candidate = self.store.one(
                "SELECT target FROM candidates WHERE id=?", (job["reference_id"],)
            )
            if candidate:
                self._resume_membership_dependents(account["id"], candidate["target"])
            return result
        self.recover()
        row = self.store.one(
            "SELECT i.id,i.account_id,i.task_id,i.payload,i.due_at,t.policy,t.mode,t.next_at,t.state,t.phase "
            "FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.id=? AND i.status='not_attempted' AND i.attempt_id IS NULL",
            (job["reference_id"],),
        )
        if not row:
            return {"processed": []}
        return {"processed": self._run_account_group([row], False, self.execution_settings())}

    def reconcile_join(self, candidate_id):
        row = self.store.one("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        if not row:
            raise NotFound("候选不存在")
        if row["state"] not in {"unknown", "pending_admin_approval", "joined", "already_joined"}:
            raise Conflict("只有已尝试的入群或成员身份需要核对")
        account = self.account(row["account_id"])
        result = self.call(
            account,
            "verify_join",
            {"candidate_id": candidate_id, "target": row["target"]},
            row["task_id"],
        )
        self.apply_cooldown(account["id"], result)
        if result.get("verified") is not True or result.get("status") not in {
            "joined",
            "already_joined",
        }:
            if row["state"] in {"joined", "already_joined"}:
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE candidates SET state='unknown',result=? WHERE id=? AND result=?",
                        (
                            canonical(
                                {**json.loads(row["result"]), "verified": False, "can_send": False}
                            ),
                            candidate_id,
                            row["result"],
                        ),
                    )
                return {"status": "unknown", "reason": "not_conclusively_verified"}
            return {"status": row["state"], "reason": "not_conclusively_verified"}
        # Membership lookup may confirm joining, but never grants another attempt.
        safe = {
            k: result[k]
            for k in (
                "status",
                "verified",
                "can_send",
                "outbound_eligible_at_utc",
                "external_reference",
            )
            if k in result
        }
        previous = json.loads(row["result"])
        if previous.get("outbound_eligible_at_utc"):
            # A read-only permission refresh is not a new join; retain the
            # original waiting deadline instead of postponing it every time.
            safe["outbound_eligible_at_utc"] = previous["outbound_eligible_at_utc"]
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE candidates SET state=?,result=? WHERE id=? AND state IN ('unknown','pending_admin_approval','joined','already_joined')",
                (safe["status"], canonical(safe), candidate_id),
            )
            db.execute(
                "UPDATE join_attempts SET outcome=?,finished_at=? WHERE candidate_id=?",
                (safe["status"], self.clock(), candidate_id),
            )
            Store.audit(
                db,
                "join.reconciled",
                {"candidate_id": candidate_id, "status": safe["status"]},
                row["task_id"],
                self.clock(),
            )
        self._resume_membership_dependents(row["account_id"], row["target"])
        return safe

    def run_cycle(self):
        if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
            return self.run_once()
        for row in self.store.rows("SELECT id,phase FROM tasks WHERE state='active'"):
            try:
                self.run_joins(row["id"])
                if row["phase"] == "starting":
                    self.start_task(row["id"], source="scheduler")
            except Conflict:
                pass
        return self.run_once()
