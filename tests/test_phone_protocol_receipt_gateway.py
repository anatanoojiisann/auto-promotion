"""The same claimed phone item gains a read-only protocol witness, never an API sender."""
import time

import httpx
import pytest
from phone_fleet_protocol import fingerprint
from phone_fleet_protocol.social import SocialReceipt

from auto_promotion.telegram_phone_observer import TelegramObserverConfig
from tests.test_phone_social import claim_original
from tests.test_phone_social import pilot as pilot


@pytest.mark.parametrize("pilot",["telegram"],indirect=True)
@pytest.mark.parametrize("primary_api", [False, True])
def test_original_phone_claim_protocol_receipt_is_cached_and_readable_after_stop(pilot, primary_api):
    from auto_promotion.backends.common import read_profile, save_profile
    from auto_promotion.models import AccountInput, ItemInput, TaskInput
    from auto_promotion.routing import route_for

    s=pilot
    binding=s.service.config.bindings[s.account["id"]].model_copy(update={"receipt_reader":"telegram_api"})
    if primary_api:
        s.account=s.engine.add_account(AccountInput(platform="telegram", label="Canonical account",
                                      backend_label="synthetic-canonical",mode="live"))
        stored={"identity":"44","api_id":1,"api_hash":"test-hash","sentinel":"preserve"}
        save_profile("telegram", s.account["backend_label"],stored)
        stored = read_profile("telegram", s.account["backend_label"])
        def primary_call(account, operation, payload):
            assert operation == "health", "Phone task fell back to the primary API sender"
            return {"healthy":True,"service_ready":True,"identity_verified":True,
                    "account_identity":"44","permissions":["send_message"]}
        s.monkeypatch.setattr(s.engine.adapters,"call",primary_call)
        s.engine.check_account(s.account["id"],force=True)
    observer=TelegramObserverConfig(backend_label=s.account["backend_label"] if primary_api else "synthetic-read-only",user_id=44,
                                    api_account_id=s.account["id"],api_database=str(s.store.path))
    s.service.config=s.service.config.model_copy(update={"bindings":{s.account["id"]:binding},"telegram_observers":{s.account["id"]:observer}})
    calls=[]
    class Reader:
        def baseline(self,*args):
            calls.append("baseline")
            return {"api_user_id":"44","peer_id":"-1000000000123","peer_username":"test_group","peer_title":"Test group",
                "identity_sha256":fingerprint({"account":binding.identity}),"joined":True,"can_send":True,"sends_as_self":True,"last_message_id":10,"observed_at":time.time()}
        def reserve_receipt(self,account_id):
            calls.append("reserve")
            return {"read_identity":"synthetic-identity","window_end":time.time()+3600,"reserved":16}
        def check_receipt_reservation(self,account_id,reservation,*,minimum_seconds=0):
            calls.append("check-reservation")
            assert reservation["read_identity"]=="synthetic-identity" and reservation["window_end"]-time.time()>=minimum_seconds
        def receipt(self,account_id,c,baseline,since,*,consume=None):
            assert consume is not None
            calls.append("receipt")
            return SocialReceipt(platform="telegram",source="telegram_api",action="send_message",native_id="98765",target_id=c.target_id,
                author=c.identity,text_sha256=fingerprint({"text":c.text}),permalink="https://t.me/test_group/98765",created_at=time.time(),observed_at=time.time())
    s.service.telegram_observer=Reader()
    def fleet(request):
        response=s.fake(request)
        value=response.json()
        if request.method=="GET" and (value.get("result") or {}).get("social",{}).get("receipt"):
            value["result"]["social"]["receipt"]["source"]="telegram_api"
            s.fake.snapshots[value["id"]]=value
        return httpx.Response(response.status_code,json=value)
    s.service.transport=httpx.MockTransport(fleet)
    if primary_api:
        s.service.register(s.account["id"])
        assert read_profile("telegram",s.account["backend_label"])==stored
        assert route_for(s.account).transport=="mtproto"
        policy={k:v for k,v in s.task["policy"].items() if k not in {"quota_snapshot"}}
        original=s.engine.create_task(TaskInput(name="Canonical native task",account_ids=[s.account["id"]],policy=policy))
        s.engine.set_items(original["id"],[ItemInput(account_id=s.account["id"],action="send_message",
                           target="https://t.me/test_group",text="Specific shared-account question.",scheduled_at=s.item["payload"]["scheduled_at"])])
        s.task=s.engine.task(original["id"])
        s.item=s.task["items"][0]
    claim,context=claim_original(s)
    s.fake.authorize=lambda body:s.service.authorize(body,s.config["runtime_token"])
    if primary_api:
        import json
        job=s.store.one("SELECT account_id,pool,route FROM execution_jobs WHERE reference_id=?", (s.item["id"],))
        assert job["account_id"]==s.account["id"] and job["pool"]=="telegram.phone"
        assert json.loads(job["route"])["resource"].startswith("phone:")
        with s.engine.adapters.execution_job(context):
            outcome=s.engine.call(s.account, "execute", claim, s.task["id"])
        assert outcome.get("verified"), outcome
        assert read_profile("telegram",s.account["backend_label"])==stored
    else:
        assert s.service.execute(s.engine.account(s.account["id"]),claim,context)["verified"]
    auth=s.store.one("SELECT * FROM phone_social_authorizations")
    body={"authorization_id":auth["id"],"request_sha256":auth["request_hash"],"native_command_id":auth["native_id"],"execution_id":auth["execution_id"],"device_id":auth["device_id"]}
    first=s.service.telegram_receipt(body,s.config["runtime_token"])
    s.engine.set_stop(True)
    second=s.service.telegram_receipt(body,s.config["runtime_token"])
    assert first["receipt"]==second["receipt"] and calls.count("receipt")==1
    assert first["receipt"]["native_id"]=="98765" and first["signature"]
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"]==1


