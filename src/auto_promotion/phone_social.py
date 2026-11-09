"""Opt-in five-App phone service on the existing AP task/claim/receipt lifecycle."""

from __future__ import annotations

import hmac
import json
import threading
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .backends.common import BackendError, read_profile, save_profile, verify_claim
from .core import Conflict
from .models import canonical, digest, epoch
from .phone_reddit import PhoneReddit, RedditPhoneConfig
from .phone_telegram import JOIN_SCHEMA, PhoneTelegram
from .store import Store
from .telegram_phone_observer import TelegramObserverConfig, TelegramPhoneObserver

ACTIONS = {"telegram": "send_message", "x": "reply", "instagram": "comment",
           "facebook": "comment", "tiktok": "comment"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_social_operations(
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL,item_id TEXT,kind TEXT NOT NULL,
 request TEXT NOT NULL,phase TEXT NOT NULL,execution_id TEXT,result TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS phone_social_preparation_lookup ON phone_social_operations(account_id,kind,item_id,created_at);
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
    receipt_reader: Literal["telegram_api"] | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)
    device_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    platform: Literal["telegram", "x", "instagram", "facebook", "tiktok"]
    identity: str = Field(min_length=1, max_length=128)
    node_os: Literal["windows", "macos"] = "windows"
    client_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,80}$")

    @model_validator(mode="after")
    def telegram_client(self):
        if self.platform!="telegram" and (self.client_id is not None or self.receipt_reader is not None):
            raise ValueError("Client and protocol reader bindings apply only to Telegram")
        return self


