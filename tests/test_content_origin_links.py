"""Origin/recipient separation and version-safe result readback; synthetic data only."""

import json

import pytest

from auto_promotion.content_queue import ContentBatch, ContentQueue
from auto_promotion.interaction_simulation import source
from auto_promotion.models import ItemInput, digest
from auto_promotion.phone_social import validate_item
from auto_promotion.quotas import event, user_identifier


@pytest.fixture
def lab():
    from test_hf_content_queue import configure_queue
    from test_interactions import InteractionTests

    value = InteractionTests(methodName="runTest")
    value.setUp()
    configure_queue(value)
    value.round([source(12345, "P1", value.now)])
    value.s.run_stage("generate", max_jobs=100)
    yield value
    value.doCleanups()


def bind(lab, key="binding"):
    from test_content_queue import iso

    queue = ContentQueue(lab.engine)
    entry = queue.list()[-1]
    value = ContentBatch(
        idempotency_key=key,
        task={
            "name": "Synthetic origin readback",
            "account_ids": [lab.account["id"]],
            "policy": {
                "execution_method": "phone",
                "execution_channel": "local",
                "dispatch_policy": "phone_queue_v1",
                "starts_at": iso(lab.now - 10),
                "ends_at": iso(lab.now + 3600),
                "min_interval": 0,
            },
        },
        assignments=[
            {
                "content_id": entry["id"],
                "account_id": lab.account["id"],
                "scheduled_at": iso(lab.now),
            }
        ],
    )
    result = queue.materialize(value)
    return value, result, lab.engine.task(result["task_id"])["items"][0]


def fixture_result(lab, item, status, result=None, reference=None):
    with lab.store.connect(write=True) as db:
        db.execute(
            "UPDATE items SET status=?,result=?,external_reference=?,attempt_id='synthetic-attempt' WHERE id=?",
            (status, json.dumps(result or {}), reference, item["id"]),
        )
    lab.s.sync_results()


def test_empty_author_field_preserves_legacy_payload_and_hash():
    raw = dict(
        account_id="a",
        action="reply",
        target="https://x.com/source/status/12345",
        text="Old fixture",
        scheduled_at="2026-11-09T00:00:00Z",
    )
    value = ItemInput(**raw).model_dump()
    assert "source_author_id" not in value
    assert digest(ItemInput(**{**value, "source_author_id": ""}).model_dump()) == digest(value)
    enriched = ItemInput(**{**raw, "source_author_id": "author"}).model_dump()
    assert enriched["source_author_id"] == "author" and enriched["recipient_id"] == ""


def test_source_author_retains_the_old_user_quota_key():
    old = {"target": "https://x.com/source/status/12345", "recipient_id": "author"}
    new = {"target": old["target"], "recipient_id": "", "source_author_id": "author"}
    assert user_identifier(old) == user_identifier(new) == "author"
    account = {"id": "a", "platform": "x", "mode": "live"}
    task = {"id": "task"}
    assert event(account, task, old, 1)["user"] == event(account, task, new, 1)["user"]
    assert user_identifier({**new, "recipient_id": "actual-recipient"}) == "actual-recipient"


def test_hf_native_scope_accepts_author_metadata_but_still_rejects_a_recipient(lab):
    payload = ContentQueue(lab.engine).list()[0]["payload"]
    assert payload["source_author_id"] == "synthetic-user-12345" and payload["recipient_id"] == ""
    validate_item({"platform": "x"}, payload)
    with pytest.raises(Exception, match="PhoneSocialOnlyCommissionedTopLevelTextAction"):
        validate_item({"platform": "x"}, {**payload, "recipient_id": "real-recipient"})


def test_binding_reuses_original_task_and_candidate_without_changing_owner(lab):
    before = lab.candidate(12345)
    value, result, item = bind(lab)
    after = lab.candidate(12345)
    assert after["task_id"] == result["task_id"] and after["item_id"] == item["id"]
    assert after["owner_account_id"] == before["owner_account_id"]
    assert after["state"] == "READY" and after["block_reason"] == "content_queue_task_draft"
    assert ContentQueue(lab.engine).materialize(value)["items"] == [item["id"]]
    assert len(lab.store.rows("SELECT * FROM tasks")) == 1
    assert not lab.store.rows("SELECT * FROM attempts")


@pytest.mark.parametrize(
    "status,proof,reference,expected",
    [
        ("executing", {}, None, "PUBLISHING"),
        ("unknown", {"submission_started": True}, None, "VERIFYING"),
        ("cancelled", {"submission_started": True}, None, "VERIFYING"),
        ("failed_known", {"confirmed_not_submitted": True}, None, "CLOSED"),
        (
            "confirmed",
            {"verified": True, "native_id": "fixture"},
            "simulation://fixture",
            "SIMULATED",
        ),
    ],
)
def test_status_readback_never_enqueues_an_automatic_submission(
    lab, status, proof, reference, expected
):
    _, result, item = bind(lab)
    before_jobs = lab.store.rows("SELECT * FROM execution_jobs")
    fixture_result(lab, item, status, proof, reference)
    current = lab.candidate(12345)
    assert current["state"] == expected
    assert current["task_id"] == result["task_id"] and current["item_id"] == item["id"]
    assert current["version"] == 1
    assert lab.store.rows("SELECT * FROM execution_jobs") == before_jobs
    assert not lab.store.rows("SELECT * FROM hf_publish_retries")
    lab.now += 10
    lab.s.sync_results()
    assert lab.candidate(12345) == current