@pytest.mark.parametrize("pilot",["telegram"],indirect=True)
def test_exhausted_shared_receipt_budget_blocks_before_submit_dispatch(pilot):
    from auto_promotion.backends.common import BackendError

    s=pilot
    binding=s.service.config.bindings[s.account["id"]].model_copy(update={"receipt_reader":"telegram_api"})
    observer=TelegramObserverConfig(backend_label="synthetic-read-only",user_id=44,
                                    api_account_id=s.account["id"],api_database=str(s.store.path))
    s.service.config=s.service.config.model_copy(update={"bindings":{s.account["id"]:binding},"telegram_observers":{s.account["id"]:observer}})
    class Reader:
        def baseline(self,*args):
            return {"api_user_id":"44","peer_id":"-1000000000123","peer_username":"test_group","peer_title":"Test group",
                "identity_sha256":fingerprint({"account":binding.identity}),"joined":True,"can_send":True,"sends_as_self":True,"last_message_id":10,"observed_at":time.time()}
        def reserve_receipt(self,*args):
            raise BackendError("TelegramReadAccountBudgetExhausted")
    s.service.telegram_observer=Reader()
    claim,context=claim_original(s)
    before=len(s.fake.requests)
    result=s.service.execute(s.engine.account(s.account["id"]),claim,context)
    assert result["confirmed_not_submitted"] and result["submission_started"] is False
    assert result["reason"]=="TelegramReadAccountBudgetExhausted"
    assert len(s.fake.requests)==before
    assert s.store.one("SELECT COUNT(*) n FROM phone_social_authorizations")["n"]==0


@pytest.mark.parametrize("pilot",["telegram"],indirect=True)
def test_missing_receipt_reservation_fences_final_click(pilot):
    from auto_promotion.core import Conflict

    s=pilot
    binding=s.service.config.bindings[s.account["id"]].model_copy(update={"receipt_reader":"telegram_api"})
    observer=TelegramObserverConfig(backend_label="synthetic-read-only",user_id=44,
                                    api_account_id=s.account["id"],api_database=str(s.store.path))
    s.service.config=s.service.config.model_copy(update={"bindings":{s.account["id"]:binding},"telegram_observers":{s.account["id"]:observer}})
    class Reader:
        def baseline(self,*args):
            return {"api_user_id":"44","peer_id":"-1000000000123","peer_username":"test_group","peer_title":"Test group",
                "identity_sha256":fingerprint({"account":binding.identity}),"joined":True,"can_send":True,"sends_as_self":True,"last_message_id":10,"observed_at":time.time()}
        def reserve_receipt(self,*args):
            return {"read_identity":"synthetic-identity","window_end":time.time()+3600,"reserved":16}
        def check_receipt_reservation(self,*args,**kwargs):
            pytest.fail("missing reservation must be rejected first")
    s.service.telegram_observer=Reader()
    claim,context=claim_original(s)
    def revoke_then_authorize(body):
        with s.store.connect(write=True) as db:
            db.execute("DELETE FROM phone_telegram_receipt_budgets WHERE authorization_id=?",(body["authorization_id"],))
        return s.service.authorize(body,s.config["runtime_token"])
    s.fake.authorize=revoke_then_authorize
    with pytest.raises(Conflict,match="ReceiptBudgetRequired"):
        s.service.execute(s.engine.account(s.account["id"]),claim,context)
    auth=s.store.one("SELECT used_at FROM phone_social_authorizations")
    assert auth and auth["used_at"] is None

@pytest.mark.parametrize("pilot",["telegram"],indirect=True)
@pytest.mark.parametrize("mismatch",["database","account","undeclared"])
def test_separate_api_queue_cannot_grant_a_phone_send(pilot, tmp_path, mismatch):
    from auto_promotion.backends.common import BackendError
    s=pilot
    acc=s.account["id"]
    binding=s.service.config.bindings[acc].model_copy(update={"receipt_reader":"telegram_api"})
    observer=TelegramObserverConfig(backend_label="synthetic-read-only",user_id=44,
        api_account_id=("different-account" if mismatch=="account" else acc),
        api_database=None if mismatch=="undeclared" else str(tmp_path/"another.db" if mismatch=="database" else s.store.path))
    s.service.config=s.service.config.model_copy(update={"submit_enabled":True,"bindings":{acc:binding},"telegram_observers":{acc:observer}})
    result=s.service.backend(s.account,"preflight",{})
    assert result["allowed"] is False and result["reason"]=="PhoneTelegramAuthoritativeQueueRequired"
    before=len(s.fake.requests)
    with pytest.raises(BackendError,match="AuthoritativeQueueRequired"):
        s.service.execute(s.account,{},None)
    assert len(s.fake.requests)==before and not s.store.rows("SELECT * FROM phone_social_authorizations")
