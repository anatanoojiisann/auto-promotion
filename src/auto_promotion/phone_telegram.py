"""Telegram phone membership uses the original qualified candidate, join attempt and pool lease."""
import json
import time
from uuid import uuid4

from .backends.common import BackendError, verify_claim
from .core import Conflict
from .models import canonical, digest
from .store import Store

JOIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_telegram_join_authorizations(
 id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL UNIQUE,join_attempt_id TEXT NOT NULL UNIQUE,
 request_hash TEXT NOT NULL,claim TEXT NOT NULL,context TEXT NOT NULL,device_id TEXT NOT NULL,
 expires_at REAL NOT NULL,used_at REAL,native_id TEXT,execution_id TEXT
);
CREATE TABLE IF NOT EXISTS phone_telegram_observer_receipts(
 authorization_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,baseline TEXT NOT NULL,
 observer_sha256 TEXT NOT NULL,receipt TEXT,verified_at REAL
);
CREATE TABLE IF NOT EXISTS phone_telegram_receipt_budgets(
 authorization_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,observer_sha256 TEXT NOT NULL,
 read_identity TEXT NOT NULL,window_end REAL NOT NULL,reserved INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0
);
"""


class PhoneTelegram:
    def inspect_telegram_target(self, account_id, target_url):
        from phone_fleet_protocol.social import target_parts

        account, binding = self.binding(account_id)
        if (binding.platform != "telegram" or binding.receipt_reader != "telegram_api"
                or not self.telegram_observer or account["disabled"] or account["cooldown_until"] > time.time()):
            raise Conflict("PhoneTelegramObserverUnavailable")
        try:
            target_parts("telegram", target_url)
            return self.telegram_observer.inspect_target(account_id, binding.identity, target_url)
        except ValueError:
            raise Conflict("PhoneTelegramPublicGroupRequired") from None
        except BackendError as exc:
            if getattr(exc, "retry_after_seconds", 0):
                self.engine.apply_cooldown(account_id, {"rate_limited":True, "retry_after_seconds":exc.retry_after_seconds})
            raise Conflict(exc.safe_code) from None

    def observer_baseline(self,account_id,identity,target_url):
        if self.telegram_observer is None or account_id not in self.config.telegram_observers:
            raise BackendError("PhoneTelegramObserverNotConfigured")
        try:
            baseline = self.telegram_observer.baseline(account_id,identity,target_url)
        except BackendError as error:
            if getattr(error,"retry_after_seconds",0):
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE accounts SET cooldown_until=MAX(cooldown_until,?) WHERE id=?",(time.time()+error.retry_after_seconds,account_id))
            raise
        if not baseline.get("joined") or not baseline.get("can_send"):
            raise BackendError("PhoneTelegramMembershipOrWritePermissionRequired")
        return baseline

    def telegram_queue_blocker(self, account_id):
        """An external API observer must use the sender's authoritative account and queue."""
        from pathlib import Path

        binding = self.config.bindings.get(account_id)
        if not binding or binding.platform != "telegram" or binding.receipt_reader != "telegram_api":
            return None
        observer = self.config.telegram_observers.get(account_id)
        if (not observer or not observer.api_database or observer.api_account_id != account_id
                or not Path(observer.api_database).is_absolute()
                or Path(observer.api_database).resolve() != Path(self.store.path).resolve()):
            return "PhoneTelegramAuthoritativeQueueRequired"
        return None

    def observer_preflight(self,account,payload,execution_job):
        """The normal leased item may prepare after its claim; arbitrary preflight cannot."""
        from .queue_store import verify_job_claim

        if self.telegram_queue_blocker(account['id']) or not execution_job or not self.config.submit_enabled or not self.engine.live_enabled or not self.fresh(account):
            return False
        with self.store.connect() as db:
            job = db.execute("SELECT * FROM execution_jobs WHERE id=?",(execution_job.get("id"),)).fetchone()
            item = db.execute("SELECT * FROM items WHERE id=?",(job["reference_id"],)).fetchone() if job else None
            if not item or item["account_id"]!=account["id"] or item["status"]!="not_attempted" or json.loads(item["payload"])!=payload:
                return False
            task = db.execute("SELECT * FROM tasks WHERE id=?",(item["task_id"],)).fetchone()
            if (not task or task["state"]!="active" or task["phase"]!="ready" or not self.task_authorized(task["id"], account["id"], db=db)
                    or db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0]!="0"):
                return False
            try:
                verify_job_claim(db,{"platform":"telegram","account_id":account["id"],"operation":"execute",
                    "execution_job":execution_job,"payload":{"item_id":item["id"]}})
            except ValueError:
                return False
            return account["id"] in self.config.telegram_observers

    def prepare_telegram_claim(self,account,claim):
        """Called only after verify_claim, on the same frozen queued item, never as a second sender."""
        _,binding = self.binding(account["id"])
        baseline = self.observer_baseline(account["id"],binding.identity,claim["payload"]["target"])
        identifier = "phone-social-prepare:"+uuid4().hex
        request = self.make_request(account["id"],"social_prepare",{"target_url":claim["payload"]["target"],
            "target_id":baseline["peer_username"],"target_author":"@"+baseline["peer_username"],"target_text":baseline["peer_title"],
            "text":claim["payload"]["text"],"discard_after_prepare":True},task_id=claim["task_id"],item_id=claim["item_id"]+":"+identifier,
            manifest=self.engine.task(claim["task_id"])["manifest_hash"],channel="local")
        snapshot = self.dispatch(request,identifier,account["id"],claim["item_id"])
        proof = (snapshot.get("result") or {}).get("social") or {}
        from phone_fleet_protocol import fingerprint
        observed = snapshot.get("result") or {}
        if (snapshot.get("status")!="succeeded" or observed.get("simulated") is not False or not proof.get("discarded")
                or not proof.get("receipt_capable") or proof.get("client_id")!=binding.client_id
                or observed.get("observed_identity_sha256")!=fingerprint({"account":binding.identity})):
            raise BackendError("PhoneTelegramNativePreparationRequired")
        return self.preparation(account["id"],claim["payload"],item_id=claim["item_id"],task_id=claim["task_id"])

    def consume_receipt_budget(self,authorization_id,account_id,observer_sha256,reads,count):
        """Spend prepaid reads durably before starting the corresponding RPC batch."""
        from .telegram_receipt_budget import validate_receipt_reads

        reservation = self.store.one("SELECT * FROM phone_telegram_receipt_budgets WHERE authorization_id=?",(authorization_id,))
        if not reservation or reservation["account_id"]!=account_id or reservation["observer_sha256"]!=observer_sha256:
            raise BackendError("PhoneTelegramReceiptBudgetBindingChanged")
        if type(count) is not int or count<1:
            raise BackendError("PhoneTelegramReceiptBudgetUnavailable")
        validate_receipt_reads(reads,reservation)
        with self.store.connect(write=True) as db:
            changed = db.execute("UPDATE phone_telegram_receipt_budgets SET used=used+? WHERE authorization_id=? AND account_id=? AND observer_sha256=? AND window_end>? AND used+?<=reserved AND EXISTS (SELECT 1 FROM phone_social_authorizations WHERE id=? AND used_at IS NOT NULL)",
                (count,authorization_id,account_id,observer_sha256,time.time(),count,authorization_id)).rowcount
            if changed!=1:
                raise BackendError("PhoneTelegramReceiptBudgetUnavailable")

    def telegram_receipt(self,body,bearer):
        import hashlib
        import hmac

        from phone_fleet_protocol import fingerprint
        from phone_fleet_protocol.social import SocialInteraction, receipt_matches

        if not hmac.compare_digest(bearer,self.config.runtime_token.get_secret_value()):
            raise Conflict("PhoneSocialRuntimeAuthenticationRequired")
        row = self.store.one("SELECT * FROM phone_social_authorizations WHERE id=?",(body["authorization_id"],))
        stored = self.store.one("SELECT * FROM phone_telegram_observer_receipts WHERE authorization_id=?",(body["authorization_id"],))
        if (not row or not stored or row["used_at"] is None or row["request_hash"]!=body["request_sha256"]
                or row["native_id"]!=body["native_command_id"] or row["execution_id"]!=body["execution_id"]
                or row["device_id"]!=body["device_id"] or time.time()-row["used_at"]>600):
            raise Conflict("PhoneTelegramReceiptCorrelationMismatch")
        context = SocialInteraction.model_validate_json(row["context"])
        if context.platform!="telegram" or context.receipt_reader!="telegram_api":
            raise Conflict("PhoneTelegramProtocolReceiptContextRequired")
        if fingerprint(self.config.telegram_observers.get(stored["account_id"]))!=stored["observer_sha256"]:
            raise Conflict("PhoneTelegramObserverBindingChanged")
        receipt = json.loads(stored["receipt"]) if stored["receipt"] else None
        if receipt is None:
            reservation = self.store.one("SELECT * FROM phone_telegram_receipt_budgets WHERE authorization_id=?",(row["id"],))
            if reservation:
                def consume(reads,count):
                    self.consume_receipt_budget(row["id"],stored["account_id"],stored["observer_sha256"],reads,count)
                observed = self.telegram_observer.receipt(stored["account_id"],context,json.loads(stored["baseline"]),float(int(row["used_at"])),consume=consume)
            else:
                # Historical, already-consumed authorizations remain read-only recoverable.
                observed = self.telegram_observer.receipt(stored["account_id"],context,json.loads(stored["baseline"]),float(int(row["used_at"])))
            if observed:
                if (observed.source!="telegram_api" or not receipt_matches(observed,context,float(int(row["used_at"])))
                        or int(observed.native_id)<=json.loads(stored["baseline"])["last_message_id"]):
                    raise Conflict("PhoneTelegramObserverReceiptMismatch")
                receipt = observed.model_dump(mode="json")
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE phone_telegram_observer_receipts SET receipt=?,verified_at=? WHERE authorization_id=? AND receipt IS NULL",
                        (canonical(receipt),time.time(),row["id"]))
        result = {"request_sha256":row["request_hash"],"native_command_id":row["native_id"],
            "execution_id":row["execution_id"],"receipt":receipt}
        result["signature"] = hmac.new(self.config.runtime_token.get_secret_value().encode(),fingerprint(result).encode(),hashlib.sha256).hexdigest()
        return result

    def reconcile_protocol_receipt(self,request,snapshot):
        """Read the original authorization only; an API receipt never rewrites B's unknown result."""
        from phone_fleet_protocol.social import SocialReceipt, receipt_matches

        context=request.social
        raw=((snapshot.get("result") or {}).get("social") or {})
        if context.platform!="telegram" or context.receipt_reader!="telegram_api" or snapshot.get("status")!="unknown" or not raw.get("submission_started"):
            return None
        auth=self.store.one("SELECT * FROM phone_social_authorizations WHERE id=?",(context.authorization_id,))
        if not auth or auth["used_at"] is None or not auth["native_id"] or auth["execution_id"]!=snapshot.get("id"):
            return None
        value=self.telegram_receipt({"authorization_id":auth["id"],"request_sha256":auth["request_hash"],"native_command_id":auth["native_id"],
            "execution_id":auth["execution_id"],"device_id":auth["device_id"]},self.config.runtime_token.get_secret_value())
        receipt=SocialReceipt.model_validate(value["receipt"]) if value.get("receipt") else None
        if not receipt_matches(receipt,context,raw.get("submitted_at") or float(int(auth["used_at"]))):
            return None
        return {"status":"confirmed","verified":True,"external_reference":receipt.permalink,"native_id":receipt.native_id,
            "message_id":receipt.native_id,"published_at":receipt.created_at,"submission_started":True,
            "reconciliation_source":"telegram_protocol","original_runtime_status":"unknown",
            "device_reconciliation_required":not snapshot.get("resource_released",False)}

    def membership(self,account_id,target_url,target_title,*,task_id=None):
        from phone_fleet_protocol import ExecutionRequest, fingerprint
        from phone_fleet_protocol.social import TelegramMembership, target_parts

        account,binding = self.binding(account_id)
        if binding.platform != "telegram":
            raise Conflict("PhoneTelegramAccountRequired")
        self.ensure_identity(account_id,task_id=task_id)
        context = TelegramMembership(identity=binding.identity,client_id=binding.client_id,target_url=target_url,
            target_id=target_parts("telegram",target_url)[0],target_title=target_title)
        now = time.time()
        request = ExecutionRequest(business_task_id=task_id or "phone-membership:"+uuid4().hex,task_item_id="membership:"+uuid4().hex,
            manifest_sha256=fingerprint(context),account_id=account_id,device_id=binding.device_id,operation="social_membership",
            execution_channel="local",node_os=binding.node_os,not_before=now,deadline=now+115,
            expected_identity_sha256=fingerprint({"account":binding.identity}),social=context)
        return self.dispatch(request,"phone-telegram-membership:"+uuid4().hex,account_id)

    @staticmethod
    def membership_outcome(snapshot,context):
        from phone_fleet_protocol.social import SocialResult, membership_matches

        result = snapshot.get("result") or {}
        try:
            proof = SocialResult.model_validate(result.get("social"))
        except (ValueError,TypeError):
            proof = None
        if (snapshot.get("status") in {"succeeded","waiting_for_human"} and result.get("simulated") is False
                and proof and membership_matches(proof.membership,context) and proof.identity==context.identity
                and proof.client_id==context.client_id and (not proof.join_started or result.get("external_effect")=="confirmed")):
            state = proof.membership.state
            return {"status":"joined" if state=="joined" and proof.join_started else "already_joined" if state=="joined" else "pending_admin_approval" if state=="pending_admin_approval" else "not_joined",
                "verified":True,"membership_verified":state=="joined","external_reference":proof.membership.native_link,
                "source_execution_id":snapshot["id"],"submission_started":bool(proof.join_started)}
        if snapshot.get("status")=="failed" and result.get("external_effect")=="none" and result.get("quiescent") is True:
            return {"status":"failed_known","verified":False,"confirmed_not_submitted":True,"reason":result.get("code")}
        return {"status":"unknown","verified":False,"reason":"PhoneTelegramMembershipReceiptRequired"}

    def join(self,account,payload,execution_job):
        from phone_fleet_protocol import ExecutionRequest, fingerprint
        from phone_fleet_protocol.social import TelegramMembership

        if not self.config.join_enabled or not self.engine.live_enabled:
            return {"status":"failed_known","verified":False,"confirmed_not_submitted":True,"reason":"PhoneTelegramJoinDisabled"}
        full = {"platform":"telegram","account":account["backend_label"],"account_id":account["id"],
            "database":self.store.path,"operation":"join","execution_job":execution_job,"payload":payload}
        verify_claim(full)
        candidate = self.store.one("SELECT * FROM candidates WHERE id=?",(payload.get("candidate_id"),))
        attempt = self.store.one("SELECT * FROM join_attempts WHERE candidate_id=?",(payload.get("candidate_id"),))
        if not candidate or not attempt or attempt["outcome"]!="claimed" or attempt["account_id"]!=account["id"]:
            raise BackendError("PhoneTelegramOriginalJoinClaimRequired")
        peer_title = json.loads(candidate["payload"]).get("title","")
        if not peer_title.strip():
            return {"status":"failed_known","verified":False,"confirmed_not_submitted":True,"reason":"PhoneTelegramPeerTitleRequired"}
        if candidate["task_id"] not in self.config.authorized_task_ids:
            raise BackendError("PhoneSocialTaskOutsideDevelopmentScope")
        old = self.store.one("SELECT * FROM phone_telegram_join_authorizations WHERE join_attempt_id=?",(attempt["id"],))
        if old:
            return self.verify_join(account,payload)
        snapshot = self.membership(account["id"],candidate["target"],peer_title,task_id=candidate["task_id"])
        if not snapshot.get("request"):
            return {"status":"unknown","verified":False,"reason":"PhoneTelegramMembershipRequestUnsettled"}
        context = TelegramMembership.model_validate(snapshot.get("request",{}).get("social"))
        before = self.membership_outcome(snapshot,context)
        if before["status"] != "not_joined":
            return before
        adapter = (snapshot.get("result") or {}).get("social") or {}
        if not adapter.get("adapter_sha256"):
            raise BackendError("PhoneTelegramCommissionedJoinRulesRequired")
        _,binding = self.binding(account["id"])
        identifier,now = "phone-telegram-join:"+uuid4().hex,time.time()
        context = context.model_copy(update={"authorization_id":identifier,"expected_adapter_sha256":adapter["adapter_sha256"]})
        task = self.store.one("SELECT * FROM tasks WHERE id=?",(candidate["task_id"],))
        request = ExecutionRequest(business_task_id=task["id"],task_item_id="join:"+attempt["id"],manifest_sha256=task["manifest_hash"] or digest(json.loads(task["policy"])),
            account_id=account["id"],device_id=binding.device_id,operation="social_join",execution_channel=json.loads(task["policy"])["execution_channel"],
            node_os=binding.node_os,not_before=now,deadline=now+115,expected_identity_sha256=fingerprint({"account":binding.identity}),social=context)
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO phone_telegram_join_authorizations(id,candidate_id,join_attempt_id,request_hash,claim,context,device_id,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (identifier,candidate["id"],attempt["id"],fingerprint(request),canonical(full),canonical(context.model_dump(mode="json")),binding.device_id,
                    min(request.deadline,(execution_job or {}).get("lease_until",request.deadline))))
        return self.membership_outcome(self.dispatch(request,identifier,account["id"]),context)

    def authorize_join(self,body):
        if not self.config.join_enabled or not self.engine.live_enabled:
            raise Conflict("PhoneTelegramJoinDisabled")
        row = self.store.one("SELECT * FROM phone_telegram_join_authorizations WHERE id=?",(body["authorization_id"],))
        if (not row or row["used_at"] is not None or row["expires_at"]<=time.time()
                or row["request_hash"]!=body["request_sha256"] or row["device_id"]!=body["device_id"]):
            raise Conflict("PhoneTelegramJoinPermissionFenced")
        operation = self.store.one("SELECT execution_id FROM phone_social_operations WHERE id=?",(row["id"],))
        if not operation or operation["execution_id"]!=body["execution_id"]:
            raise Conflict("PhoneTelegramJoinExecutionMismatch")
        full = json.loads(row["claim"])
        verify_claim(full)
        candidate = self.store.one("SELECT * FROM candidates WHERE id=?",(row["candidate_id"],))
        if not candidate or candidate["task_id"] not in self.config.authorized_task_ids:
            raise Conflict("PhoneSocialTaskOutsideDevelopmentScope")
        with self.store.connect(write=True) as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?",(candidate["task_id"],)).fetchone()
            active = db.execute("SELECT * FROM candidates WHERE id=?",(row["candidate_id"],)).fetchone()
            attempt = db.execute("SELECT * FROM join_attempts WHERE id=?",(row["join_attempt_id"],)).fetchone()
            if (not task or task["state"]!="active" or not active or active["state"]!="executing"
                    or not attempt or attempt["outcome"]!="claimed" or db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0]!="0"):
                raise Conflict("PhoneTelegramJoinStopped")
            self.engine._integrity(db,task,"discovery_join")
            changed = db.execute("UPDATE phone_telegram_join_authorizations SET used_at=?,native_id=?,execution_id=? WHERE id=? AND used_at IS NULL AND expires_at>?",
                (time.time(),body["native_command_id"],body["execution_id"],row["id"],time.time())).rowcount
            if changed != 1:
                raise Conflict("PhoneTelegramJoinPermissionAlreadyUsed")
            Store.audit(db,"phone_telegram.join_authorized",{"candidate_id":row["candidate_id"],"join_attempt_id":row["join_attempt_id"],
                "native_command_id":body["native_command_id"]},candidate["task_id"],time.time())
        return {"allowed":True,"submit_once":True,"request_sha256":row["request_hash"],"native_command_id":body["native_command_id"],"expires_at":min(time.time()+2,row["expires_at"])}

    def verify_join(self,account,payload):
        from phone_fleet_protocol import ExecutionRequest

        row = self.store.one("SELECT * FROM phone_telegram_join_authorizations WHERE candidate_id=?",(payload.get("candidate_id"),))
        if not row:
            return {"status":"unknown","verified":False,"reason":"PhoneTelegramNoOriginalJoin"}
        request = self.store.one("SELECT * FROM phone_social_operations WHERE id=?",(row["id"],))
        if not request or request["account_id"]!=account["id"]:
            raise Conflict("PhoneTelegramJoinAccountMismatch")
        from .phone_protocol import is_legacy
        if is_legacy(json.loads(request["request"])):
            return self.read_legacy_operation(request)
        typed = ExecutionRequest.model_validate_json(request["request"])
        return self.membership_outcome(self.dispatch(typed,request["id"],account["id"]),typed.social)