def test_new_candidate_version_keeps_old_execution_history(lab):
    _, result, item = bind(lab)
    candidate = lab.candidate(12345)
    # A source refresh is represented explicitly, not a resend or rewritten old task.
    with lab.store.connect(write=True) as db:
        db.execute(
            "UPDATE hf_candidates SET version=version+1,draft='New source version',state='READY',task_id=NULL,item_id=NULL WHERE id=?",
            (candidate["id"],),
        )
    fixture_result(
        lab,
        item,
        "confirmed",
        {"verified": True, "native_id": "fixture"},
        "simulation://old-version",
    )
    current = lab.candidate(12345)
    assert (
        current["version"] == 2
        and current["draft"] == "New source version"
        and current["item_id"] is None
    )
    assert current["content_executions"][0]["item_id"] == item["id"]
    assert current["content_executions"][0]["candidate_version"] == 1
    assert current["content_executions"][0]["state"] == "SIMULATED"
    assert lab.engine.task(result["task_id"])["items"][0]["status"] == "confirmed"


def test_foreign_existing_execution_is_never_overwritten(lab):
    _, _, item = bind(lab)
    with lab.store.connect(write=True) as db:
        db.execute(
            "UPDATE hf_candidates SET task_id='preserve-task',item_id='preserve-item',state='VERIFYING' WHERE source_event_id='12345'"
        )
    fixture_result(
        lab, item, "confirmed", {"verified": True, "native_id": "fixture"}, "simulation://fixture"
    )
    current = lab.candidate(12345)
    assert (current["task_id"], current["item_id"], current["state"]) == (
        "preserve-task",
        "preserve-item",
        "VERIFYING",
    )


def test_corrupt_binding_is_not_used_for_result_readback(lab):
    _, _, item = bind(lab)
    with lab.store.connect(write=True) as db:
        db.execute(
            "UPDATE hf_candidates SET task_id=NULL,item_id=NULL,state='READY' WHERE source_event_id='12345'"
        )
        db.execute("UPDATE items SET payload_hash=? WHERE id=?", ("f" * 64, item["id"]))
    fixture_result(
        lab, item, "confirmed", {"verified": True, "native_id": "fixture"}, "simulation://fixture"
    )
    current = lab.candidate(12345)
    assert current["item_id"] is None and current["state"] == "READY"
    assert not current["content_executions"][0]["verified_binding"]


def test_legacy_attempt_and_new_author_share_the_same_enforced_user_limit():
    from test_engine import EngineTests

    lab = EngineTests(methodName="runTest")
    lab.setUp()
    try:
        old = lab.start(lab.task("x", user_daily_limit=1), [lab.item("x", recipient_id="author")])
        lab.engine.run_once(old["id"])
        assert lab.provider.sends == 1
        assert "source_author_id" not in lab.engine.task(old["id"])["items"][0]["payload"]
        new = lab.start(
            lab.task("x", user_daily_limit=1),
            [lab.item("x", target="https://x.com/author/status/67890", source_author_id="author")],
        )
        lab.engine.run_once(new["id"])
        assert lab.provider.sends == 1
        blocked = lab.engine.task(new["id"])["items"][0]
        assert (
            blocked["status"] == "not_attempted"
            and blocked["result"]["skip_reason"] == "activity_limit"
        )
        other = lab.start(
            lab.task("x", user_daily_limit=1),
            [lab.item("x", target="https://x.com/other/status/98765", source_author_id="other")],
        )
        lab.engine.run_once(other["id"])
        assert lab.provider.sends == 2
    finally:
        lab.doCleanups()


def test_stale_publish_job_reuses_the_bound_task(lab):
    _, result, item = bind(lab)
    before = lab.candidate(12345)
    with lab.store.connect(write=True) as db:
        lab.s.queue.put(db, "publish", before["id"] + "|" + str(before["version"]))
    lab.s.run_stage("publish", max_jobs=10)
    after = lab.candidate(12345)
    assert after == before
    assert after["task_id"] == result["task_id"] and after["item_id"] == item["id"]
    assert len(ContentQueue(lab.engine).list()) == 1
    assert not lab.store.rows("SELECT * FROM attempts")


@pytest.mark.parametrize("metadata", ["[]", "null", "broken json"])
def test_malformed_optional_provenance_cannot_block_receipt_readback(lab, metadata):
    _, _, item = bind(lab)
    content_id = ContentQueue(lab.engine).list()[0]["id"]
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE content_queue_entries SET context=? WHERE id=?", (metadata, content_id))
    fixture_result(
        lab, item, "confirmed", {"verified": True, "native_id": "fixture"}, "simulation://fixture"
    )
    current = lab.candidate(12345)
    assert current["state"] == "READY" and not current["content_executions"][0]["verified_binding"]
    assert lab.engine.task(current["task_id"])["items"][0]["status"] == "confirmed"


def test_content_execution_does_not_depend_on_the_search_owner_authorization(lab):
    from auto_promotion.interaction_intents import candidate_guard

    _, _, item = bind(lab)
    with lab.store.connect(write=True) as db:
        policy = lab.s.policy(lab.p["id"])["config"]
        policy.update(enabled=False, account_ids=[])
        db.execute("UPDATE hf_policies SET config=? WHERE id=?", (json.dumps(policy), lab.p["id"]))
        assert candidate_guard(db, item["id"], lab.now) is None
