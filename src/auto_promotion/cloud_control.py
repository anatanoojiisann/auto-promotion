"""Cloudflare task/attempt mirror and fail-closed executor authorization."""

import hashlib
import hmac
import json
import logging
import math
import os
import socket
import ssl
import time
import uuid
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from . import quotas
from .models import canonical, digest, epoch
from .routing import ExecutionRoute, route_for


def _scope_signature(engine, task_id):
    from .health_checks import probe_revision

    task = engine.store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        return None
    accounts = [
        engine.store.one(
            "SELECT id,platform,backend_label,mode,identity_hash,status,disabled,daily_limit,hourly_limit,min_interval FROM accounts WHERE id=?",
            (key,),
        )
        for key in json.loads(task["account_ids"])
    ]
    stopped = engine.store.one("SELECT value FROM meta WHERE key='global_stop'")
    return digest(
        [
            task["policy_hash"],
            task["manifest_hash"],
            task["state"],
            task["phase"],
            accounts,
            [probe_revision(account, engine.store.path) for account in accounts if account],
            engine.execution_settings(),
            engine.live_enabled,
            stopped["value"] if stopped else "1",
        ]
    )


def _scope_destination(engine, url, node_id):
    store = engine.store.one("SELECT value FROM meta WHERE key='execution_store_id'")
    return digest([url.rstrip("/"), node_id, store["value"] if store else None])


def task_scope_health(engine, task_id, *, cloud=None):
    """Fresh bound sync observation for display, never a write authorization."""
    row = engine.store.one("SELECT value FROM meta WHERE key=?", ("cloud_task_health:" + task_id,))
    if not row:
        return {
            "scope": "task",
            "task_id": task_id,
            "valid_for_task": False,
            "execution_ready": False,
        }
    value = json.loads(row["value"])
    url = cloud.url if cloud else os.getenv("AP_CLOUD_CONTROL_URL", "")
    node = cloud.node_id if cloud else os.getenv("AP_NODE_ID")
    now = engine.clock()
    valid = bool(
        url
        and value.get("task_signature") == _scope_signature(engine, task_id)
        and value.get("destination") == _scope_destination(engine, url, node)
        and isinstance(value.get("at"), (int, float))
        and 0 <= now - value["at"] <= 300
        and value.get("expires_at", 0) > now
    )
    return {
        key: value[key]
        for key in (
            "scope",
            "task_id",
            "state",
            "at",
            "execution_ready",
            "expires_at",
            "revision",
            "retry_at",
            "diagnostics",
        )
        if key in value
    } | {
        "valid_for_task": valid,
        "execution_ready": bool(valid and value.get("execution_ready")),
        **({"invalid_reason": "scope_or_observation_changed"} if not valid else {}),
    }


def _safe_transport_cause(error):
    """Finite, cyclic-safe classification; never stringify an exception/cause."""
    seen = set()
    for _ in range(4):
        if not isinstance(error, BaseException) or id(error) in seen:
            break
        seen.add(id(error))
        for cls, category in (
            (TimeoutError, "timeout"),
            (ssl.SSLError, "tls"),
            (socket.gaierror, "dns"),
            (RemoteDisconnected, "remote_disconnected"),
            (IncompleteRead, "incomplete_read"),
            (ConnectionRefusedError, "connection_refused"),
            (ConnectionResetError, "connection_reset"),
            (json.JSONDecodeError, "json_decode"),
            (UnicodeDecodeError, "text_decode"),
            (HTTPError, "http_rejected"),
        ):
            if isinstance(error, cls):
                return category
        error = error.reason if isinstance(error, URLError) else error.__cause__
    return "unknown"