class CommissionedSinglePass(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    adapter_id: str = Field(min_length=1, max_length=100)
    adapter_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SocialPhoneConfig(RedditPhoneConfig):
    dynamic_task_authorization_enabled: bool = False
    async_visual_review_enabled: bool = False
    single_pass_adapters: dict[str, CommissionedSinglePass] = Field(default_factory=dict)
    single_pass_task_ids: list[str] = Field(default_factory=list)
    join_enabled: bool = False
    telegram_observers: dict[str,TelegramObserverConfig] = Field(default_factory=dict)
    bindings: dict[str, SocialBinding]
    runtime_token: SecretStr = Field(min_length=24)

    @model_validator(mode="after")
    def observer_scope(self):
        users = []
        for account_id,observer in self.telegram_observers.items():
            if account_id not in self.bindings or self.bindings[account_id].platform!="telegram":
                raise ValueError("Protocol observer must bind a registered Telegram account")
            users.append(observer.user_id)
        if len(set(users))!=len(users):
            raise ValueError("One Telegram API identity cannot become multiple phone senders")
        if any(b.receipt_reader=="telegram_api" and a not in self.telegram_observers for a,b in self.bindings.items()):
            raise ValueError("Protocol receipt mode requires a same-account observer authorization")
        return self


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


class PhoneSocial(PhoneTelegram, PhoneReddit):
    # Only transport/token refresh/durable create-or-lookup are shared with Reddit.
    operations_table = "phone_social_operations"

    def __init__(self, engine, config, *, transport=None,telegram_observer=None):
        from phone_fleet.auth import KeycloakAuth
        from phone_fleet.client import trusted_url

        self.engine, self.store = engine, engine.store
        self.config = SocialPhoneConfig.model_validate(config)
        self.telegram_observer = telegram_observer or (TelegramPhoneObserver(self.config.telegram_observers) if self.config.telegram_observers else None)
        if not self.config.enabled:
            raise Conflict("PhoneSocialDevelopmentDisabled")
        self.url = trusted_url(self.config.fleet_url, development=True)
        self.transport, self.token_lock = transport, threading.Lock()
        self.cached_token, self.token_until = None, 0
        self.refresh_lock, self.account_refresh_locks = threading.Lock(), {}
        if self.config.oauth_issuer:
            KeycloakAuth.validate_endpoint(self.config.oauth_issuer, True)
            if not self.config.oauth_client_id or not self.config.oauth_client_secret:
                raise Conflict("PhoneSocialOAuthClientRequired")
        with self.store.connect(write=True) as db:
            for statement in (SCHEMA+JOIN_SCHEMA).split(";"):
                if statement.strip():
                    db.execute(statement)
        engine.adapters.phone_social = self
        from .temporal_tasks import commission_dynamic_admission

        commission_dynamic_admission(self)

    def single_pass_adapter(self, account_id, task_id=None):
        from .phone_single_pass import adapter

        return adapter(self, account_id, task_id)

    def worker_config(self):
        from .phone_private import write_worker_config

        return write_worker_config(self.store.path, self.config, "social")

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
            context = (SocialIdentity(platform=binding.platform, identity=binding.identity, client_id=binding.client_id) if operation == "social_identity"
                else SocialInteraction.model_validate({**context, "platform": binding.platform,
                    "action": ACTIONS[binding.platform], "identity": binding.identity, "client_id":binding.client_id,"receipt_reader":binding.receipt_reader}))
        except ValueError:
            raise Conflict("PhoneSocialImmutableContextInvalid") from None
        now = time.time()
        return ExecutionRequest(business_task_id=task_id or "phone-health:" + uuid4().hex,
            task_item_id=item_id or "identity", manifest_sha256=manifest or digest({"identity": binding.identity}),
            account_id=account_id, device_id=binding.device_id, operation=operation,
            execution_channel=channel, node_os=binding.node_os, not_before=now, deadline=now + 115,
            expected_identity_sha256=fingerprint({"account": binding.identity}), social=context)

    def shared_telegram_binding(self, account, profile=None):
        """Allow one existing API identity to use its own AP queue for explicit phone tasks."""
        account_id = account["id"]
        binding = self.config.bindings.get(account_id)
        observer = self.config.telegram_observers.get(account_id)
        if (account["platform"] != "telegram" or not binding or binding.receipt_reader != "telegram_api"
                or not observer or self.telegram_queue_blocker(account_id)
                or observer.backend_label != account["backend_label"]):
            return None
        if profile is None:
            try:
                profile = read_profile("telegram", account["backend_label"])
            except BackendError:
                return None
        if (profile.get("connection_type") not in (None, "mtproto")
                or str(profile.get("identity")) != str(observer.user_id)
                or not profile.get("api_id") or not profile.get("api_hash")):
            return None
        return binding

    def shared_account_binding(self, account, profile=None):
        """Use a verified existing account on an explicit phone task without replacing its profile."""
        if account.get("platform") == "telegram":
            return self.shared_telegram_binding(account, profile)
        platform = account.get("platform")
        binding = self.config.bindings.get(account.get("id"))
        if account.get("mode") != "live" or not binding or binding.platform != platform:
            return None
        if profile is None:
            try:
                profile = read_profile(platform, account["backend_label"])
            except BackendError:
                return None
        connection = profile.get("connection_type")
        supported = (
            platform in {"instagram", "facebook"} and connection == "meta_desktop"
            or platform == "tiktok" and connection == "tiktok_desktop"
            or platform == "x" and connection in (None, "x_api")
        )
        if not supported:
            return None
        if platform == "x" and connection is None and profile.get("browser_channel", "chromium") not in {
            "chromium", "chrome", "msedge", "tabbit", "browser_use"
        }:
            return None
        stored = profile.get("identity")
        if not isinstance(stored, str) or not stored or not account.get("identity_hash"):
            return None
        if digest([platform, stored]) != account["identity_hash"]:
            return None
        from phone_fleet_protocol.social import SocialIdentity

        try:
            SocialIdentity(platform=platform, identity=binding.identity, client_id=binding.client_id)
        except ValueError:
            return None
        stored_identity = stored.casefold() if platform == "facebook" else stored.lstrip("@").casefold()
        bound_identity = binding.identity.casefold() if platform == "facebook" else binding.identity.lstrip("@").casefold()
        if stored_identity != bound_identity:
            return None
        return binding

    def register(self, account_id, context=None):
        account, binding = self.binding(account_id)
        previous = {}
        shared = False
        try:
            previous = read_profile(account["platform"], account["backend_label"])
        except BackendError as exc:
            if exc.safe_code != "AccountNotConfigured":
                raise
        else:
            if previous.get("connection_type") != "phone_fleet":
                shared = bool(self.shared_account_binding(account, previous))
                if not shared:
                    raise Conflict("PhoneSocialExistingConnectionPreserved")
            else:
                old = previous.get("phone_fleet", {})
                if old.get("device_id") != binding.device_id or previous.get("identity") != binding.identity or old.get("client_id") != binding.client_id:
                    raise Conflict("PhoneSocialExistingBindingPreserved")
        request = self.make_request(account_id, "social_identity", context)
        snapshot = self.dispatch(request, "phone-social-identity:" + uuid4().hex, account_id)
        result = snapshot.get("result") or {}
        proof = result.get("social") or {}
        if (snapshot.get("status") != "succeeded" or result.get("simulated") is not False
                or result.get("observed_identity_sha256") != request.expected_identity_sha256
                or proof.get("platform") != binding.platform or proof.get("identity") != binding.identity
                or proof.get("client_id") != binding.client_id):
            raise Conflict("PhoneSocialFreshIdentityRequired")
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO phone_social_accounts VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET platform=excluded.platform,identity=excluded.identity,device_id=excluded.device_id,observation=excluded.observation,observed_at=excluded.observed_at",
                (account_id, binding.platform, binding.identity, binding.device_id, canonical(snapshot), time.time()))
        if not shared:
            save_profile(binding.platform, account["backend_label"], {
                **previous,
                "identity": binding.identity, "connection_type": "phone_fleet",
                "phone_fleet": {**previous.get("phone_fleet", {}), "device_id": binding.device_id, "node_os": binding.node_os,
                                "social_actions": [ACTIONS[binding.platform]], "client_id":binding.client_id}})
            self.engine.check_account(account_id, force=True)
        return {"identity_verified": True, "identity": binding.identity, "platform": binding.platform,
                "submission_enabled": self.config.submit_enabled, "source_execution_id": snapshot["id"]}

    def ensure_identity(self, account_id, *, task_id=None):
        """Explicit starts refresh the native proof; routine health remains read-only."""
        with self.refresh_lock:
            lock = self.account_refresh_locks.setdefault(account_id, threading.RLock())
        with lock:
            account, binding = self.binding(account_id)
            if account.get("disabled") or account.get("cooldown_until",0) > time.time():
                raise Conflict("PhoneSocialAccountStoppedOrCoolingDown")
            if self.fresh(account):
                return {"refreshed":False,"identity_verified":True}
            if task_id:
                from .phone_visual_preparation import trusted_ready

                if trusted_ready(self, account_id, task_id):
                    return {"refreshed":False,"identity_verified":False,"native_identity_check_pending":True}
            previous = self.store.one("SELECT * FROM phone_social_operations WHERE account_id=? AND kind='social_identity' ORDER BY created_at DESC LIMIT 1",(account_id,))
            if previous:
                snapshot = json.loads(previous["result"])
                if previous["phase"] != "linked" or snapshot.get("status") in {"queued","leased","running","unknown"}:
                    from phone_fleet_protocol import ExecutionRequest

                    from .phone_protocol import is_legacy
                    if is_legacy(json.loads(previous["request"])):
                        self.read_legacy_operation(previous)
                        raise Conflict("PhoneSocialIdentityReconciliationRequired")
                    snapshot = self.dispatch(ExecutionRequest.model_validate_json(previous["request"]),previous["id"],account_id)
                    if snapshot.get("status") not in {"succeeded","failed","cancelled"} and not snapshot.get("resource_released"):
                        raise Conflict("PhoneSocialIdentityReconciliationRequired")
            value = self.register(account_id)
            if not self.fresh(self.engine.account(account_id)):
                raise Conflict("PhoneSocialFreshIdentityRequired")
            with self.store.connect(write=True) as db:
                Store.audit(db,"phone_social.identity_refreshed",{"account_id":account_id,
                    "client_id":binding.client_id,"source_execution_id":value["source_execution_id"]},task_id,time.time())
            return {**value,"refreshed":True}

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
        if binding.platform == "telegram" and binding.receipt_reader == "telegram_api":
            self.observer_baseline(account["id"], binding.identity, payload["target"])
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
        from .phone_protocol import expected_version
        row = self.store.one("SELECT * FROM phone_social_accounts WHERE account_id=?", (account["id"],))
        binding = self.config.bindings.get(account["id"])
        return bool(row and binding and account["platform"] == binding.platform
                    and row["platform"] == binding.platform and row["identity"] == binding.identity
                    and row["device_id"] == binding.device_id
                    and json.loads(row["observation"]).get("request",{}).get("version") == expected_version()
                    and json.loads(row["observation"]).get("request",{}).get("social",{}).get("client_id") == binding.client_id
                    and 0 <= time.time() - row["observed_at"] < 300)

    def preparation(self, account_id, payload, *, item_id=None, task_id=None):
        # Bind to the original item when executing; do not scan unrelated account history.
        binding = self.config.bindings.get(account_id)
        if not binding:
            return None, None, None
        sql = ("SELECT * FROM phone_social_operations WHERE account_id=? AND kind='social_prepare' "
               "AND json_extract(request,'$.social.text')=? "
               "AND json_extract(request,'$.social.target_url')=? "
               "AND json_extract(request,'$.social.action')=? "
               "AND json_extract(request,'$.social.client_id') IS ? "
               "AND json_extract(request,'$.social.receipt_reader') IS ? "
               "AND json_extract(request,'$.device_id')=?")
        params = [account_id, payload.get("text"), payload.get("target"), payload.get("action"),
                  binding.client_id, binding.receipt_reader, binding.device_id]
        if item_id is not None:
            sql += " AND item_id=?"
            params.append(item_id)
        if task_id is not None:
            sql += " AND json_extract(request,'$.business_task_id')=?"
            params.append(task_id)
        row = self.store.one(sql + " ORDER BY created_at DESC,id DESC LIMIT 1", params)
        if not row:
            return None, None, None
        return row, json.loads(row["request"]), json.loads(row["result"])

    def backend(self, account, operation, payload, execution_job=None):
        _, binding = self.binding(account["id"])
        fresh = self.fresh(account)
        if binding.platform == "telegram" and operation == "join":
            return self.join(account,payload,execution_job)
        if binding.platform == "telegram" and operation == "verify_join":
            return self.verify_join(account,payload)
        if operation == "health":
            identity = binding.identity
            if fresh and self.shared_account_binding(account):
                # The same Telegram account has an API numeric ID and a phone
                # username. Keep its registered canonical identity and history.
                identity = read_profile(binding.platform, account["backend_label"])["identity"]
            return {"healthy": fresh, "identity_verified": fresh, "service_ready": True,
                "permissions": ([ACTIONS[binding.platform]]+(["join"] if binding.platform=="telegram" and self.config.join_enabled else [])) if fresh else [],
                "account_identity": identity if fresh else None,
                "reason": "phone_social_identity_observed" if fresh else "PhoneSocialFreshIdentityRequired"}
        if operation == "preflight":
            from .phone_single_pass import preflight

            if self.single_pass_adapter(account["id"]) and preflight(self, account, payload.get("payload", {}), execution_job):
                return {"allowed": True, "confirmed_not_submitted": True,
                        "identity_verified": fresh, "identity_check": "native_before_submit",
                        "reason": "PhoneTelegramSinglePassWithinClaim"}
            queue_blocker = self.telegram_queue_blocker(account["id"])
            if queue_blocker:
                return {"allowed":False, "confirmed_not_submitted":True, "reason":queue_blocker}
            if binding.platform=="telegram" and binding.receipt_reader=="telegram_api" and self.observer_preflight(account,payload.get("payload",{}),execution_job):
                return {"allowed":True,"confirmed_not_submitted":True,"reason":"PhoneTelegramPrepareWithinClaim"}
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
        queue_blocker = self.telegram_queue_blocker(account["id"])
        if queue_blocker:
            raise BackendError(queue_blocker)
        if not self.task_authorized(claim["task_id"], account["id"]):
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
        single_pass = self.single_pass_adapter(account["id"], claim["task_id"])
        single_baseline = None
        if single_pass:
            from .account_control_state import snapshot as control_state
            from .phone_single_pass import prepare_context

            if not control_state(self.engine, account["id"], task_id=claim["task_id"])["allowed_read"]:
                raise BackendError("PhoneSocialAccountControlNotReady")
            try:
                social, adapter_sha256, single_baseline = prepare_context(self, account, claim)
            except BackendError as error:
                return {"status":"failed_known", "verified":False, "confirmed_not_submitted":True,
                        "submission_started":False, "reason":error.safe_code}
        else:
            row, prepared, snapshot = self.preparation(account["id"], claim["payload"], item_id=claim["item_id"], task_id=claim["task_id"])
            if binding.platform=="telegram" and binding.receipt_reader=="telegram_api" and (not row or row["item_id"]!=claim["item_id"] or prepared["business_task_id"]!=claim["task_id"]):
                try:
                    row,prepared,snapshot = self.prepare_telegram_claim(account,claim)
                except BackendError as error:
                    return {"status":"failed_known","verified":False,"confirmed_not_submitted":True,"submission_started":False,"reason":error.safe_code}
            proof = ((snapshot or {}).get("result") or {}).get("social") or {}
            if (not row or snapshot.get("status") != "succeeded" or proof.get("receipt_capable") is not True or not proof.get("adapter_sha256")
                    or row["item_id"] != claim["item_id"] or prepared["business_task_id"] != claim["task_id"]):
                raise BackendError("PhoneSocialReceiptCommissionedPreparationRequired")
            social, adapter_sha256 = prepared["social"], proof["adapter_sha256"]
        identifier = "phone-social-submit:" + uuid4().hex
        context = {**social, "authorization_id": identifier, "discard_after_prepare": False,
                   "expected_adapter_sha256": adapter_sha256}
        request = self.make_request(account["id"], "social_submit", context, task_id=task["id"],
            item_id=claim["item_id"], manifest=task["manifest_hash"], channel=policy["execution_channel"])
        if context.get("visual_review_id"):
            from .phone_visual_preparation import trusted_ready

            prepared = trusted_ready(self, account["id"], task["id"], item_id=claim["item_id"])
            if not prepared:
                raise BackendError("PhoneVisualPreparationRequired")
            request = type(request).model_validate({**request.model_dump(mode="json"),
                "deadline": min(request.deadline, prepared["expires_at"], epoch(policy["ends_at"]))})
        try:
            baseline = single_baseline or (self.observer_baseline(account["id"],binding.identity,context["target_url"]) if binding.receipt_reader=="telegram_api" else None)
            reservation = self.telegram_observer.reserve_receipt(account["id"]) if baseline is not None else None
        except BackendError as error:
            return {"status":"failed_known","verified":False,"confirmed_not_submitted":True,"submission_started":False,"reason":error.safe_code}
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO phone_social_authorizations(id,item_id,request_hash,claim,context,device_id,expires_at) VALUES(?,?,?,?,?,?,?)",
                (identifier, claim["item_id"], fingerprint(request), canonical(full_claim), canonical(context),
                 binding.device_id, min(request.deadline, (execution_job or {}).get("lease_until", request.deadline))))
            if baseline is not None:
                observer_sha256 = fingerprint(self.config.telegram_observers[account["id"]])
                db.execute("INSERT INTO phone_telegram_observer_receipts(authorization_id,account_id,baseline,observer_sha256) VALUES(?,?,?,?)",
                    (identifier,account["id"],canonical(baseline),observer_sha256))
                db.execute("INSERT INTO phone_telegram_receipt_budgets(authorization_id,account_id,observer_sha256,read_identity,window_end,reserved) VALUES(?,?,?,?,?,?)",
                    (identifier,account["id"],observer_sha256,reservation["read_identity"],reservation["window_end"],reservation["reserved"]))
        return self.outcome(self.dispatch(request, identifier, account["id"], claim["item_id"]), request.social)

    def authorize(self, body, bearer):
        if not hmac.compare_digest(bearer, self.config.runtime_token.get_secret_value()):
            raise Conflict("PhoneSocialRuntimeAuthenticationRequired")
        if body["authorization_id"].startswith("phone-telegram-join:"):
            return self.authorize_join(body)
        if not self.config.submit_enabled or not self.engine.live_enabled:
            raise Conflict("PhoneSocialSubmissionDisabled")
        row = self.store.one("SELECT * FROM phone_social_authorizations WHERE id=?", (body["authorization_id"],))
        if (not row or row["used_at"] is not None or row["expires_at"] <= time.time()
                or body["request_sha256"] != row["request_hash"] or body["device_id"] != row["device_id"]):
            raise Conflict("PhoneSocialSubmitPermissionFenced")
        operation = self.store.one("SELECT execution_id FROM phone_social_operations WHERE id=?", (row["id"],))
        if not operation or operation["execution_id"] != body["execution_id"]:
            raise Conflict("PhoneSocialExecutionCorrelationMismatch")
        context = json.loads(row["context"])
        if context.get("receipt_reader")=="telegram_api":
            from phone_fleet_protocol import fingerprint

            baseline = self.store.one("SELECT * FROM phone_telegram_observer_receipts WHERE authorization_id=?",(row["id"],))
            if (not baseline or time.time()-json.loads(baseline["baseline"])["observed_at"]>120
                    or fingerprint(self.config.telegram_observers.get(baseline["account_id"]))!=baseline["observer_sha256"]):
                raise Conflict("PhoneTelegramObserverBaselineRequired")
            if json.loads(baseline["baseline"]).get("sends_as_self") is not True:
                raise Conflict("PhoneTelegramSenderIdentityUnverified")
            queue_blocker = self.telegram_queue_blocker(baseline["account_id"])
            if queue_blocker:
                raise Conflict(queue_blocker)
            reservation = self.store.one("SELECT * FROM phone_telegram_receipt_budgets WHERE authorization_id=?",(row["id"],))
            if (not reservation or reservation["account_id"]!=baseline["account_id"]
                    or reservation["observer_sha256"]!=baseline["observer_sha256"] or reservation["used"]!=0):
                raise Conflict("PhoneTelegramReceiptBudgetRequired")
            from .telegram_receipt_budget import MIN_RECEIPT_SECONDS
            try:
                self.telegram_observer.check_receipt_reservation(baseline["account_id"],reservation,minimum_seconds=MIN_RECEIPT_SECONDS)
            except BackendError:
                raise Conflict("PhoneTelegramReceiptBudgetRequired") from None
        verify_claim(json.loads(row["claim"]))
        if not self.task_authorized(json.loads(row["claim"])["payload"]["task_id"], json.loads(row["claim"])["account_id"]):
            raise Conflict("PhoneSocialTaskOutsideDevelopmentScope")
        with self.store.connect(write=True) as db:
            claim = json.loads(row["claim"])
            if not self.task_authorized(claim["payload"]["task_id"], claim["account_id"], db=db):
                raise Conflict("PhoneSocialTaskOutsideDevelopmentScope")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (claim["payload"]["task_id"],)).fetchone()
            self.engine._integrity(db, task, "outbound")
            from .phone_rounds import submission_blocker

            reason = submission_blocker(db, task["id"], time.time())
            if reason:
                raise Conflict(reason)
            if task["state"] != "active" or db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0] != "0":
                raise Conflict("PhoneSocialStopped")
            if context.get("receipt_reader")=="telegram_api":
                current = db.execute("SELECT used,window_end FROM phone_telegram_receipt_budgets WHERE authorization_id=?",(row["id"],)).fetchone()
                if not current or current["used"]!=0 or current["window_end"]-time.time()<MIN_RECEIPT_SECONDS:
                    raise Conflict("PhoneTelegramReceiptBudgetRequired")
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
                and proof.client_id == context.client_id
                and (context.receipt_reader!="telegram_api" or (proof.receipt and proof.receipt.source=="telegram_api"))
                and receipt_matches(proof.receipt, context, proof.submitted_at)):
            receipt = proof.receipt
            return {"status": "confirmed", "verified": True, "external_reference": receipt.permalink,
                "native_id": receipt.native_id, "message_id": receipt.native_id if context.platform == "telegram" else None,
                "native_comment_id": receipt.native_id, **({"comment_id": receipt.native_id} if context.platform == "instagram" else {}),
                **({"publish_id": receipt.native_id, "reply_id": receipt.native_id} if context.platform == "x" else {}),
                "published_at": receipt.created_at, "submission_started": True}
        if (snapshot.get("status") == "failed" and result.get("external_effect") == "none"
                and result.get("quiescent") is True and not (proof and proof.submission_started)):
            return {"status": "failed_known", "verified": False, "confirmed_not_submitted": True,
                    "submission_started": False, "reason": result.get("code", "PhoneSocialStoppedBeforeSubmit")}
        return {"status": "unknown", "verified": False, "submission_started": proof.submission_started if proof else None,
                "reason": "PhoneSocialIndependentNativeReceiptRequired", "receipt_pending": bool(proof and proof.submission_started),
                "resource_state": "released" if snapshot.get("resource_released") else "fenced"}

    def read_confirmed_comment_id(self, item):
        """Recover the validated native ID for older confirmed phone receipts without another attempt."""
        account = self.engine.account(item["account_id"])
        result = self.engine.call(account, "verify", {"item_id": item["id"], "payload": json.loads(item["payload"])}, item["task_id"])
        fields = ("reply_id", "publish_id") if account["platform"] == "x" else ("comment_id",)
        mismatch = "PhoneXOriginalReceiptMismatch" if account["platform"] == "x" else "PhoneInstagramOriginalReceiptMismatch"
        native_id = result.get(fields[0])
        if (not result.get("verified") or not isinstance(native_id, str) or not native_id.isascii()
                or not native_id.isdigit() or not item["external_reference"]
                or result.get("external_reference") != item["external_reference"]):
            raise Conflict(mismatch)
        with self.store.connect(write=True) as db:
            original = db.execute("SELECT result FROM items WHERE id=? AND status='confirmed' AND attempt_id=? AND external_reference=?",
                                  (item["id"], item["attempt_id"], item["external_reference"])).fetchone()
            if not original:
                raise Conflict(mismatch)
            recorded = json.loads(original["result"])
            if any(recorded.get(field) not in (None, native_id) for field in fields):
                raise Conflict(mismatch)
            if any(not recorded.get(field) for field in fields):
                recorded.update(dict.fromkeys(fields, native_id))
                db.execute("UPDATE items SET result=? WHERE id=?", (canonical(recorded), item["id"]))
                Store.audit(db, "phone_social." + fields[0] + "_read_back", {"item_id": item["id"], fields[0]: native_id},
                            item["task_id"], self.engine.clock())
        return recorded

    def verify(self, account, payload):
        from .execution_timing import stage

        item = self.store.one("SELECT task_id FROM items WHERE id=?", (payload.get("item_id"),))
        with stage(self.engine, item["task_id"] if item else None, "receipt_verification",
                   account_id=account["id"], item_id=payload.get("item_id")):
            return self._verify(account, payload)

    def _verify(self, account, payload):
        from phone_fleet_protocol import ExecutionRequest

        row = self.store.one("SELECT * FROM phone_social_operations WHERE item_id=? AND kind='social_submit' ORDER BY created_at DESC LIMIT 1", (payload.get("item_id"),))
        if not row:
            return {"status": "unknown", "verified": False, "reason": "PhoneSocialNoOriginalSubmission"}
        if row["account_id"] != account["id"]:
            raise Conflict("PhoneSocialReceiptAccountMismatch")
        from .phone_protocol import is_legacy
        if is_legacy(json.loads(row["request"])):
            return self.read_legacy_operation(row)
        request = ExecutionRequest.model_validate_json(row["request"])
        # dispatch on an existing durable record only GETs its original execution.
        snapshot=self.dispatch(request,row["id"],account["id"],row["item_id"])
        return self.reconcile_protocol_receipt(request,snapshot) or self.outcome(snapshot,request.social)


def attach_worker(engine):
    import os

    path = os.getenv("AP_PHONE_SOCIAL_CONFIG")
    if not path:
        return
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if Path(value["database"]).resolve() != Path(engine.store.path).resolve():
        raise Conflict("PhoneSocialWorkerDatabaseMismatch")
    engine.adapters.phone_social = PhoneSocial(engine, value["config"])
