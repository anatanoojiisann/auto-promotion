"""Cloudflare task/attempt mirror and fail-closed executor authorization."""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import quotas
from .models import canonical, epoch
from .routing import ExecutionRoute, route_for


class CloudUnavailable(RuntimeError):
    safe_code = "CloudControlUnavailable"

    def __init__(self, code, *, retry_after_seconds=None):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds


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
        token = self.operator_token if operator else self.node_token
        if not token or (not operator and not self.node_id):
            raise CloudUnavailable("CloudCredentialNotConfigured")
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AutoPromotionExecutor/1.0",
        }
        if not operator:
            headers["X-Node-ID"] = self.node_id
        elif path in {"/v1/settings", "/v1/sync"} and self.node_token:
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
                return result
        except HTTPError as exc:
            # Provider responses and URLs may contain secrets. Only expose our allowlisted code.
            try:
                details = json.loads(exc.read(4096))
                code = details.get("error", "CloudRequestRejected")
                if code == "CloudRequestRejected" and details.get("error_code") == 1101:
                    code = "CloudWorkerException"
                retry = details.get("retry_after_seconds")
            except Exception:
                code = "CloudRequestRejected"
                retry = None
            if not isinstance(code, str) or not code.isidentifier() or len(code) > 100:
                code = "CloudRequestRejected"
            if not isinstance(retry, (int, float)) or not 1 <= retry <= 90000:
                retry = None
            raise CloudUnavailable(code, retry_after_seconds=retry) from None
        except Exception:
            raise CloudUnavailable("CloudTransportUnavailable") from None

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

    def sync(self, engine, *, force=False):
        from .execution_channels import binding

        # Allocate before reading: a delayed older upload cannot undo a later pause.
        with engine.store.connect(write=True) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cloud_sync_records("
                "destination TEXT NOT NULL,section TEXT NOT NULL,record_id TEXT NOT NULL,"
                "digest TEXT NOT NULL,revision INTEGER NOT NULL,"
                "PRIMARY KEY(destination,section,record_id))"
            )
            row = db.execute(
                "SELECT value FROM meta WHERE key='cloud_snapshot_revision'"
            ).fetchone()
            revision = int(row[0]) + 1 if row else 1
            db.execute(
                "INSERT INTO meta VALUES('cloud_snapshot_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(revision),),
            )
        snapshot = export_snapshot(engine)
        destination = hashlib.sha256(self.url.encode()).hexdigest()
        acknowledged = {
            (r["section"], r["record_id"]): r["digest"]
            for r in engine.store.rows(
                "SELECT section,record_id,digest FROM cloud_sync_records WHERE destination=?",
                (destination,),
            )
        }
        changed = 0
        # Import historical quota evidence before activating any new work.
        for section in ("quota_events", "accounts", "tasks", "jobs"):
            pending = []
            for record in snapshot[section]:
                record_id = (
                    canonical([record["attempt_id"], record["scope"], record["key"]])
                    if section == "quota_events"
                    else record["id"]
                )
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
                result = self.request("/v1/sync", payload, operator=True)
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
        return {"changed_records": changed, "unchanged": changed == 0}

    def flush_settings(self, engine):
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


def export_snapshot(engine):
    """Export business data and immutable references, never credential profiles or sessions."""
    accounts = {a["id"]: a for a in engine.accounts()}
    tasks = {t["id"]: t for t in engine.store.rows("SELECT * FROM tasks")}
    result = {"schema": 1, "accounts": [], "tasks": [], "jobs": [], "quota_events": []}
    routes = {
        r["job_id"]: r["route"] for r in engine.store.rows("SELECT * FROM execution_job_routes")
    }
    queued_routes = {
        r["id"]: r["route"] for r in engine.store.rows("SELECT id,route FROM execution_jobs")
    }
    new_routes = []

    def frozen_route(job_id, account, payload=None):
        stored = routes.get(job_id) or queued_routes.get(job_id)
        route = ExecutionRoute(**json.loads(stored)) if stored else route_for(account, payload)
        if job_id not in routes:
            routes[job_id] = canonical(route.public())
            new_routes.append((job_id, routes[job_id]))
        return route

    for a in accounts.values():
        route = route_for(a)
        allowed = a["automatic_actions"] if a["platform"] == "x" else a["capabilities"]
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
            }
        )
    for t in tasks.values():
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
    with engine.store.connect() as db:
        for row in db.execute("SELECT * FROM items").fetchall():
            t, a = tasks[row["task_id"]], accounts[row["account_id"]]
            # A relative membership schedule is mutable until its manifest is
            # frozen. Such placeholders must never become cloud execution jobs.
            if not t["manifest_hash"]:
                continue
            p, policy = json.loads(row["payload"]), json.loads(t["policy"])
            route = frozen_route("send:" + row["id"], a, p)
            native = p["action"] in {"schedule_message", "schedule_file"}
            scopes = [
                {"scope": k, "key": canonical([a["mode"], str(key)]), "limit": limit}
                for k, key, limit in quotas.scopes(db, a, t, p)
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
            t, a = tasks[row["task_id"]], accounts[row["account_id"]]
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
                for k, key, limit in quotas.scopes(db, a, t, p)
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
                    "state": "cancelled"
                    if t["state"] == "cancelled" and not attempt
                    else row["state"],
                    "available_at": epoch(policy["starts_at"]),
                    "expires_at": epoch(policy["ends_at"]),
                    "attempt_id": attempt["id"] if attempt else None,
                    "result": json.loads(row["result"]),
                    "scopes": scopes,
                }
            )
        # Migrate the recent quota ledger, including uncertain attempts, before admitting work.
        attempts = {
            (r["account_id"], r["task_id"], r["started_at"], r["target_key"], r["user_key"]): r[
                "id"
            ]
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
    if new_routes:
        with engine.store.connect(write=True) as db:
            db.executemany("INSERT OR IGNORE INTO execution_job_routes VALUES(?,?)", new_routes)
    return result


class CloudJournal:
    """Persist a lease before execution; replay only receipt uploads after a crash."""

    def __init__(self, engine, cloud):
        self.engine, self.store, self.cloud = engine, engine.store, cloud

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

    def flush(self, job_id=None):
        now = self.engine.clock()
        if not job_id:
            self.refresh_reconciled()
        sql = "SELECT * FROM cloud_receipt_outbox WHERE retry_at<=?"
        args = (now,)
        if job_id:
            sql += " AND job_id=?"
            args += (job_id,)
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

    def refresh_reconciled(self):
        for row in self.store.rows(
            "SELECT * FROM cloud_receipt_history WHERE json_extract(outcome,'$.state') IN ('unknown','scheduled','pending_admin_approval') LIMIT 256"
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
