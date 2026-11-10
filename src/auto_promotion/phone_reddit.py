"""Explicit Reddit phone pilot on the normal AP queue; no autonomous starts."""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .backends.common import BackendError, read_profile, save_profile, verify_claim
from .core import Conflict
from .models import canonical, digest
from .store import Store


class RedditBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device_id: str
    identity: str = Field(pattern=r"^u/[A-Za-z0-9_-]{1,40}$")
    node_os: str = Field(default="windows", pattern=r"^(windows|macos|any)$")


class RedditPhoneConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: str = Field(default="development", pattern=r"^development$")
    enabled: bool = False
    submit_enabled: bool = False
    fleet_url: str
    service_token: SecretStr = SecretStr("")
    oauth_issuer: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: SecretStr | None = None
    reddit_read_access_token: SecretStr | None = None
    runtime_token: SecretStr
    authorized_task_ids: list[str] = Field(default_factory=list)
    bindings: dict[str, RedditBinding]


SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_reddit_operations(
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL,item_id TEXT,kind TEXT NOT NULL,
 request TEXT NOT NULL,phase TEXT NOT NULL,execution_id TEXT,result TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS phone_reddit_authorizations(
 id TEXT PRIMARY KEY,item_id TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,
 claim TEXT NOT NULL,context TEXT NOT NULL,device_id TEXT NOT NULL,expires_at REAL NOT NULL,
 used_at REAL,native_id TEXT,execution_id TEXT
);
CREATE TABLE IF NOT EXISTS phone_reddit_accounts(
 account_id TEXT PRIMARY KEY,identity TEXT NOT NULL,device_id TEXT NOT NULL,
 observation TEXT NOT NULL,observed_at REAL NOT NULL
);
"""


class PhoneReddit:
    operations_table = "phone_reddit_operations"

    def __init__(self, engine, config, *, transport=None, reader=None):
        from phone_fleet.client import trusted_url
        from phone_fleet_protocol.reddit import RedditReader

        self.engine, self.store = engine, engine.store
        self.config = RedditPhoneConfig.model_validate(config)
        if not self.config.enabled:
            raise Conflict("PhoneRedditDevelopmentDisabled")
        self.url = trusted_url(self.config.fleet_url, development=True)
        self.transport = transport
        self.token_lock = threading.Lock()
        self.cached_token = None
        self.token_until = 0
        if self.config.oauth_issuer:
            from phone_fleet.auth import KeycloakAuth

            KeycloakAuth.validate_endpoint(self.config.oauth_issuer, True)
            if not self.config.oauth_client_id or not self.config.oauth_client_secret:
                raise Conflict("PhoneRedditOAuthClientRequired")
        self.reader = reader or RedditReader(
            access_token_provider=(lambda: self.config.reddit_read_access_token.get_secret_value())
            if self.config.reddit_read_access_token
            else None
        )
        with self.store.connect(write=True) as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
        self.engine.adapters.phone_reddit = self

    def task_authorized(self, task_id, account_id, *, db=None):
        if task_id in self.config.authorized_task_ids:
            return True
        from .phone_rounds import round_task_authorized
        from .temporal_tasks import dynamically_authorized

        def authorized(connection):
            return dynamically_authorized(
                self, connection, task_id, account_id
            ) or round_task_authorized(connection, task_id, account_id, self.engine.clock())

        if db is not None:
            return authorized(db)
        with self.store.connect() as connection:
            return authorized(connection)

    def binding(self, account_id):
        account = self.engine.account(account_id)
        binding = self.config.bindings.get(account_id)
        if not binding or account["platform"] != "reddit" or account["mode"] != "live":
            raise Conflict("PhoneRedditAccountNotBound")
        return account, binding

    def worker_config(self):
        from .phone_private import write_worker_config

        return write_worker_config(self.store.path, self.config, "reddit")

    def fleet_token(self):
        if not self.config.oauth_issuer:
            return self.config.service_token.get_secret_value()
        with self.token_lock:
            if self.cached_token and self.token_until > time.time():
                return self.cached_token
            with httpx.Client(
                timeout=8, trust_env=False, follow_redirects=False, transport=self.transport
            ) as client:
                response = client.post(
                    self.config.oauth_issuer.rstrip("/") + "/protocol/openid-connect/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.config.oauth_client_id,
                        "client_secret": self.config.oauth_client_secret.get_secret_value(),
                    },
                )
            if response.status_code != 200 or len(response.content) > 16384:
                raise BackendError("PhoneRedditFleetOAuthUnavailable")
            value = response.json()
            if (
                not isinstance(value.get("access_token"), str)
                or not value["access_token"]
                or type(value.get("expires_in")) not in {int, float}
                or value["expires_in"] <= 30
            ):
                raise BackendError("PhoneRedditFleetOAuthInvalid")
            self.cached_token, self.token_until = (
                value["access_token"],
                time.time() + value["expires_in"] - 30,
            )
            return self.cached_token

    def request(self, method, path, body=None, key=None, *, timeout=None):
        token = self.fleet_token()
        with httpx.Client(
            base_url=self.url,
            timeout=timeout if timeout is not None else (3 if path == "/capabilities" else 12),
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            response = client.request(
                method,
                "/internal/fleet/v1" + path,
                json=body,
                headers={
                    "Authorization": "Bearer " + token,
                    **({"Idempotency-Key": key} if key else {}),
                },
            )
        if response.status_code in {401, 403}:
            raise BackendError("PhoneFleetAuthorizationRequired")
        if response.status_code != 200 or len(response.content) > 1_000_000:
            raise BackendError("PhoneRedditFleetReadOrDispatchUnavailable")
        return response.json()

    def dispatch(self, request, operation_id, account_id, item_id=None, *, wait=True):
        from .execution_timing import stage

        with stage(
            self.engine,
            request.business_task_id,
            "phone_" + request.operation,
            operation_id=operation_id,
            item_id=item_id,
        ):
            return self._dispatch(request, operation_id, account_id, item_id, wait=wait)

    def _dispatch(self, request, operation_id, account_id, item_id=None, *, wait=True):
        """Persist intent before create; an uncertain POST is only looked up thereafter."""
        from phone_fleet_protocol import ExecutionSnapshot, fingerprint

        table = self.operations_table
        if table not in {"phone_reddit_operations", "phone_social_operations"}:
            raise ValueError("Untrusted operation table")
        value = request.model_dump(mode="json")
        from .phone_protocol import require_capabilities

        # Do not create an intent or POST a command against a different protocol.
        if not self.store.one(f"SELECT id FROM {table} WHERE id=?", (operation_id,)):
            if time.time() >= request.deadline:
                raise Conflict("PhoneExecutionDeadlineElapsed")
            require_capabilities(self.request("GET", "/capabilities"), request.operation)
        with self.store.connect(write=True) as db:
            old = db.execute(f"SELECT * FROM {table} WHERE id=?", (operation_id,)).fetchone()
            if old and json.loads(old["request"]) != value:
                raise Conflict("PhoneRedditImmutableRequestChanged")
            if not old:
                if time.time() >= request.deadline:
                    raise Conflict("PhoneExecutionDeadlineElapsed")
                db.execute(
                    f"INSERT INTO {table}(id,account_id,item_id,kind,request,phase,created_at) VALUES(?,?,?,?,?,'dispatching',?)",
                    (
                        operation_id,
                        account_id,
                        item_id,
                        request.operation,
                        canonical(value),
                        time.time(),
                    ),
                )
        try:
            if old:
                if old["execution_id"]:
                    snapshot = self.request("GET", "/executions/" + old["execution_id"])
                else:
                    from urllib.parse import urlencode

                    snapshot = self.request(
                        "GET",
                        "/executions/lookup?"
                        + urlencode(
                            {
                                "business_task_id": request.business_task_id,
                                "task_item_id": request.task_item_id,
                                "operation": request.operation,
                            }
                        ),
                    )
            else:
                snapshot = self.request("POST", "/executions", value, operation_id)
            until = time.monotonic() + min(100, max(0, request.deadline - time.time()))

            def remaining():
                return min(until - time.monotonic(), request.deadline - time.time())

            last_saved = (
                old["result"] if old and old["execution_id"] and old["phase"] == "linked" else None
            )
            poll_delay = 0.2
            while True:
                checked = ExecutionSnapshot.model_validate(snapshot)
                if fingerprint(checked.request) != fingerprint(request):
                    raise Conflict("PhoneRedditFleetCorrelationMismatch")
                encoded = canonical(snapshot)
                if encoded != last_saved:
                    with self.store.connect(write=True) as db:
                        db.execute(
                            f"UPDATE {table} SET execution_id=?,result=?,phase='linked' WHERE id=?",
                            (checked.id, encoded, operation_id),
                        )
                    last_saved = encoded
                if (
                    not wait
                    or checked.status
                    in {"succeeded", "failed", "cancelled", "unknown", "waiting_for_human"}
                    or remaining() <= 0
                ):
                    return snapshot
                time.sleep(max(0, min(poll_delay, remaining())))
                if remaining() <= 0:
                    return snapshot
                snapshot = self.request("GET", "/executions/" + checked.id)
                poll_delay = min(2.0, poll_delay * 2)
        except (httpx.HTTPError, BackendError):
            # No resend, even if lookup is 404 after a lost initial response.
            with self.store.connect(write=True) as db:
                db.execute(f"UPDATE {table} SET phase='uncertain' WHERE id=?", (operation_id,))
            return {"status": "unknown", "result": None}

    def read_legacy_operation(self, row):
        from urllib.parse import quote, urlencode

        from .phone_protocol import legacy_result

        request = json.loads(row["request"])
        path = (
            "/executions/" + quote(row["execution_id"], safe="")
            if row["execution_id"]
            else "/executions/lookup?"
            + urlencode(
                {
                    "business_task_id": request["business_task_id"],
                    "task_item_id": request["task_item_id"],
                    "operation": request["operation"],
                }
            )
        )
        return legacy_result(self.request("GET", path), request, row["execution_id"])

    def make_request(
        self,
        account_id,
        operation,
        context,
        *,
        task_id=None,
        item_id=None,
        manifest=None,
        channel="local",
    ):
        from phone_fleet_protocol import ExecutionRequest, RedditComment, fingerprint

        _, binding = self.binding(account_id)
        context = RedditComment.model_validate({**context, "identity": binding.identity})
        now = time.time()
        return ExecutionRequest(
            business_task_id=task_id or "phone-health:" + uuid4().hex,
            task_item_id=item_id or "identity",
            manifest_sha256=manifest or digest({"identity": binding.identity}),
            account_id=account_id,
            device_id=binding.device_id,
            operation=operation,
            execution_channel=channel,
            node_os=binding.node_os,
            not_before=now,
            deadline=now + 115,
            expected_identity_sha256=fingerprint({"account": binding.identity}),
            reddit=context,
        )

    def register(self, account_id, context):
        account, binding = self.binding(account_id)
        try:
            previous = read_profile("reddit", account["backend_label"])
        except BackendError as error:
            if error.safe_code != "AccountNotConfigured":
                raise
        else:
            if previous.get("connection_type") != "phone_fleet":
                raise Conflict("PhoneRedditExistingConnectionPreserved")
        request = self.make_request(account_id, "reddit_identity", context)
        snapshot = self.dispatch(request, "phone-identity:" + uuid4().hex, account_id)
        result = snapshot.get("result") or {}
        if (
            snapshot["status"] != "succeeded"
            or result.get("simulated") is not False
            or result.get("observed_identity_sha256") != request.expected_identity_sha256
        ):
            raise Conflict("PhoneRedditFreshIdentityRequired")
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO phone_reddit_accounts VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET identity=excluded.identity,device_id=excluded.device_id,observation=excluded.observation,observed_at=excluded.observed_at",
                (account_id, binding.identity, binding.device_id, canonical(snapshot), time.time()),
            )
        # This is a private phone binding, never a copied browser session or password.
        save_profile(
            "reddit",
            account["backend_label"],
            {
                "identity": binding.identity,
                "connection_type": "phone_fleet",
                "phone_fleet": {
                    "device_id": binding.device_id,
                    "node_os": binding.node_os,
                    "reddit_comments_enabled": True,
                },
            },
        )
        self.engine.check_account(account_id, force=True)
        return {
            "identity": binding.identity,
            "device_id": binding.device_id,
            "identity_verified": True,
            "submission_enabled": self.config.submit_enabled,
            "source_execution_id": snapshot["id"],
        }

    def prepare(self, item_id, metadata, *, discard=False):
        item = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not item or item["status"] != "not_attempted" or item["attempt_id"]:
            raise Conflict("PhoneRedditPreparationRequiresUnattemptedItem")
        payload = json.loads(item["payload"])
        if payload["action"] != "reply" or payload.get("media"):
            raise Conflict("PhoneRedditOnlyTopLevelTextComments")
        context = {
            **metadata,
            "text": payload["text"],
            "target_url": payload["target"],
            "discard_after_prepare": discard,
        }
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (item["task_id"],))
        policy = json.loads(task["policy"])
        _, bound = self.binding(item["account_id"])
        if policy.get("execution_method", "inherit") not in {"inherit", "phone"} or policy.get(
            "execution_node_os", "any"
        ) not in {"any", bound.node_os}:
            raise Conflict("PhoneRedditExecutionMethodOrNodeMismatch")
        identifier = "phone-prepare:" + uuid4().hex
        request = self.make_request(
            item["account_id"],
            "reddit_prepare",
            context,
            task_id=item["task_id"],
            item_id=item_id + ":" + identifier,
            manifest=task["manifest_hash"] or item["payload_hash"],
            channel=json.loads(task["policy"]).get("execution_channel", "inherit"),
        )
        snapshot = self.dispatch(request, identifier, item["account_id"], item_id)
        observed = snapshot.get("result") or {}
        if (
            snapshot.get("status") == "succeeded"
            and observed.get("simulated") is False
            and observed.get("observed_identity_sha256") == request.expected_identity_sha256
            and type(observed.get("observed_at")) in {int, float}
        ):
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE phone_reddit_accounts SET observation=?,observed_at=? WHERE account_id=? AND identity=? AND device_id=?",
                    (
                        canonical(snapshot),
                        observed["observed_at"],
                        item["account_id"],
                        request.reddit.identity,
                        request.device_id,
                    ),
                )
        return snapshot

    def fresh(self, account):
        from .phone_protocol import expected_version

        observed = self.store.one(
            "SELECT * FROM phone_reddit_accounts WHERE account_id=?", (account["id"],)
        )
        binding = self.config.bindings.get(account["id"])
        return bool(
            observed
            and binding
            and observed["identity"] == binding.identity
            and observed["device_id"] == binding.device_id
            and json.loads(observed["observation"]).get("request", {}).get("version")
            == expected_version()
            and 0 <= time.time() - observed["observed_at"] < 300
        )

    def backend(self, account, operation, payload, execution_job=None):
        binding = self.config.bindings.get(account["id"])
        fresh = self.fresh(account)
        if operation == "health":
            return {
                "healthy": fresh,
                "identity_verified": fresh,
                "service_ready": True,
                "permissions": ["reply"] if fresh else [],
                "account_identity": binding.identity if fresh else None,
                "reason": "phone_reddit_identity_observed"
                if fresh
                else "PhoneRedditFreshIdentityRequired",
            }
        if operation == "preflight":
            prepared = self.store.one(
                "SELECT result FROM phone_reddit_operations WHERE account_id=? AND kind='reddit_prepare' ORDER BY created_at DESC LIMIT 1",
                (account["id"],),
            )
            proof = json.loads(prepared["result"]) if prepared else {}
            request = proof.get("request", {})
            context = request.get("reddit", {})
            item = payload.get("payload", {})
            allowed = bool(
                self.config.submit_enabled
                and self.engine.live_enabled
                and fresh
                and proof.get("status") == "succeeded"
                and context.get("text") == item.get("text")
                and context.get("target_url") == item.get("target")
                and not context.get("discard_after_prepare")
            )
            if allowed:
                from phone_fleet_protocol import RedditComment

                try:
                    asyncio.run(self.reader.target(RedditComment.model_validate(context)))
                except Exception:
                    return {
                        "allowed": False,
                        "confirmed_not_submitted": True,
                        "reason": "PhoneRedditTargetReadUnavailable",
                    }
            return {
                "allowed": allowed,
                "confirmed_not_submitted": True,
                "reason": "PhoneRedditPrepared"
                if allowed
                else "PhoneRedditSubmissionDisabledOrPreparationRequired",
            }
        if operation == "execute":
            if not self.config.submit_enabled or not self.engine.live_enabled:
                return {
                    "status": "failed_known",
                    "verified": False,
                    "confirmed_not_submitted": True,
                    "submission_started": False,
                    "reason": "PhoneRedditSubmissionDisabled",
                }
            return self.execute(account, payload, execution_job)
        if operation == "verify":
            return self.verify(account, payload)
        raise BackendError("PhoneRedditUnsupportedOperation")

    def execute(self, account, claim, execution_job):
        if not self.config.submit_enabled or not self.engine.live_enabled:
            raise BackendError("PhoneRedditSubmissionDisabled")
        item_id = claim["item_id"]
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (claim["task_id"],))
        if not self.task_authorized(claim["task_id"], account["id"]):
            raise BackendError("PhoneRedditTaskOutsideDevelopmentScope")
        full_claim = {
            "platform": "reddit",
            "account": account["backend_label"],
            "account_id": account["id"],
            "database": self.store.path,
            "operation": "execute",
            "execution_job": execution_job,
            "payload": claim,
        }
        policy = json.loads(task["policy"])
        _, bound = self.binding(account["id"])
        if policy.get("execution_method", "inherit") not in {"inherit", "phone"} or policy.get(
            "execution_node_os", "any"
        ) not in {"any", bound.node_os}:
            raise BackendError("PhoneRedditExecutionMethodOrNodeMismatch")
        verify_claim(full_claim)
        old = self.store.one(
            "SELECT * FROM phone_reddit_authorizations WHERE item_id=?", (item_id,)
        )
        if old:
            return self.verify(account, claim)
        prep = self.store.one(
            "SELECT request FROM phone_reddit_operations WHERE item_id=? AND kind='reddit_prepare' ORDER BY created_at DESC LIMIT 1",
            (item_id,),
        )
        if not prep:
            raise BackendError("PhoneRedditPreparationRequired")
        context = json.loads(prep["request"])["reddit"]
        if (
            context["text"] != claim["payload"]["text"]
            or context["target_url"] != claim["payload"]["target"]
        ):
            raise BackendError("PhoneRedditPreparedContentChanged")
        identifier = "phone-submit:" + uuid4().hex
        context.update(authorization_id=identifier, discard_after_prepare=False)
        request = self.make_request(
            account["id"],
            "reddit_submit",
            context,
            task_id=claim["task_id"],
            item_id=item_id,
            manifest=task["manifest_hash"],
            channel=json.loads(task["policy"]).get("execution_channel", "inherit"),
        )
        from phone_fleet_protocol import fingerprint

        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO phone_reddit_authorizations(id,item_id,request_hash,claim,context,device_id,expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    item_id,
                    fingerprint(request),
                    canonical(full_claim),
                    canonical(context),
                    request.device_id,
                    min(request.deadline, execution_job.get("lease_until", request.deadline)),
                ),
            )
        snapshot = self.dispatch(request, identifier, account["id"], item_id)
        return self.outcome(snapshot, request.reddit)

    def authorize(self, body, bearer):
        if not hmac.compare_digest(bearer, self.config.runtime_token.get_secret_value()):
            raise Conflict("PhoneRedditRuntimeAuthenticationRequired")
        if not self.config.submit_enabled or not self.engine.live_enabled:
            raise Conflict("PhoneRedditSubmissionDisabled")
        row = self.store.one(
            "SELECT * FROM phone_reddit_authorizations WHERE id=?", (body["authorization_id"],)
        )
        if (
            not row
            or row["used_at"] is not None
            or row["expires_at"] <= time.time()
            or body["request_sha256"] != row["request_hash"]
            or body["device_id"] != row["device_id"]
        ):
            raise Conflict("PhoneRedditSubmitPermissionFenced")
        operation = self.store.one(
            "SELECT execution_id FROM phone_reddit_operations WHERE id=?", (row["id"],)
        )
        if not operation or operation["execution_id"] != body["execution_id"]:
            raise Conflict("PhoneRedditExecutionCorrelationMismatch")
        verify_claim(json.loads(row["claim"]))
        with self.store.connect(write=True) as db:
            claim = json.loads(row["claim"])
            if not self.task_authorized(claim["payload"]["task_id"], claim["account_id"], db=db):
                raise Conflict("PhoneRedditTaskOutsideDevelopmentScope")
            task = db.execute(
                "SELECT * FROM tasks WHERE id=?", (claim["payload"]["task_id"],)
            ).fetchone()
            self.engine._integrity(db, task, "outbound")
            from .phone_rounds import submission_blocker

            reason = submission_blocker(db, task["id"], time.time())
            if reason:
                raise Conflict(reason)
            if (
                task["state"] != "active"
                or db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0] != "0"
            ):
                raise Conflict("PhoneRedditStopped")
            changed = db.execute(
                "UPDATE phone_reddit_authorizations SET used_at=?,native_id=?,execution_id=? WHERE id=? AND used_at IS NULL AND expires_at>?",
                (
                    time.time(),
                    body["native_command_id"],
                    body["execution_id"],
                    row["id"],
                    time.time(),
                ),
            ).rowcount
            if changed != 1:
                raise Conflict("PhoneRedditSubmitPermissionAlreadyUsed")
            db.execute(
                "UPDATE items SET result=? WHERE id=? AND status='executing'",
                (
                    canonical(
                        {"submission_started": True, "execution_step": "phone_reddit_submit"}
                    ),
                    row["item_id"],
                ),
            )
            Store.audit(
                db,
                "phone_reddit.submit_authorized",
                {"item_id": row["item_id"], "native_command_id": body["native_command_id"]},
                claim["payload"]["task_id"],
                time.time(),
            )
        return {
            "allowed": True,
            "submit_once": True,
            "request_sha256": row["request_hash"],
            "native_command_id": body["native_command_id"],
            "expires_at": time.time() + 2,
        }

    @staticmethod
    def outcome(snapshot, context):
        from phone_fleet_protocol import RedditResult, fingerprint

        result = snapshot.get("result") or {}
        try:
            proof = RedditResult.model_validate(result["reddit"]) if result.get("reddit") else None
        except ValueError:
            return {
                "status": "unknown",
                "verified": False,
                "submission_started": None,
                "reason": "PhoneRedditInvalidIndependentReceipt",
            }
        from .publication_receipts import outcome_v2

        if proof and proof.receipt_v2 is not None:
            return outcome_v2(snapshot, context, proof)
        receipt = proof.receipt if proof else None
        if (
            snapshot.get("status") == "succeeded"
            and result.get("simulated") is False
            and result.get("external_effect") == "confirmed"
            and proof
            and proof.submission_started
            and receipt
            and receipt.post_id == "t3_" + context.post_id
            and receipt.author == context.identity.removeprefix("u/")
            and receipt.text_sha256 == fingerprint({"text": context.text})
            and proof.submitted_at
            and receipt.created_utc >= proof.submitted_at
        ):
            return {
                "status": "confirmed",
                "verified": True,
                "external_reference": receipt.permalink,
                "comment_id": receipt.comment_id,
                "native_comment_id": receipt.comment_id,
                "published_at": receipt.created_utc,
                "submission_started": True,
            }
        if (
            snapshot.get("status") == "failed"
            and result.get("external_effect") == "none"
            and result.get("quiescent") is True
            and not (proof and proof.submission_started)
        ):
            return {
                "status": "failed_known",
                "verified": False,
                "confirmed_not_submitted": True,
                "submission_started": False,
                "reason": result.get("code", "PhoneRedditStoppedBeforeSubmit"),
            }
        return {
            "status": "unknown",
            "verified": False,
            "submission_started": proof.submission_started if proof else None,
            "reason": "PhoneRedditIndependentReceiptRequired",
        }

    def verify(self, account, payload):
        from .execution_timing import stage

        item = self.store.one("SELECT task_id FROM items WHERE id=?", (payload.get("item_id"),))
        with stage(
            self.engine,
            item["task_id"] if item else None,
            "receipt_verification",
            account_id=account["id"],
            item_id=payload.get("item_id"),
        ):
            return self._verify(account, payload)

    def _verify(self, account, payload):
        from phone_fleet_protocol import ExecutionRequest

        item_id = payload.get("item_id")
        row = self.store.one(
            "SELECT * FROM phone_reddit_operations WHERE item_id=? AND kind='reddit_submit' ORDER BY created_at DESC LIMIT 1",
            (item_id,),
        )
        if not row:
            return {
                "status": "unknown",
                "verified": False,
                "reason": "PhoneRedditNoOriginalSubmission",
            }
        if row["account_id"] != account["id"]:
            raise Conflict("PhoneRedditReceiptAccountMismatch")
        from .phone_protocol import is_legacy

        if is_legacy(json.loads(row["request"])):
            return self.read_legacy_operation(row)
        request = ExecutionRequest.model_validate_json(row["request"])
        snapshot = self.dispatch(request, row["id"], account["id"], item_id)
        outcome = self.outcome(snapshot, request.reddit)
        if outcome["verified"]:
            return outcome
        proof = (snapshot.get("result") or {}).get("reddit") or {}
        if proof.get("submission_started") and proof.get("submitted_at"):
            try:
                receipt = asyncio.run(self.reader.receipt(request.reddit, proof["submitted_at"]))
            except Exception:
                receipt = None
            if receipt:
                return {
                    "status": "confirmed",
                    "verified": True,
                    "external_reference": receipt.permalink,
                    "comment_id": receipt.comment_id,
                    "native_comment_id": receipt.comment_id,
                    "published_at": receipt.created_utc,
                    "submission_started": True,
                }
        return outcome


def attach_worker(engine):
    import os
    from pathlib import Path

    path = os.getenv("AP_PHONE_REDDIT_CONFIG")
    if not path:
        return
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if Path(value["database"]).resolve() != Path(engine.store.path).resolve():
        raise Conflict("PhoneRedditWorkerDatabaseMismatch")
    engine.adapters.phone_reddit = PhoneReddit(engine, value["config"])
