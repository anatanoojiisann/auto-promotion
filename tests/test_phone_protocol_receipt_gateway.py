"""The same claimed phone item gains a read-only protocol witness, never an API sender."""
import time

import httpx
import pytest

from auto_promotion.telegram_phone_observer import TelegramObserverConfig
from phone_fleet_protocol import fingerprint
from phone_fleet_protocol.social import SocialReceipt
from tests.test_phone_social import claim_original
from tests.test_phone_social import pilot as pilot


@pytest.mark.parametrize("pilot",["telegram"],indirect=True)
def test_original_phone_claim_protocol_receipt_is_cached_and_readable_after_stop(pilot):
    s=pilot
    binding=s.service.config.bindings[s.account["id"]].model_copy(update={"receipt_reader":"telegram_api"})
    observer=TelegramObserverConfig(backend_label="synthetic-read-only",user_id=44)
    s.service.config=s.service.config.model_copy(update={"bindings":{s.account["id"]:binding},"telegram_observers":{s.account["id"]:observer}})
    calls=[]
    class Reader:
        def baseline(self,*args):
            calls.append("baseline")
            return {"api_user_id":"44","peer_id":"-1000000000123","peer_username":"test_group","peer_title":"Test group",
                "identity_sha256":fingerprint({"account":binding.identity}),"joined":True,"can_send":True,"last_message_id":10,"observed_at":time.time()}
        def receipt(self,account_id,c,baseline,since):
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
    claim,context=claim_original(s)
    s.fake.authorize=lambda body:s.service.authorize(body,s.config["runtime_token"])
    assert s.service.execute(s.engine.account(s.account["id"]),claim,context)["verified"]
    auth=s.store.one("SELECT * FROM phone_social_authorizations")
    body={"authorization_id":auth["id"],"request_sha256":auth["request_hash"],"native_command_id":auth["native_id"],"execution_id":auth["execution_id"],"device_id":auth["device_id"]}
    first=s.service.telegram_receipt(body,s.config["runtime_token"])
    s.engine.set_stop(True)
    second=s.service.telegram_receipt(body,s.config["runtime_token"])
    assert first["receipt"]==second["receipt"] and calls.count("receipt")==1
    assert first["receipt"]["native_id"]=="98765" and first["signature"]
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"]==1
