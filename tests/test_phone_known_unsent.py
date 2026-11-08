"""Audited no-submit recovery retains source records and never permits an unknown send."""

import json

import pytest

from auto_promotion.core import Conflict
from auto_promotion.models import ItemInput, TaskInput
from auto_promotion.phone_known_unsent import authorized, register
from auto_promotion.queue_store import JobQueue
from tests.test_phone_social import claim_original
from tests.test_phone_social import pilot as pilot


def replacement(s):
    claim,_ = claim_original(s)
    s.engine.finish(claim,{"status":"failed_known","verified":False,"confirmed_not_submitted":True,"submission_started":False})
    queue=JobQueue(s.engine)
    job=s.store.one("SELECT * FROM execution_jobs WHERE reference_id=?",(s.item["id"],))
    queue.settle(job)
    policy={k:v for k,v in s.engine.task(s.task["id"])["policy"].items() if k!="quota_snapshot"}
    task=s.engine.create_task(TaskInput(name="Explicit replacement",account_ids=[s.account["id"]],policy=policy))
    payload={k:v for k,v in s.item["payload"].items() if k not in {"platform","position","account_label"}}
    s.engine.set_items(task["id"],[ItemInput(**payload)])
    return s.engine.task(task["id"])


@pytest.mark.parametrize("pilot",["telegram"],indirect=True)
def test_exact_proven_unsent_recovery_keeps_old_result_and_usage(pilot):
    s=pilot
    task=replacement(s)
    item=task["items"][0]
    before=s.store.one("SELECT * FROM items WHERE id=?",(s.item["id"],))
    usage=s.store.one("SELECT count(*) n FROM activity")["n"]
    proof=register(s.engine,s.item["id"],item["id"],"Explicit synthetic instruction to resume proven unsent text")
    assert proof["authorized"]
    with s.store.connect() as db:
        raw=db.execute("SELECT * FROM tasks WHERE id=?",(task["id"],)).fetchone()
        assert authorized(db,raw,item["payload"],s.item["id"])
    assert before==s.store.one("SELECT * FROM items WHERE id=?",(s.item["id"],))
    assert usage==s.store.one("SELECT count(*) n FROM activity")["n"]


@pytest.mark.parametrize("pilot",["telegram"],indirect=True)
@pytest.mark.parametrize("uncertain",[True,False])
def test_unknown_or_changed_text_cannot_borrow_no_submit_recovery(pilot,uncertain):
    s=pilot
    task=replacement(s)
    if uncertain:
        with s.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown',result=? WHERE id=?",
                       (json.dumps({"submission_started":True}),s.item["id"]))
    else:
        item=task["items"][0]
        with s.store.connect(write=True) as db:
            payload=json.loads(db.execute("SELECT payload FROM items WHERE id=?",(item["id"],)).fetchone()[0])
            payload["text"]+=" changed"
            db.execute("UPDATE items SET payload=? WHERE id=?",(json.dumps(payload),item["id"]))
    with pytest.raises(Conflict):
        register(s.engine,s.item["id"],task["items"][0]["id"],"Synthetic instruction cannot override evidence")
