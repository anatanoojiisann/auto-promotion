"""Opt-in five-App phone service on the existing AP task/claim/receipt lifecycle."""

from __future__ import annotations

import hmac
import json
import threading
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .backends.common import BackendError, read_profile, save_profile, verify_claim
from .core import Conflict
from .models import canonical, digest
from .phone_reddit import PhoneReddit, RedditPhoneConfig
from .store import Store

ACTIONS = {"telegram": "send_message", "x": "reply", "instagram": "comment",
           "facebook": "comment", "tiktok": "comment"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_social_operations(
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL,item_id TEXT,kind TEXT NOT NULL,
 request TEXT NOT NULL,phase TEXT NOT NULL,execution_id TEXT,result TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS phone_social_authorizations(
 id TEXT PRIMARY KEY,item_id TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,
 claim TEXT NOT NULL,context TEXT NOT NULL,device_id TEXT NOT NULL,expires_at REAL NOT NULL,
 used_at REAL,native_id TEXT,execution_id TEXT
);
CREATE TABLE IF NOT EXISTS phone_social_accounts(
 account_id TEXT PRIMARY KEY,platform TEXT NOT NULL,identity TEXT NOT NULL,device_id TEXT NOT NULL,
 observation TEXT NOT NULL,observed_at REAL NOT NULL
);
"""


class SocialBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    platform: Literal["telegram", "x", "instagram", "facebook", "tiktok"]
    identity: str = Field(min_length=1, max_length=128)
    node_os: Literal["windows", "macos"] = "windows"


class SocialPhoneConfig(RedditPhoneConfig):
    bindings: dict[str, SocialBinding]
    runtime_token: SecretStr = Field(min_length=24)


def validate_item(account, payload):
    """A phone binding never widens the existing platform's action set."""
    from phone_fleet_protocol.social import target_parts

    if (payload.get("action") != ACTIONS.get(account["platform"]) or payload.get("media")
            or payload.get("title") or payload.get("reply_to") or payload.get("recipient_id")
            or payload.get("discord_reply_to") or payload.get("tiktok_comment_id")
            or payload.get("tiktok_options") or payload.get("tiktok_conversation_id")
            or payload.get("tiktok_message_id")):
        raise BackendError("PhoneSocialOnlyCommissionedTopLevelTextAction")
    text = payload.get("text", "")
    if not text.strip() or len(text) > 2000 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise BackendError("PhoneSocialPlainTextRequired")
    parts = target_parts(account["platform"], payload["target"])
    if payload.get("tiktok_video_id") and payload["tiktok_video_id"] != parts[-1]:
        raise BackendError("PhoneSocialNativeTargetMismatch")


class PhoneSocial(PhoneReddit):
    # Only transport/token refresh/durable create-or-lookup are shared with Reddit.
    operations_table = "phone_social_operations"

    def __init__(self, engine, config, *, transport=None):
        from phone_fleet.auth import KeycloakAuth
        from phone_fleet.client import trusted_url

        self.engine, self.store = engine, engine.store
        self.config = SocialPhoneConfig.model_validate(config)
        if not self.config.enabled:
            raise Conflict("PhoneSocialDevelopmentDisabled")
        self.url = trusted_url(self.config.fleet_url, development=True)
        self.transport, self.token_lock = transport, threading.Lock()
        self.cached_token, self.token_until = None, 0
        if self.config.oauth_issuer:
            KeycloakAuth.validate_endpoint(self.config.oauth_issuer, True)
            if not self.config.oauth_client_id or not self.config.oauth_client_secret:
                raise Conflict("PhoneSocialOAuthClientRequired")
        with self.store.connect(write=True) as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
        engine.adapters.phone_social = self

    def worker_config(self):
        import os

        path = Path(self.store.path).parent / "phone-social-worker.private.json"
        value = self.config.model_dump(mode="json")
        for name in ("service_token", "runtime_token", "oauth_client_secret", "reddit_read_access_token"):
            secret = getattr(self.config, name)
            if secret is not None:
                value[name] = secret.get_secret_value()
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump({"database": str(Path(self.store.path).resolve()), "config": value}, stream)
        temporary.replace(path)
        return str(path)

    def binding(self, account_id):
        account = self.engine.account(account_id)
        binding = self.config.bindings.get(account_id)
        if not binding or account["mode"] != "live" or account["platform"] != binding.platform:
            raise Conflict("PhoneSocialAccountNotBound")
        return account, binding

    def make_request(self, account_id, operation, context, *, task_id=None, item_id=None,
                     manifest=None, channel="local"):
        from phone_fleet_protocol import ExecutionRequest, fingerprint
        from phone_fleet_protocol.social import SocialIdentity, SocialInteraction

        _, binding = self.binding(account_id)
        try:
            context = (SocialIdentity(platform=binding.platform, identity=binding.identity) if operation == "social_identity"
                else SocialInteraction.model_validate({**context, "platform": binding.platform,
                    "action": ACTIONS[binding.platform], "identity": binding.identity}))
        except ValueError:
            raise Conflict("PhoneSocialImmutableContextInvalid") from None
        now = time.time()
        return ExecutionRequest(business_task_id=task_id or "phone-health:" + uuid4().hex,
            task_item_id=item_id or "identity", manifest_sha256=manifest or digest({"identity": binding.identity}),
            account_id=account_id, device_id=binding.device_id, operation=operation,
            execution_channel=channel, node_os=binding.node_os, not_before=now, deadline=now + 115,
            expected_identity_sha256=fingerprint({"account": binding.identity}), social=context)

    def register(self, account_id, context=None):
        account, binding = self.binding(account_id)
        previous = {}
        try:
            previous = read_profile(account["platform"], account["backend_label"])
        except BackendError as exc:
            if exc.safe_code != "AccountNotConfigured":
                raise
        else:
            if previous.get("connection_type") != "phone_fleet":
                raise Conflict("PhoneSocialExistingConnectionPreserved")
            old = previous.get("phone_fleet", {})
            if old.get("device_id") != binding.device_id or previous.get("identity") != binding.identity:
                raise Conflict("PhoneSocialExistingBindingPreserved")
        request = self.make_request(account_id, "social_identity", context)
        snapshot = self.dispatch(request, "phone-social-identity:" + uuid4().hex, account_id)
        result = snapshot.get("result") or {}
        proof = result.get("social") or {}
        if (snapshot.get("status") != "succeeded" or result.get("simulated") is not False
                or result.get("observed_identity_sha256") != request.expected_identity_sha256
                or proof.get("platform") != binding.platform or proof.get("identity") != binding.identity):
            raise Conflict("PhoneSocialFreshIdentityRequired")
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO phone_social_accounts VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET platform=excluded.platform,identity=excluded.identity,device_id=excluded.device_id,observation=excluded.observation,observed_at=excluded.observed_at",
                (account_id, binding.platform, binding.identity, binding.device_id, canonical(snapshot), time.time()))
        save_profile(binding.platform, account["backend_label"], {
            **previous,
            "identity": binding.identity, "connection_type": "phone_fleet",
            "phone_fleet": {**previous.get("phone_fleet", {}), "device_id": binding.device_id, "node_os": binding.node_os,
                            "social_actions": [ACTIONS[binding.platform]]}})
        self.engine.check_account(account_id, force=True)
        return {"identity_verified": True, "identity": binding.identity, "platform": binding.platform,
                "submission_enabled": self.config.submit_enabled, "source_execution_id": snapshot["id"]}

    def prepare(self, item_id, metadata, *, discard=True):
        item = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not item or item["status"] != "not_attempted" or item["attempt_id"]:
            raise Conflict("PhoneSocialPreparationRequiresUnattemptedItem")
        payload = json.loads(item["payload"])
        account, binding = self.binding(item["account_id"])
        validate_item(account, payload)
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (item["task_id"],))
        policy = json.loads(task["policy"])
        self.check_method(policy, binding)
        identifier = "phone-social-prepare:" + uuid4().hex
        request = self.make_request(account["id"], "social_prepare", {**metadata,
            "text": payload["text"], "target_url": payload["target"], "discard_after_prepare": discard},
            task_id=task["id"], item_id=item_id + ":" + identifier,
            manifest=task["manifest_hash"] or item["payload_hash"], channel=policy["execution_channel"])
        snapshot = self.dispatch(request, identifier, account["id"], item_id)
        observed = snapshot.get("result") or {}
        if (snapshot.get("status") == "succeeded" and observed.get("simulated") is False
                and observed.get("observed_identity_sha256") == request.expected_identity_sha256
                and type(observed.get("observed_at")) in {int, float}):
            with self.store.connect(write=True) as db:
                db.execute("UPDATE phone_social_accounts SET observation=?,observed_at=? WHERE account_id=? AND identity=? AND device_id=?",
                    (canonical(snapshot), observed["observed_at"], account["id"], binding.identity, binding.device_id))
        return snapshot

    @staticmethod
    def check_method(policy, binding):
        if (policy.get("execution_method", "inherit") not in {"inherit", "phone"}
                or policy.get("execution_node_os", "any") not in {"any", binding.node_os}):
            raise BackendError("PhoneSocialExecutionMethodOrNodeMismatch")

    def fresh(self, account):
        row = self.store.one("SELECT * FROM phone_social_accounts WHERE account_id=?", (account["id"],))
        binding = self.config.bindings.get(account["id"])
        return bool(row and binding and account["platform"] == binding.platform
                    and row["platform"] == binding.platform and row["identity"] == binding.identity
                    and row["device_id"] == binding.device_id and 0 <= time.time() - row["observed_at"] < 300)

    def preparation(self, account_id, payload):
        # Select by immutable content, not whichever item happened to prepare last.
        rows = self.store.rows("SELECT * FROM phone_social_operations WHERE account_id=? AND kind='social_prepare' ORDER BY created_at DESC LIMIT 1000", (account_id,))
        for row in rows:
            request = json.loads(row["request"])
            context = request["social"]
            if (context["text"] == payload.get("text") and context["target_url"] == payload.get("target")
                    and context["action"] == payload.get("action")):
                return row, request, json.loads(row["result"])
        return None, None, None

    def backend(self, account, operation, payload, execution_job=None):
        _, binding = self.binding(account["id"])
        fresh = self.fresh(account)
        if operation == "health":
            return {"healthy": fresh, "identity_verified": fresh, "service_ready": True,
                "permissions": [ACTIONS[binding.platform]] if fresh else [],
                "account_identity": binding.identity if fresh else None,
                "reason": "phone_social_identity_observed" if fresh else "PhoneSocialFreshIdentityRequired"}
        if operation == "preflight":
            _, request, snapshot = self.preparation(account["id"], payload.get("payload", {}))
            proof = ((snapshot or {}).get("result") or {}).get("social") or {}
            allowed = bool(self.config.submit_enabled and self.engine.live_enabled and fresh and request
                and snapshot.get("status") == "succeeded" and proof.get("receipt_capable") is True and proof.get("adapter_sha256")
                and proof.get("editor_verified") is True and proof.get("submit_control_observed") is True)
            return {"allowed": allowed, "confirmed_not_submitted": True,
                "reason": "PhoneSocialPrepared" if allowed else "PhoneSocialSubmissionDisabledOrReceiptCommissioningRequired"}
        if operation == "execute":
            if not self.config.submit_enabled or not self.engine.live_enabled:
                return {"status": "failed_known", "verified": False, "confirmed_not_submitted": True,
                        "submission_started": False, "reason": "PhoneSocialSubmissionDisabled"}
            return self.execute(account, payload, execution_job)
        if operation == "verify":
            return self.verify(account, payload)
        raise BackendError("PhoneSocialUnsupportedOperation")

    def execute(self, account, claim, execution_job):
        from phone_fleet_protocol import fingerprint

        if not self.config.submit_enabled or not self.engine.live_enabled:
            raise BackendError("PhoneSocialSubmissionDisabled")
        _, binding = self.binding(account["id"])
        if claim["task_id"] not in self.config.authorized_task_ids:
            raise BackendError("PhoneSocialTaskOutsideDevelopmentScope")
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (claim["task_id"],))
        policy = json.loads(task["policy"])
        self.check_method(policy, binding)
        validate_item(account, claim["payload"])
        full_claim = {"platform": account["platform"], "account": account["backend_label"], "account_id": account["id"],
            "database": self.store.path, "operation": "execute", "execution_job": execution_job, "payload": claim}
        verify_claim(full_claim)
        old = self.store.one("SELECT * FROM phone_social_authorizations WHERE item_id=?", (claim["item_id"],))
        if old:
            return self.verify(account, claim)
        row, prepared, snapshot = self.preparation(account["id"], claim["payload"])
        proof = ((snapshot or {}).get("result") or {}).get("social") or {}
        if (not row or snapshot.get("status") != "succeeded" or proof.get("receipt_capable") is not True or not proof.get("adapter_sha256")
                or row["item_id"] != claim["item_id"] or prepared["business_task_id"] != claim["task_id"]):
            raise BackendError("PhoneSocialReceiptCommissionedPreparationRequired")
        identifier = "phone-social-submit:" + uuid4().hex
        context = {**prepared["social"], "authorization_id": identifier, "discard_after_prepare": False,
                   "expected_adapter_sha256": proof["adapter_sha256"]}
        request = self.make_request(account["id"], "social_submit", context, task_id=task["id"],
            item_id=claim["item_id"], manifest=task["manifest_hash"], channel=policy["execution_channel"])
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO phone_social_authorizations(id,item_id,request_hash,claim,context,device_id,expires_at) VALUES(?,?,?,?,?,?,?)",
                (identifier, claim["item_id"], fingerprint(request), canonical(full_claim), canonical(context),
                 binding.device_id, min(request.deadline, (execution_job or {}).get("lease_until", request.deadline))))
        return self.outcome(self.dispatch(request, identifier, account["id"], claim["item_id"]), request.social)

    def authorize(self, body, bearer):
        if not hmac.compare_digest(bearer, self.config.runtime_token.get_secret_value()):
            raise Conflict("PhoneSocialRuntimeAuthenticationRequired")
        if not self.config.submit_enabled or not self.engine.live_enabled:
            raise Conflict("PhoneSocialSubmissionDisabled")
        row = self.store.one("SELECT * FROM phone_social_authorizations WHERE id=?", (body["authorization_id"],))
        if (not row or row["used_at"] is not None or row["expires_at"] <= time.time()
                or body["request_sha256"] != row["request_hash"] or body["device_id"] != row["device_id"]):
            raise Conflict("PhoneSocialSubmitPermissionFenced")
        operation = self.store.one("SELECT execution_id FROM phone_social_operations WHERE id=?", (row["id"],))
        if not operation or operation["execution_id"] != body["execution_id"]:
            raise Conflict("PhoneSocialExecutionCorrelationMismatch")
        verify_claim(json.loads(row["claim"]))
        if json.loads(row["claim"])["payload"]["task_id"] not in self.config.authorized_task_ids:
            raise Conflict("PhoneSocialTaskOutsideDevelopmentScope")
        with self.store.connect(write=True) as db:
            claim = json.loads(row["claim"])
            task = db.execute("SELECT * FROM tasks WHERE id=?", (claim["payload"]["task_id"],)).fetchone()
            self.engine._integrity(db, task, "outbound")
            if task["state"] != "active" or db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0] != "0":
                raise Conflict("PhoneSocialStopped")
            changed = db.execute("UPDATE phone_social_authorizations SET used_at=?,native_id=?,execution_id=? WHERE id=? AND used_at IS NULL AND expires_at>?",
                (time.time(), body["native_command_id"], body["execution_id"], row["id"], time.time())).rowcount
            if changed != 1:
                raise Conflict("PhoneSocialSubmitPermissionAlreadyUsed")
            db.execute("UPDATE items SET result=? WHERE id=? AND status='executing'",
                (canonical({"submission_started": True, "execution_step": "phone_social_submit"}), row["item_id"]))
            Store.audit(db, "phone_social.submit_authorized", {"item_id": row["item_id"],
                "native_command_id": body["native_command_id"]}, task["id"], time.time())
        return {"allowed": True, "submit_once": True, "request_sha256": row["request_hash"],
                "native_command_id": body["native_command_id"], "expires_at": min(time.time() + 2, row["expires_at"])}

    @staticmethod
    def outcome(snapshot, context):
        from phone_fleet_protocol.social import SocialResult, receipt_matches

        result = snapshot.get("result") or {}
        try:
            proof = SocialResult.model_validate(result["social"]) if result.get("social") else None
        except ValueError:
            return {"status": "unknown", "verified": False, "submission_started": None,
                    "reason": "PhoneSocialInvalidIndependentReceipt"}
        if (snapshot.get("status") == "succeeded" and result.get("simulated") is False
                and result.get("external_effect") == "confirmed" and proof and proof.submission_started
                and receipt_matches(proof.receipt, context, proof.submitted_at)):
            receipt = proof.receipt
            return {"status": "confirmed", "verified": True, "external_reference": receipt.permalink,
                "native_id": receipt.native_id, "message_id": receipt.native_id if context.platform == "telegram" else None,
                "native_comment_id": receipt.native_id, "published_at": receipt.created_at, "submission_started": True}
        if (snapshot.get("status") == "failed" and result.get("external_effect") == "none"
                and result.get("quiescent") is True and not (proof and proof.submission_started)):
            return {"status": "failed_known", "verified": False, "confirmed_not_submitted": True,
                    "submission_started": False, "reason": result.get("code", "PhoneSocialStoppedBeforeSubmit")}
        return {"status": "unknown", "verified": False, "submission_started": proof.submission_started if proof else None,
                "reason": "PhoneSocialIndependentNativeReceiptRequired"}

    def verify(self, account, payload):
        from phone_fleet_protocol import ExecutionRequest

        row = self.store.one("SELECT * FROM phone_social_operations WHERE item_id=? AND kind='social_submit' ORDER BY created_at DESC LIMIT 1", (payload.get("item_id"),))
        if not row:
            return {"status": "unknown", "verified": False, "reason": "PhoneSocialNoOriginalSubmission"}
        if row["account_id"] != account["id"]:
            raise Conflict("PhoneSocialReceiptAccountMismatch")
        request = ExecutionRequest.model_validate_json(row["request"])
        # dispatch on an existing durable record only GETs its original execution.
        return self.outcome(self.dispatch(request, row["id"], account["id"], row["item_id"]), request.social)


def attach_worker(engine):
    import os

    path = os.getenv("AP_PHONE_SOCIAL_CONFIG")
    if not path:
        return
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if Path(value["database"]).resolve() != Path(engine.store.path).resolve():
        raise Conflict("PhoneSocialWorkerDatabaseMismatch")
    engine.adapters.phone_social = PhoneSocial(engine, value["config"])