def _safe_diagnostic_id(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = uuid.UUID(value)
        return value if parsed.version == 4 and str(parsed) == value else None
    except ValueError:
        return None


class CloudUnavailable(RuntimeError):
    safe_code = "CloudControlUnavailable"

    def __init__(self, code, *, retry_after_seconds=None, diagnostics=None):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds
        self.diagnostics = diagnostics


class CloudJobClosed(CloudUnavailable):
    def __init__(self, state):
        super().__init__("CloudJobAlreadySettled")
        self.state = state


class CloudControl:
    def __init__(self, url, *, node_id=None, node_token=None, operator_token=None):
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        ):
            raise ValueError("CloudControlRequiresHTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("InvalidCloudControlURL")
        self.url = url.rstrip("/")
        self.node_id, self.node_token, self.operator_token = node_id, node_token, operator_token
        self.last_contact_monotonic = None
        self.last_request_diagnostics = None
        self._diagnostic_logs = 0
        self._diagnostic_normal_logs = 0

    def _request_diagnostic(self, request_id, path, started, outcome, **metadata):
        try:
            return self._build_request_diagnostic(request_id, path, started, outcome, **metadata)
        except Exception:
            # Even malformed error objects or a failing formatter are optional
            # diagnostics. Preserve the original successful result/exception.
            result = {"schema": 1, "request_id": request_id, "outcome": "diagnostic_unavailable"}
            self.last_request_diagnostics = result
            return result

    def _build_request_diagnostic(
        self,
        request_id,
        path,
        started,
        outcome,
        *,
        error=None,
        status=None,
        payload=None,
        parse_error=None,
        context=None,
    ):
        # No URL, body, account, exception text, stack or credential is retained.
        operation = {
            "/v1/executor/telegram/login-begin": "login_begin",
            "/v1/executor/telegram/login-poll": "login_poll",
            "/v1/executor/telegram/status": "status",
            "/v1/executor/telegram/read": "read",
            "/v1/sync": "snapshot_sync",
            "/v1/account-channel/status": "channel_status",
            "/v1/account-channel": "channel_handoff",
            "/v1/claim": "job_claim",
            "/v1/authorize": "job_authorize",
            "/v1/heartbeat": "heartbeat",
        }.get(path, "cloud_request")
        result = {
            "schema": 1,
            "request_id": request_id,
            "operation": operation,
            "outcome": outcome,
            "elapsed_ms": min(1200000, max(0, int((time.monotonic() - started) * 1000))),
            "timeout_ms": 12000,
        }
        if error is not None:
            result["cause"] = _safe_transport_cause(error)
        if parse_error is not None:
            result["response_parse_cause"] = _safe_transport_cause(parse_error)
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            result["http_status"] = status
        if isinstance(payload, dict):
            for key in ("flow_id", "request_id"):
                value = _safe_diagnostic_id(payload.get(key))
                if value:
                    result["response_request_id" if key == "request_id" else key] = value
            generation = payload.get("generation")
            if (
                isinstance(generation, int)
                and not isinstance(generation, bool)
                and 0 < generation < 2**53
            ):
                result["generation"] = generation
        if "flow_id" not in result and isinstance(context, dict):
            flow_id = _safe_diagnostic_id(context.get("flow_id"))
            if flow_id:
                result["flow_id"] = flow_id
        self.last_request_diagnostics = result
        if (
            operation != "cloud_request"
            and self._diagnostic_logs < 96
            and (outcome != "completed" or self._diagnostic_normal_logs < 64)
        ):
            self._diagnostic_logs += 1
            if outcome == "completed":
                self._diagnostic_normal_logs += 1
            try:
                logger = logging.getLogger(__name__)
                (logger.info if outcome == "completed" else logger.warning)(
                    "cloud_request %s", json.dumps(result, sort_keys=True)
                )
            except Exception:
                pass  # An optional log sink must never change the request outcome.
        return result

    @classmethod
    def configured(cls):
        url = os.getenv("AP_CLOUD_CONTROL_URL")
        operator_token = os.getenv("AP_CLOUD_OPERATOR_TOKEN")
        if url and not operator_token and os.getenv("AP_CLOUD_OPERATOR_TOKEN_FILE"):
            try:
                operator_token = (
                    Path(os.environ["AP_CLOUD_OPERATOR_TOKEN_FILE"]).read_text().strip()
                )
            except OSError:
                raise CloudUnavailable("CloudOperatorCredentialUnavailable") from None
        return (
            cls(
                url,
                node_id=os.getenv("AP_NODE_ID"),
                node_token=os.getenv("AP_CLOUD_NODE_TOKEN"),
                operator_token=operator_token,
            )
            if url
            else None
        )

    def request(self, path, data=None, *, operator=False, method="POST"):
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        token = self.operator_token if operator else self.node_token
        if not token or (not operator and not self.node_id):
            raise CloudUnavailable("CloudCredentialNotConfigured")
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AutoPromotionExecutor/1.0",
            "X-AP-Request-ID": request_id,
        }
        if not operator:
            headers["X-Node-ID"] = self.node_id
        elif (
            path
            in {
                "/v1/settings",
                "/v1/sync",
                "/v1/account-channel",
                "/v1/workflow/create",
                "/v1/workflow/node",
                "/v1/workflow/state",
                "/v1/workflow/start-public",
                "/v1/workflow/stop-public",
                "/v1/workflow/replies/start",
                "/v1/workflow/replies/stop",
                "/v1/workflow/replies/prepare",
                "/v1/workflow/replies/reconfirm",
                "/v1/executor/telegram/login-begin",
                "/v1/executor/telegram/login-poll",
                "/v1/executor/telegram/read",
                "/v1/executor/telegram/activate",
                "/v1/executor/telegram/link-native",
                "/v1/executor/telegram/dispatch-native",
                "/v1/executor/telegram/start-reader",
                "/v1/executor/telegram/stop-reader",
            }
            and self.node_token
        ):
            headers["X-Local-Control-Proof"] = hmac.new(
                hashlib.sha256(self.node_token.encode()).hexdigest().encode(),
                canonical(data).encode(),
                hashlib.sha256,
            ).hexdigest()
        request = Request(
            self.url + path,
            data=canonical(data).encode() if data is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=12) as response:
                result = json.load(response)
                self.last_contact_monotonic = time.monotonic()
                self._request_diagnostic(
                    request_id,
                    path,
                    started,
                    "completed",
                    status=getattr(response, "status", None),
                    payload=result,
                    context=data,
                )
                engine = getattr(self, "_block_engine", None)
                if engine is not None:
                    self.import_account_blocks(engine, result)
                return result
        except HTTPError as exc:
            # Provider responses and URLs may contain secrets. Only expose our allowlisted code.
            parse_error = None
            try:
                details = json.loads(exc.read(4096))
                code = details.get("error", "CloudRequestRejected")
                if code == "CloudRequestRejected" and details.get("error_code") == 1101:
                    code = "CloudWorkerException"
                retry = details.get("retry_after_seconds")
                conflict = details.get("conflict")
            except Exception as decode_error:
                code = "CloudRequestRejected"
                retry = None
                conflict = None
                parse_error = decode_error
            finally:
                exc.close()
            if not isinstance(code, str) or not code.isidentifier() or len(code) > 100:
                code = "CloudRequestRejected"
            if not isinstance(retry, (int, float)) or not math.isfinite(retry) or retry <= 0:
                retry = None
            diagnostic = self._request_diagnostic(
                request_id,
                path,
                started,
                "http_rejected",
                error=exc,
                status=exc.code,
                parse_error=parse_error,
                context=data,
            )
            if code == "FrozenJobChanged" and isinstance(conflict, dict):
                index, fields = conflict.get("conflict_index"), conflict.get("changed_fields")
                if (
                    isinstance(index, int)
                    and not isinstance(index, bool)
                    and index >= 0
                    and isinstance(fields, list)
                    and all(
                        field
                        in {"version", "account_id", "task_id", "reference_id", "pool", "kind"}
                        for field in fields
                    )
                ):
                    diagnostic["sync_conflict"] = {
                        "conflict_index": index,
                        "changed_fields": fields,
                    }
            raise CloudUnavailable(
                code, retry_after_seconds=retry, diagnostics=diagnostic
            ) from None
        except Exception as exc:
            diagnostic = self._request_diagnostic(
                request_id, path, started, "transport_failed", error=exc, context=data
            )
            raise CloudUnavailable("CloudTransportUnavailable", diagnostics=diagnostic) from None

    def claim(self, pool, expected_id):
        started = time.monotonic()
        result = self.request("/v1/claim", {"pool": pool, "expected_id": expected_id}).get("job")
        if result and result["id"] != expected_id:
            self.complete(result, "pending", {}, None)
            return None
        if not result:
            source = self.request("/v1/job", {"id": expected_id}).get("job")
            if source and source["state"] not in {"pending", "leased"}:
                raise CloudJobClosed(source["state"])
        if result:
            self._renewal_deadline(result, result, started)
        return result

    def guard(self, context, attempt_id):
        if (
            not context
            or not context.get("cloud")
            or not self.authorize(context["cloud"], attempt_id)
        ):
            raise CloudUnavailable("CloudExecutionClaimRequired")

    def renew(self, lease):
        now = time.monotonic()
        if now < lease.get("_renew_at_monotonic", 0):
            return True
        result = self.request("/v1/renew", {"id": lease["id"], "lease_token": lease["lease_token"]})
        if result.get("renewed") is not True:
            return False
        self._renewal_deadline(lease, result, now)
        return True

    @staticmethod
    def _renewal_deadline(lease, response, started):
        # Relative server duration + monotonic clock avoids wall-clock skew.
        # Count network time against the margin; older servers keep the original
        # frequent renewal behavior until they expose the duration explicitly.
        delay = response.get("renew_after_seconds")
        if isinstance(delay, (int, float)) and 0 <= delay <= 120:
            lease["_renew_at_monotonic"] = started + delay
        else:
            lease.pop("_renew_at_monotonic", None)
        if isinstance(response.get("lease_until"), (int, float)):
            lease["lease_until"] = response["lease_until"]

    def authorize(self, lease, attempt_id):
        return (
            self.request(
                "/v1/authorize",
                {"id": lease["id"], "lease_token": lease["lease_token"], "attempt_id": attempt_id},
            ).get("allowed")
            is True
        )

    def complete(self, lease, state, result, attempt_id):
        return self.request(
            "/v1/complete",
            {
                "id": lease["id"],
                "lease_token": lease["lease_token"],
                "state": state,
                "result": result,
                "attempt_id": attempt_id,
            },
        )

    def sync(self, engine, *, force=False, task_ids=None, metadata_only=False):
        from .execution_channels import binding, offline_only

        if offline_only(engine.store):
            if not task_ids:
                raise CloudUnavailable("LocalOfflineModeActive")
            for tid in task_ids:
                row = engine.store.one("SELECT account_ids FROM tasks WHERE id=?", (tid,))
                if not row or any(
                    offline_only(engine.store, a) for a in json.loads(row["account_ids"])
                ):
                    raise CloudUnavailable("LocalOfflineModeActive")
        binding(engine)
        selected = sorted(set(task_ids or [])) if task_ids is not None else None
        if selected is not None:
            for task_id in selected:
                saved = engine.store.one(
                    "SELECT value FROM meta WHERE key=?", ("cloud_task_health:" + task_id,)
                )
                previous = json.loads(saved["value"]) if saved else {}
                if (
                    previous.get("task_signature") == _scope_signature(engine, task_id)
                    and previous.get("destination")
                    == _scope_destination(engine, self.url, self.node_id)
                    and previous.get("state") != "connected"
                    and previous.get("retry_at", 0) > engine.clock()
                ):
                    raise CloudUnavailable(
                        previous["state"], retry_after_seconds=previous["retry_at"] - engine.clock()
                    )
        scope = "metadata" if metadata_only else "task" if selected is not None else "full"
        contexts = []
        keys = selected if selected is not None else [scope]
        for key in keys:
            token = uuid.uuid4().hex
            generation_key = "cloud_sync_observation:" + (
                "task:" + key if scope == "task" else scope
            )
            signature = _scope_signature(engine, key) if scope == "task" else None
            task = (
                engine.store.one("SELECT policy FROM tasks WHERE id=?", (key,))
                if scope == "task"
                else None
            )
            contexts.append(
                (
                    key,
                    token,
                    generation_key,
                    signature,
                    epoch(json.loads(task["policy"])["ends_at"]) if task else engine.clock() + 300,
                )
            )
            with engine.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (generation_key, token),
                )
        try:
            result = self._sync(engine, force=force, task_ids=selected, metadata_only=metadata_only)
        except Exception as error:
            self._observe_sync(
                engine, scope, contexts, error=error, revision=getattr(error, "sync_revision", None)
            )
            raise
        if not result.get("unchanged"):
            self._observe_sync(engine, scope, contexts, revision=result.get("revision"))
        return {
            **result,
            "scope": scope,
            **({"task_ids": selected} if selected is not None else {}),
        }

    def _observe_sync(self, engine, scope, contexts, error=None, revision=None):
        now = engine.clock()
        for key, token, generation_key, signature, deadline in contexts:
            if scope == "task" and signature != _scope_signature(engine, key):
                continue
            code = (
                str(error)
                if isinstance(error, CloudUnavailable)
                else type(error).__name__
                if error
                else "connected"
            )
            code = (
                code
                if isinstance(code, str) and code.isidentifier() and len(code) <= 100
                else "CloudSyncUnavailable"
            )
            delay = getattr(error, "retry_after_seconds", None)
            if error and not (
                isinstance(delay, (int, float)) and math.isfinite(delay) and delay > 0
            ):
                delay = 30
            value = {
                "scope": scope,
                "state": code,
                "at": now,
                "execution_ready": error is None,
                "expires_at": min(deadline, now + 300),
                "task_signature": signature,
                "destination": _scope_destination(engine, self.url, self.node_id),
                **({"task_id": key} if scope == "task" else {}),
                **({"retry_at": now + delay} if error else {}),
            }
            value["revision"] = revision
            if error and isinstance(getattr(error, "diagnostics", None), dict):
                value["diagnostics"] = {
                    field: error.diagnostics[field]
                    for field in (
                        "request_id",
                        "http_status",
                        "elapsed_ms",
                        "sync_scope",
                        "sync_revision",
                        "sync_batch_section",
                        "sync_record_hash",
                        "sync_conflict",
                    )
                    if field in error.diagnostics
                }
            with engine.store.connect(write=True) as db:
                current = db.execute(
                    "SELECT value FROM meta WHERE key=?", (generation_key,)
                ).fetchone()
                if not current or current[0] != token:
                    continue
                health_key = (
                    "cloud_task_health:" + key
                    if scope == "task"
                    else "cloud_" + scope + "_sync_health"
                )
                db.execute(
                    "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (health_key, canonical(value)),
                )

    def _sync(self, engine, *, force=False, task_ids=None, metadata_only=False):
        from .execution_channels import binding

        self._block_engine = engine
        destination = hashlib.sha256(self.url.encode()).hexdigest()
        acknowledged = {
            (r["section"], r["record_id"]): r["digest"]
            for r in engine.store.rows(
                "SELECT section,record_id,digest FROM cloud_sync_records WHERE destination=?",
                (destination,),
            )
        }

        def snapshot_for_scope():
            snapshot = (
                export_snapshot(engine, task_ids=task_ids)
                if task_ids is not None
                else export_snapshot(engine)
            )
            if task_ids is not None:
                selected = set(task_ids)
                snapshot["tasks"] = [t for t in snapshot["tasks"] if t["id"] in selected]
                if {t["id"] for t in snapshot["tasks"]} != selected:
                    raise CloudUnavailable("CloudSnapshotTaskNotFound")
                accounts = {a for t in snapshot["tasks"] for a in t["account_ids"]}
                snapshot["accounts"] = [a for a in snapshot["accounts"] if a["id"] in accounts]
                snapshot["jobs"] = [j for j in snapshot["jobs"] if j["task_id"] in selected]
                # Retain the whole recent ledger: global limits span all tasks.
            if metadata_only:
                snapshot["jobs"] = []
            return snapshot

        snapshot = snapshot_for_scope()

        def record_key(section, record):
            return (
                canonical([record["attempt_id"], record["scope"], record["key"]])
                if section == "quota_events"
                else record["id"]
            )

        if not force and all(
            acknowledged.get((section, record_key(section, record)))
            == hashlib.sha256(canonical(record).encode()).hexdigest()
            for section in ("quota_events", "accounts", "tasks", "jobs")
            for record in snapshot[section]
        ):
            return {"changed_records": 0, "unchanged": True}
        # Allocate BEFORE the authoritative read. A delayed upload cannot undo
        # a later pause; unchanged polling does not advance either cursor.
        with engine.store.connect(write=True) as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='cloud_snapshot_revision'"
            ).fetchone()
            revision = int(row[0]) + 1 if row else 1
            db.execute(
                "INSERT INTO meta VALUES('cloud_snapshot_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(revision),),
            )
        snapshot = snapshot_for_scope()
        changed = 0
        # Import historical quota evidence before activating any new work.
        for section in ("quota_events", "accounts", "tasks", "jobs"):
            pending = []
            for record in snapshot[section]:
                record_id = record_key(section, record)
                checksum = hashlib.sha256(canonical(record).encode()).hexdigest()
                if force or acknowledged.get((section, record_id)) != checksum:
                    pending.append((record_id, checksum, record))
            for start in range(0, len(pending), 100):
                batch = pending[start : start + 100]
                payload = {
                    "local_store_id": binding(engine),
                    "schema": 1,
                    "revision": revision,
                    "accounts": [],
                    "tasks": [],
                    "jobs": [],
                    "quota_events": [],
                }
                payload[section] = [r[2] for r in batch]
                if len(canonical(payload).encode()) > 3_500_000:
                    raise CloudUnavailable("CloudSnapshotBatchTooLarge")
                try:
                    result = self.request("/v1/sync", payload, operator=True)
                except CloudUnavailable as error:
                    diagnostic = dict(error.diagnostics or {})
                    conflict = diagnostic.get("sync_conflict", {})
                    index = conflict.get("conflict_index")
                    diagnostic.update(
                        sync_scope="task"
                        if task_ids is not None
                        else "metadata"
                        if metadata_only
                        else "full",
                        sync_revision=revision,
                        sync_batch_section=section,
                    )
                    if isinstance(index, int) and 0 <= index < len(batch):
                        diagnostic["sync_record_hash"] = hashlib.sha256(
                            str(batch[index][0]).encode()
                        ).hexdigest()
                    error.diagnostics = diagnostic
                    error.sync_revision = revision
                    raise
                self.import_account_blocks(engine, result)
                if result.get("stale"):
                    raise CloudUnavailable("CloudSnapshotStale")
                # Commit each acknowledged slice, so failure resumes at the first
                # unacknowledged record instead of replaying a whole snapshot.
                with engine.store.connect(write=True) as db:
                    db.executemany(
                        "INSERT INTO cloud_sync_records VALUES(?,?,?,?,?) "
                        "ON CONFLICT(destination,section,record_id) DO UPDATE SET "
                        "digest=excluded.digest,revision=excluded.revision "
                        "WHERE excluded.revision>=cloud_sync_records.revision",
                        [(destination, section, r[0], r[1], revision) for r in batch],
                    )
                changed += len(batch)
        return {"changed_records": changed, "unchanged": changed == 0, "revision": revision}

    @staticmethod
    def import_account_blocks(engine, response):
        """Mirror scoped remote stops; imported health never authorizes local recovery."""
        from .account_blocks import merge_remote_block

        if not isinstance(response, dict) or not isinstance(response.get("account_blocks"), list):
            return
        with engine.store.connect(write=True) as db:
            for record in response["account_blocks"]:
                if not isinstance(record, dict) or not isinstance(record.get("account_id"), str):
                    continue
                account = db.execute(
                    "SELECT * FROM accounts WHERE id=?", (record.get("account_id"),)
                ).fetchone()
                if not account or route_for(dict(account)).identity != record.get("identity"):
                    continue
                state = record.get("outbound_block_state")
                if isinstance(state, dict):
                    merge_remote_block(
                        db,
                        account["id"],
                        {**state, "affected_tasks": record.get("affected_tasks", [])},
                        now=engine.clock(),
                    )

    def flush_settings(self, engine, *, account_id=None):
        from .execution_channels import offline_only

        if offline_only(engine.store, account_id):
            raise CloudUnavailable("LocalOfflineModeActive")
        pending = engine.store.one("SELECT value FROM meta WHERE key='cloud_pending_settings'")
        if not pending:
            return
        from .execution_channels import binding

        self.request(
            "/v1/settings",
            {**json.loads(pending["value"]), "local_store_id": binding(engine)},
            operator=True,
            method="PUT",
        )
        with engine.store.connect(write=True) as db:
            db.execute(
                "DELETE FROM meta WHERE key='cloud_pending_settings' AND value=?",
                (pending["value"],),
            )


