"""Explicit five-platform scope; no Facebook actions, fixtures never publish."""

import pytest
from test_phone_rounds import finish, make_lab

from auto_promotion.core import Conflict
from auto_promotion.phone_rounds import PLATFORMS
from auto_promotion.phone_rounds_api import RoundInput


@pytest.fixture
def lab(tmp_path):
    return make_lab(tmp_path)


def five(lab):
    return [task for task in lab.ids if task != "task-facebook"]


def test_explicit_five_round_keeps_original_tasks_and_requires_five_receipts(lab):
    rid = lab.rounds.create(five(lab), "five-app-round-1")["id"]
    started = lab.rounds.start(rid)
    assert started["expected_platform_count"] == 5
    assert {m["platform"] for m in started["members"]} == PLATFORMS - {"facebook"}
    for _ in range(8):
        lab.rounds.tick()
    assert sorted(lab.calls) == sorted(five(lab)) and "task-facebook" not in lab.calls
    for platform in PLATFORMS - {"facebook"}:
        finish(lab, platform)
    lab.rounds.tick()
    result = lab.rounds.get(rid)
    assert result["state"] == "completed" and result["confirmed_count"] == 5
    assert result["all_members_within_budget"] is True
    assert result["all_six_within_budget"] is False and result["end_to_end_six_app_pass"] is False
    assert result["end_to_end_selected_apps_pass"] is False
    assert result["selected_platforms"] == sorted(PLATFORMS - {"facebook"})


def test_any_other_five_platform_subset_is_rejected(lab):
    tasks = [task for task in lab.ids if task != "task-telegram"]
    with pytest.raises(Conflict, match="SupportedPlatforms"):
        lab.rounds.create(tasks, "wrong-five-scope")
    assert lab.store.one("SELECT COUNT(*) n FROM phone_rounds")["n"] == 0


def test_mismatched_six_platform_preflight_cannot_admit_five_task_round(lab):
    rid = lab.rounds.create(five(lab), "five-app-round-2")["id"]
    lab.rounds.preflight_check = lambda rounds, identifier: {
        "round_id": identifier,
        "submission_authorized": False,
        "members": [{"platform": p, "ready": True, "blockers": []} for p in sorted(PLATFORMS)],
    }
    result = lab.rounds.start(rid)
    assert result["state"] == "blocked"
    assert result["preflight"]["blockers"] == ["PhoneRoundPreflightUnavailable"]
    assert not lab.calls and lab.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0


def test_api_input_accepts_five_or_six_but_no_smaller_collection():
    from pydantic import ValidationError

    assert (
        len(RoundInput(task_ids=["a", "b", "c", "d", "e"], request_key="request-123").task_ids) == 5
    )
    with pytest.raises(ValidationError):
        RoundInput(task_ids=["a", "b", "c", "d"], request_key="request-123")


def test_five_platforms_continue_only_for_the_ready_member_when_others_block(lab):
    from test_phone_round_preflight import services

    services(lab, fail_social=True)
    rid = lab.rounds.create(five(lab), "five-app-partial")["id"]
    lab.rounds.preflight_check = None
    result = lab.rounds.start(rid)
    assert result["state"] == "running" and result["preflight"]["ready_count"] == 1
    for _ in range(8):
        lab.rounds.tick()
    assert lab.calls == ["task-reddit"]
    result = lab.rounds.get(rid)
    assert sum(m["stage"] == "blocked" for m in result["members"]) == 4
    assert result["all_members_within_budget"] is False
    assert "task-facebook" not in lab.calls


def test_four_receipts_do_not_complete_a_selected_five_platform_round(lab):
    rid = lab.rounds.create(five(lab), "five-app-four-only")["id"]
    lab.rounds.start(rid)
    for _ in range(8):
        lab.rounds.tick()
    for platform in PLATFORMS - {"facebook", "x"}:
        finish(lab, platform)
    lab.rounds.tick()
    result = lab.rounds.get(rid)
    assert result["state"] == "running" and result["confirmed_count"] == 4
    assert result["all_members_within_budget"] is False
