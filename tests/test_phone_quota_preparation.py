"""Synthetic pre-submission proof and quota counters, without device actions."""
import json

import pytest

from auto_promotion.models import canonical, digest
from auto_promotion.queue_store import JobQueue
from auto_promotion.quotas import check_claim
from tests.test_phone_rounds import create, finish, make_lab


@pytest.mark.parametrize("uncertain", [False, True])
def test_proven_preparation_failure_keeps_account_usage_and_does_not_contact_user(tmp_path, uncertain):
    lab = make_lab(tmp_path)
    rid = create(lab)["id"]
    lab.rounds.start(rid)
    for _ in lab.ids:
        lab.rounds.tick()
    JobQueue(lab.engine).sync("task-telegram")
    finish(lab, "telegram", outcome="failed_known", verified=False)
    with lab.store.connect(write=True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS phone_social_authorizations(item_id TEXT)")
        result = {"status": "failed_known", "verified": False,
                  "confirmed_not_submitted": not uncertain, "submission_started": uncertain}
        db.execute("UPDATE items SET result=?,external_reference=NULL WHERE id='item-telegram'", (canonical(result),))
        db.execute("UPDATE execution_jobs SET state='failed_known',owner=NULL,lease_until=0 WHERE reference_id='item-telegram'")
        account = dict(db.execute("SELECT * FROM accounts WHERE platform='telegram'").fetchone())
        item = db.execute("SELECT * FROM items WHERE id='item-telegram'").fetchone()
        payload = json.loads(item["payload"])
        db.execute("UPDATE attempts SET target_key=?,user_key=? WHERE item_id='item-telegram'",
                   (digest(["telegram",payload["target"]]), digest(["telegram",payload["target"]])))
        task = {"id": "new-phone-task", "campaign_id": None,
                "policy": canonical({"execution_method":"phone","daily_limit":1,
                                     "target_daily_limit":1,"user_daily_limit":1})}
        detail = {}
        reason = check_claim(db, account, task, payload, lab.now[0], lab.now[0], "new-item", details=detail)
        assert reason == ("activity_limit" if uncertain else None)
        if uncertain:
            assert detail["retry_at"] > lab.now[0]
        account["daily_limit"] = 1
        assert check_claim(db, account, task, payload, lab.now[0], lab.now[0], "new-item") == "activity_limit"
        assert db.execute("SELECT count(*) FROM activity").fetchone()[0] == 1