def queue_settings(engine, changes):
    """Only explicit settings changes may alter cloud stops or limits."""
    required = engine.store.one("SELECT value FROM meta WHERE key='cloud_control_required'")
    if not os.getenv("AP_CLOUD_CONTROL_URL") and not (required and required["value"] == "1"):
        return
    with engine.store.connect(write=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='cloud_pending_settings'").fetchone()
        pending = json.loads(row[0]) if row else {}
        pending.update(changes)
        db.execute(
            "INSERT INTO meta VALUES('cloud_pending_settings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (canonical(pending),),
        )


def export_snapshot(engine, *, task_ids=None):
    """Export business data and immutable references, never credential profiles or sessions."""
    with engine.store.connect() as db:
        # One WAL read snapshot for parents, children, routes and quota evidence.
        # A concurrent task creation must not leave a child outside its parents.
        db.execute("BEGIN")
        try:
            result, new_routes = _export_snapshot(engine, db, task_ids=task_ids)
        except (KeyError, TypeError, ValueError):
            # Imported or partially migrated definitions must fail closed with
            # a stable code, never export an incomplete authorization boundary.
            raise CloudUnavailable("CloudSnapshotInconsistent") from None
    if new_routes:
        with engine.store.connect(write=True) as db:
            db.executemany("INSERT OR IGNORE INTO execution_job_routes VALUES(?,?)", new_routes)
    return result


def _export_snapshot(engine, db, *, task_ids=None):
    from .backends.common import BackendError, automatic_actions, read_profile
    from .models import CAPABILITIES

    accounts = {a["id"]: dict(a) for a in db.execute("SELECT * FROM accounts")}
    # Keep cleared generations as tombstones: omission is never a remote resume.
    # Export only authorization metadata, not health identity or raw evidence.
    blocks = {
        row["account_id"]: {
            "state": row["state"],
            "reason": row["reason"],
            "since": row["since"],
            "until": row["until_at"],
            "generation": row["generation"],
            "health_checked_at": row["health_checked_at"],
            "health_ok": bool(row["health_ok"]),
        }
        for row in db.execute("SELECT * FROM account_blocks")
    }
    for a in accounts.values():
        a["capabilities"] = sorted(CAPABILITIES[a["platform"]])
        a["automatic_actions"] = automatic_actions(a)
        a["owned_target_url"] = ""
        if a["platform"] == "x" and a["mode"] == "live":
            try:
                identity = read_profile("x", a["backend_label"]).get("identity", "")
                if (
                    isinstance(identity, str)
                    and identity
                    and identity.isascii()
                    and len(identity) <= 15
                    and identity.replace("_", "").isalnum()
                ):
                    a["owned_target_url"] = "https://x.com/" + identity
            except BackendError:
                pass
    tasks = {t["id"]: dict(t) for t in db.execute("SELECT * FROM tasks")}
    # Imported history never becomes remote executable jobs. A production
    # catalog release awaiting human approval must not disrupt existing platforms
    # by uploading unsupported desktop accounts; it grants no local write path.
    archived_tasks = {
        r[0]
        for r in db.execute(
            "SELECT task_id FROM meta_browser_sources UNION "
            "SELECT i.task_id FROM meta_browser_history h JOIN items i ON i.id=h.item_id"
        )
    }
    release = db.execute("SELECT value FROM meta WHERE key='meta_browser_cloud_catalog'").fetchone()
    withheld = (
        {r[0] for r in db.execute("SELECT account_id FROM meta_browser_accounts")}
        if release and release[0] == "pending_user_approval"
        else set()
    )
    excluded = archived_tasks | {
        t["id"] for t in tasks.values() if withheld.intersection(json.loads(t["account_ids"]))
    }
    if task_ids is not None and set(task_ids).intersection(excluded):
        raise CloudUnavailable("MetaBrowserCloudCatalogPending")
    tasks = {k: t for k, t in tasks.items() if k not in excluded}
    accounts = {k: a for k, a in accounts.items() if k not in withheld}
    if task_ids is not None:
        selected = set(task_ids)
        if selected - tasks.keys():
            raise CloudUnavailable("CloudSnapshotTaskNotFound")
        tasks = {key: value for key, value in tasks.items() if key in selected}
    scoped_accounts = (
        {account_id for t in tasks.values() for account_id in json.loads(t["account_ids"])}
        if task_ids is not None
        else None
    )
    result = {"schema": 1, "accounts": [], "tasks": [], "jobs": [], "quota_events": []}
    routes = {r["job_id"]: r["route"] for r in db.execute("SELECT * FROM execution_job_routes")}
    queued_routes = {r["id"]: r["route"] for r in db.execute("SELECT id,route FROM execution_jobs")}
    new_routes = []

    def frozen_route(job_id, account, payload=None):
        stored = routes.get(job_id) or queued_routes.get(job_id)
        route = ExecutionRoute(**json.loads(stored)) if stored else route_for(account, payload)
        if job_id not in routes:
            routes[job_id] = canonical(route.public())
            new_routes.append((job_id, routes[job_id]))
        return route

    for a in accounts.values():
        if scoped_accounts is not None and a["id"] not in scoped_accounts:
            continue
        route = route_for(a)
        allowed = a["automatic_actions"] if a["platform"] == "x" else a["capabilities"]
        if route.transport == "desktop":
            allowed = a["automatic_actions"]
        elif a["platform"] == "instagram":
            allowed = ["publish", "manual_comment"]
        if a["platform"] == "reddit":
            allowed = []
        result["accounts"].append(
            {
                "id": a["id"],
                "identity": route.identity,
                "platform": a["platform"],
                "mode": a["mode"],
                "limits": {"daily": a["daily_limit"], "hourly": a["hourly_limit"]},
                "min_interval": a["min_interval"],
                "next_at": a["next_at"],
                "cooldown_until": a["cooldown_until"],
                "disabled": bool(a["disabled"]),
                "allowed_actions": allowed,
                **({"outbound_block_state": blocks[a["id"]]} if a["id"] in blocks else {}),
                **({"owned_target_url": a["owned_target_url"]} if a["platform"] == "x" else {}),
            }
        )
    for t in tasks.values():
        policy = json.loads(t["policy"])
        if not isinstance(policy, dict) or not all(
            isinstance(policy.get(key), str) for key in ("starts_at", "ends_at")
        ):
            raise CloudUnavailable("CloudSnapshotInconsistent")
        # Missing timezone is compatible with the historical policy default.
        ZoneInfo(policy.get("timezone", "Asia/Shanghai"))
        if epoch(policy["ends_at"]) <= epoch(policy["starts_at"]):
            raise CloudUnavailable("CloudSnapshotInconsistent")
        if any(account_id not in accounts for account_id in json.loads(t["account_ids"])):
            raise CloudUnavailable("CloudSnapshotInconsistent")
        result["tasks"].append(
            {
                "id": t["id"],
                "name": t["name"],
                "state": t["state"],
                "phase": t["phase"],
                "version": t["policy_hash"],
                "policy": json.loads(t["policy"]),
                "account_ids": json.loads(t["account_ids"]),
                "manifest": json.loads(t["manifest"]) if t["manifest"] else None,
                "manifest_hash": t["manifest_hash"],
            }
        )
    for row in db.execute("SELECT * FROM items").fetchall():
        if row["task_id"] in excluded:
            continue
        if task_ids is not None and row["task_id"] not in tasks:
            continue
        t, a = tasks.get(row["task_id"]), accounts.get(row["account_id"])
        if t is None or a is None:
            raise CloudUnavailable("CloudSnapshotInconsistent")
        # A relative membership schedule is mutable until its manifest is
        # frozen. Such placeholders must never become cloud execution jobs.
        if not t["manifest_hash"]:
            continue
        p, policy = json.loads(row["payload"]), json.loads(t["policy"])
        route = frozen_route("send:" + row["id"], a, p)
        native = p["action"] in {"schedule_message", "schedule_file"}
        scopes = [
            {"scope": k, "key": canonical([a["mode"], str(key)]), "limit": limit}
            for k, key, limit in quotas.scopes(db, a, t, p, include_local_override=False)
        ]
        result["jobs"].append(
            {
                "id": "send:" + row["id"],
                "kind": "send",
                "reference_id": row["id"],
                "task_id": t["id"],
                "account_id": a["id"],
                "pool": route.pool,
                "resource": route.resource,
                "action": p["action"],
                "position": row["position"],
                "version": row["payload_hash"],
                "payload": p,
                "state": row["status"],
                "available_at": epoch(policy["starts_at"]) if native else row["due_at"],
                "expires_at": min(
                    epoch(policy["ends_at"]), row["due_at"] if native else row["due_at"] + 900
                ),
                "attempt_id": row["attempt_id"],
                "result": json.loads(row["result"]),
                "scopes": scopes,
            }
        )
    for row in db.execute("SELECT * FROM candidates WHERE decision='qualified'").fetchall():
        if row["task_id"] in excluded:
            continue
        if task_ids is not None and row["task_id"] not in tasks:
            continue
        t, a = tasks.get(row["task_id"]), accounts.get(row["account_id"])
        if t is None or a is None:
            raise CloudUnavailable("CloudSnapshotInconsistent")
        policy = json.loads(t["policy"])
        if a["platform"] != "telegram" or not policy["max_joins"]:
            continue
        route = frozen_route("join:" + row["id"], a)
        attempt = db.execute(
            "SELECT id FROM join_attempts WHERE candidate_id=?", (row["id"],)
        ).fetchone()
        p = {"target": row["target"], "recipient_id": row["target"], "action": "join"}
        scopes = [
            {"scope": k, "key": canonical([a["mode"], str(key)]), "limit": limit}
            for k, key, limit in quotas.scopes(db, a, t, p, include_local_override=False)
        ]
        result["jobs"].append(
            {
                "id": "join:" + row["id"],
                "kind": "join",
                "reference_id": row["id"],
                "task_id": t["id"],
                "account_id": a["id"],
                "pool": route.pool,
                "resource": route.resource,
                "action": "join",
                "version": hashlib.sha256(row["payload"].encode()).hexdigest(),
                "payload": json.loads(row["payload"]),
                "state": "cancelled" if t["state"] == "cancelled" and not attempt else row["state"],
                "available_at": epoch(policy["starts_at"]),
                "expires_at": epoch(policy["ends_at"]),
                "attempt_id": attempt["id"] if attempt else None,
                "result": json.loads(row["result"]),
                "scopes": scopes,
            }
        )
    # Migrate the recent quota ledger, including uncertain attempts, before admitting work.
    attempts = {
        (r["account_id"], r["task_id"], r["started_at"], r["target_key"], r["user_key"]): r["id"]
        for r in db.execute(
            "SELECT id,account_id,task_id,started_at,target_key,user_key FROM attempts UNION ALL SELECT id,account_id,task_id,started_at,target_key,user_key FROM join_attempts"
        )
    }
    for event in quotas.events(db, "live") + quotas.events(db, "simulation"):
        if event["at"] < time.time() - 86400:
            continue
        # A stable source-event key avoids counting the same historical event twice.
        key = attempts.get(
            (event["account"], event["task"], event["at"], event["target"], event["user"])
        )
        if key is None:
            continue  # Future reservations are not completed attempts.
        for scope in (
            "global",
            "platform",
            "account",
            "task",
            "campaign",
            "pool",
            "target",
            "user",
        ):
            if event.get(scope) is not None:
                result["quota_events"].append(
                    {
                        "attempt_id": key,
                        "scope": scope,
                        "key": canonical([event["mode"], str(event[scope])]),
                        "at": event["at"],
                    }
                )
        for scope, value in (
            ("cloud_global", event["mode"]),
            ("cloud_account", route_for(accounts[event["account"]]).identity),
            ("cloud_task", event["task"]),
        ):
            result["quota_events"].append(
                {"attempt_id": key, "scope": scope, "key": str(value), "at": event["at"]}
            )
    return result, new_routes


class CloudJournal:
    """Persist a lease before execution; replay only receipt uploads after a crash."""

    def __init__(self, engine, cloud):
        self.engine, self.store, self.cloud = engine, engine.store, cloud
        if cloud is not None:
            cloud._block_engine = engine

    def remember(self, lease):
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO cloud_receipt_outbox(job_id,lease,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET lease=excluded.lease,outcome=NULL,updated_at=excluded.updated_at,retry_at=0,error=NULL",
                (lease["id"], canonical(lease), self.engine.clock()),
            )

    def outcome(self, lease):
        with self.store.connect() as db:
            if lease["kind"] == "send":
                row = db.execute(
                    "SELECT status state,result,attempt_id FROM items WHERE id=?",
                    (lease["reference_id"],),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT c.state,c.result,a.id attempt_id FROM candidates c LEFT JOIN join_attempts a ON a.candidate_id=c.id WHERE c.id=?",
                    (lease["reference_id"],),
                ).fetchone()
            if not row:
                return {"state": "cancelled", "result": {}, "attempt_id": None}
            state = row["state"]
            if state == "not_attempted":
                state = "pending" if not row["attempt_id"] else "unknown"
            if state == "executing":
                state = "unknown"
            account = db.execute(
                "SELECT next_at,cooldown_until FROM accounts WHERE id=?", (lease["account_id"],)
            ).fetchone()
            result = json.loads(row["result"])
            result["next_at"] = max(
                account["next_at"], account["cooldown_until"], self.engine.clock() + 5
            )
            return {"state": state, "result": result, "attempt_id": row["attempt_id"]}

    def finish(self, lease):
        outcome = self.outcome(lease)
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE cloud_receipt_outbox SET outcome=?,updated_at=?,retry_at=0 WHERE job_id=?",
                (canonical(outcome), self.engine.clock(), lease["id"]),
            )
        self.flush(job_id=lease["id"])

    def flush(self, job_id=None, *, account_id=None):
        from .execution_channels import offline_only

        if job_id and account_id is None:
            row = self.store.one("SELECT account_id FROM execution_jobs WHERE id=?", (job_id,))
            if row:
                account_id = row["account_id"]
        if offline_only(self.engine.store, account_id):
            raise CloudUnavailable("LocalOfflineModeActive")
        now = self.engine.clock()
        if not job_id:
            self.refresh_reconciled(account_id=account_id)
        sql = "SELECT * FROM cloud_receipt_outbox WHERE retry_at<=?"
        args = (now,)
        if job_id:
            sql += " AND job_id=?"
            args += (job_id,)
        if account_id:
            sql += " AND json_extract(lease,'$.account_id')=?"
            args += (account_id,)
        for row in self.store.rows(sql + " ORDER BY updated_at LIMIT 32", args):
            lease = json.loads(row["lease"])
            outcome = json.loads(row["outcome"]) if row["outcome"] else None
            if outcome is None:
                job = self.store.one(
                    "SELECT state,lease_until FROM execution_jobs WHERE id=?", (row["job_id"],)
                )
                if job and job["state"] == "leased" and job["lease_until"] > now:
                    continue
                outcome = self.outcome(lease)
            try:
                try:
                    self.cloud.complete(lease, **outcome)
                except CloudUnavailable as exc:
                    if outcome["state"] != "pending" or str(exc) != "AttemptCannotReplay":
                        raise
                    # The cloud has already quarantined an expired lease. Never reopen it.
                    outcome = {**outcome, "state": "unknown"}
                    self.cloud.complete(lease, **outcome)
                with self.store.connect(write=True) as db:
                    db.execute(
                        "INSERT INTO cloud_receipt_history VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET lease=excluded.lease,outcome=excluded.outcome",
                        (row["job_id"], row["lease"], canonical(outcome)),
                    )
                    db.execute(
                        "DELETE FROM cloud_receipt_outbox WHERE job_id=? AND lease=?",
                        (row["job_id"], row["lease"]),
                    )
            except CloudUnavailable as exc:
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE cloud_receipt_outbox SET outcome=?,retry_at=?,error=? WHERE job_id=? AND lease=?",
                        (canonical(outcome), now + 30, str(exc), row["job_id"], row["lease"]),
                    )

    def refresh_reconciled(self, *, account_id=None):
        scope = " AND json_extract(lease,'$.account_id')=?" if account_id else ""
        for row in self.store.rows(
            "SELECT * FROM cloud_receipt_history WHERE json_extract(outcome,'$.state') IN ('unknown','scheduled','pending_admin_approval')"
            + scope
            + " LIMIT 256",
            (account_id,) if account_id else (),
        ):
            outcome = self.outcome(json.loads(row["lease"]))
            prior = json.loads(row["outcome"])
            if outcome["state"] == prior["state"]:
                continue
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT OR IGNORE INTO cloud_receipt_outbox(job_id,lease,outcome,updated_at) VALUES(?,?,?,?)",
                    (row["job_id"], row["lease"], canonical(outcome), self.engine.clock()),
                )
