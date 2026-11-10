"""Existing discovery/generation fixtures stop at content saving in the opt-in mode."""

import copy

import pytest

from auto_promotion.content_queue import ContentQueue
from auto_promotion.core import Conflict
from auto_promotion.interaction_models import ConfigSave, InteractionConfig
from auto_promotion.interaction_simulation import source


@pytest.fixture
def lab():
    from test_interactions import InteractionTests

    value = InteractionTests(methodName="runTest")
    value.setUp()
    yield value
    value.doCleanups()


def configure_queue(lab):
    value = lab.cfg.model_copy(update={"policy_key": "content-only", "content_queue_only": True})
    lab.p = lab.s.save(ConfigSave(config=value))


def test_legacy_policy_bytes_do_not_gain_a_new_default_field():
    old = InteractionConfig().model_dump()
    assert "content_queue_only" not in old
    assert InteractionConfig.model_validate(old).model_dump() == old


def test_automatic_generation_saves_content_without_checking_a_publisher_or_creating_sends(lab):
    configure_queue(lab)
    lab.round([source(12345, "P1", lab.now)])
    # Discovery already has its own source proof. Phone/publisher readiness must not gate production.
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET status='unconfigured' WHERE id=?", (lab.account["id"],))

    def no_publisher(*args, **kwargs):
        raise AssertionError("content production touched a publisher")

    lab.io.eligibility = no_publisher
    lab.s.run_stage("generate", max_jobs=100)
    entries = ContentQueue(lab.engine).list()
    assert len(entries) == 1 and entries[0]["binding"] is None
    assert entries[0]["context"]["producer"] == "hf_candidate"
    assert not lab.store.rows("SELECT * FROM tasks")
    assert not lab.store.rows(
        "SELECT * FROM execution_jobs WHERE kind IN ('send','join','hf_publish')"
    )
    lab.s.schedule()
    assert not lab.store.rows("SELECT * FROM execution_jobs WHERE kind='hf_publish'")
    candidate = lab.candidate(12345)
    with lab.store.connect(write=True) as db:
        lab.s.queue.put(db, "publish", candidate["id"] + "|" + str(candidate["version"]))
    lab.s.run_stage("publish", max_jobs=100)
    assert len(ContentQueue(lab.engine).list()) == 1
    assert not lab.store.rows("SELECT * FROM attempts")


def test_existing_policy_cannot_silently_switch_between_production_and_sending(lab):
    before = copy.deepcopy(lab.s.policy(lab.p["id"]))
    value = InteractionConfig.model_validate({**before["config"], "content_queue_only": True})
    with pytest.raises(Conflict, match="CreateNewPolicy"):
        lab.s.save(ConfigSave(config=value, expected_version=before["version"]))
    assert lab.s.policy(lab.p["id"]) == before


def test_joint_production_never_assigns_a_sender():
    from test_interaction_joint import JointTests

    lab = JointTests(methodName="runTest")
    lab.setUp()
    try:
        config = {
            **lab.s.policy(lab.p["id"])["config"],
            "policy_key": "joint-content-only",
            "content_queue_only": True,
        }
        lab.p = lab.s.save(ConfigSave(config=config))
        lab.scan([lab.event()])
        assert lab.c()["execution_account_id"] is None
        lab.s.run_stage("generate", max_jobs=100)
        entries = ContentQueue(lab.engine).list()
        assert len(entries) == 1 and entries[0]["binding"] is None
        assert not lab.store.rows("SELECT * FROM hf_processing_tasks")
        assert not lab.store.rows("SELECT * FROM tasks")
        assert not lab.store.rows("SELECT * FROM execution_jobs WHERE kind='hf_publish'")
        from auto_promotion.interaction_models import CandidateAction

        lab.s.action(
            CandidateAction(
                action="edit",
                versions={lab.c()["id"]: lab.c()["version"]},
                text="Updated synthetic reply.",
            )
        )
        lab.s.run_stage("generate", max_jobs=100)
        assert (
            lab.c()["execution_account_id"] is None
            and lab.c()["draft"] == "Updated synthetic reply."
        )
        assert len(ContentQueue(lab.engine).list()) == 2
        assert not lab.store.rows("SELECT * FROM hf_assignment_events")
        assert not lab.store.rows(
            "SELECT * FROM execution_jobs WHERE kind IN ('hf_assign','hf_publish')"
        )
    finally:
        lab.doCleanups()
