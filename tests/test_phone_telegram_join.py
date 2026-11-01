"""Original AP join job and attempt, with synthetic B/native membership receipts."""
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from auto_promotion.backends.common import BackendError
from auto_promotion.core import Conflict
from auto_promotion.models import CandidateInput, TaskInput
from auto_promotion.queue_store import JobQueue, job_scope
from auto_promotion.task_channels import TaskChannelRouter
from tests.test_phone_social import pilot as pilot


@pytest.mark.parametrize("pilot",["telegram"],indirect=True)
@pytest.mark.parametrize("pending",[False,True])
def test_original_join_lane_uses_one_permission_and_records_membership(pilot,pending):
    s=pilot
    now=time.time()
    def iso(n):
        return datetime.fromtimestamp(n,timezone.utc).isoformat()
    task=s.engine.create_task(TaskInput(name="Synthetic native join",account_ids=[s.account["id"]],policy={
        "execution_channel":"local","execution_method":"phone","starts_at":iso(now-5),"ends_at":iso(now+600),"max_joins":1}))
    candidate=s.engine.candidate(task["id"],CandidateInput(account_id=s.account["id"],target="https://t.me/test_group",title="Test group",
        text="Synthetic public test group",language="en",members=1000,activity=20,confidence=1,is_public=True))
    assert candidate["decision"]=="qualified"
    candidate_id=s.store.one("SELECT id FROM candidates WHERE task_id=?",(task["id"],))["id"]
    s.service.config=s.service.config.model_copy(update={"join_enabled":True,"authorized_task_ids":[task["id"]]})
    def fleet(request):
        if request.method=="POST":
            body=json.loads(request.content)
            if body["operation"] not in {"social_membership","social_join"}:
                return s.fake(request)
            identifier="join-exec-"+str(len(s.fake.snapshots))
            social=body["social"]
            member={"peer_username":"test_group","native_link":"https://t.me/test_group","state":"not_joined",
                "peer_observation_sha256":"b"*64,"state_observation_sha256":"c"*64,"observed_at":time.time()}
            result={"execution_id":identifier,"command_id":"native-command","device_id":"device-test","status":"succeeded",
                "simulated":False,"quiescent":True,"external_effect":"none","observed_identity_sha256":body["expected_identity_sha256"],
                "social":{"platform":"telegram","identity":social["identity"],"target_id":"test_group","adapter_id":"synthetic-client",
                    "adapter_sha256":"a"*64,"membership":member,"join_started":False}}
            row={"id":identifier,"request":body,"status":"succeeded","desired":"run","revision":1,"created_at":now,"updated_at":now,
                "blocked_reason":None,"command_id":"native-command","device_id":"device-test","node_id":"synthetic-node","external_effect":"none","result":result}
            if body["operation"]=="social_join":
                row.update(status="running",result=None)
            s.fake.snapshots[identifier]=row
            s.fake.requests.append(request)
            return httpx.Response(200,json=row)
        row=s.fake.snapshots[request.url.path.rsplit("/",1)[1]]
        if row["status"]=="running":
            social=row["request"]["social"]
            from phone_fleet_protocol import fingerprint
            permission={"authorization_id":social["authorization_id"],"request_sha256":fingerprint(row["request"]),
                "native_command_id":"one-native-join","execution_id":row["id"],"device_id":"device-test"}
            s.engine.set_stop(True)
            with pytest.raises((Conflict,BackendError),match="[Ss]top"):
                s.service.authorize(permission,s.config["runtime_token"])
            assert s.store.one("SELECT COUNT(*) n FROM phone_telegram_join_authorizations WHERE used_at IS NOT NULL")["n"]==0
            s.engine.set_stop(False)
            s.service.authorize(permission,s.config["runtime_token"])
            with pytest.raises(Conflict,match="Fenced"):
                s.service.authorize({**permission,"native_command_id":"second-join"},s.config["runtime_token"])
            member={"peer_username":"test_group","native_link":"https://t.me/test_group","state":"pending_admin_approval" if pending else "joined",
                "peer_observation_sha256":"b"*64,"state_observation_sha256":"c"*64,"observed_at":time.time()}
            row.update(status="waiting_for_human" if pending else "succeeded",external_effect="confirmed",result={
                "execution_id":row["id"],"command_id":"native-command","device_id":"device-test","status":"waiting_for_human" if pending else "succeeded",
                "simulated":False,"quiescent":True,"external_effect":"confirmed","observed_identity_sha256":row["request"]["expected_identity_sha256"],
                "social":{"platform":"telegram","identity":social["identity"],"target_id":"test_group","adapter_id":"synthetic-client","adapter_sha256":"a"*64,
                    "membership":member,"join_started":True,"join_submitted_at":now}})
        s.fake.requests.append(request)
        return httpx.Response(200,json=row)
    s.service.transport=httpx.MockTransport(fleet)
    s.engine.start_task(task["id"],source="synthetic_explicit_join",sending=False)
    queue=JobQueue(s.engine)
    queue.sync(task["id"])
    job=queue.claim("telegram.phone","synthetic-join-worker",lease_seconds=180,reference_id=candidate_id)
    assert job
    context,_=TaskChannelRouter(s.engine).prepare(job)
    with job_scope(job),s.engine.adapters.execution_job(context):
        result=s.engine._run_joins(task["id"],s.account["id"],candidate_id)
    state=s.store.one("SELECT state FROM candidates WHERE id=?",(candidate_id,))["state"]
    assert state==("pending_admin_approval" if pending else "joined"),result
    assert s.store.one("SELECT COUNT(*) n FROM join_attempts")["n"]==1
    assert s.store.one("SELECT COUNT(*) n FROM phone_telegram_join_authorizations WHERE used_at IS NOT NULL")["n"]==1
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"]==0
    before=len([r for r in s.fake.requests if r.method=="POST"])
    assert s.service.verify_join(s.engine.account(s.account["id"]),{"candidate_id":candidate_id})["verified"]
    assert len([r for r in s.fake.requests if r.method=="POST"])==before
