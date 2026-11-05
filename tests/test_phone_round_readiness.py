"""Synthetic reviewed candidates and local readiness. No provider requests."""

from datetime import datetime, timedelta, timezone

import pytest
from test_phone_rounds import create, make_lab

from auto_promotion.models import canonical
from auto_promotion.phone_round_readiness import preflight, shortlist
from auto_promotion.store import Store


@pytest.fixture
def lab(tmp_path):
    return make_lab(tmp_path)


def iso(at):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=at)).isoformat()


def candidate(
    lab,
    suffix="one",
    *,
    age=0,
    permission="allowed",
    account_ids=None,
    reviewed=True,
    expires=1000,
    published=-20,
    draft="Contextual Aurax draft",
):
    account_id = lab.store.one("SELECT account_id FROM items WHERE id='item-x'")["account_id"]
    value = {
        "platform": "x",
        "external_id": suffix,
        "target": "https://x.com/source/status/" + suffix,
        "context": "Specific creator video question",
        "draft_text": draft,
        "action": "reply",
        "permission": permission,
        "permission_evidence": "Synthetic reviewed permission",
        "permission_account_ids": [account_id] if account_ids is None else account_ids,
        "expires_at": iso(lab.now[0] + expires),
        "published_at": iso(lab.now[0] + published),
    }
    with lab.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO opportunities VALUES(?,?,?, ?,1,?,?)",
            (suffix, "x", "key-" + suffix, canonical(value), lab.now[0] - age, lab.now[0] - age),
        )
        if reviewed:
            Store.audit(
                db,
                "growth.content_reviewed",
                {"opportunity_id": suffix, "revision": 1},
                now=lab.now[0],
            )
    return account_id


def test_shortlist_reuses_three_recent_reviewed_account_bound_candidates(lab):
    for suffix in ["a", "b", "c", "d"]:
        account_id = candidate(lab, suffix)
    result = shortlist(lab.engine, account_id)
    assert len(result["candidates"]) == 3
    assert result["read_only"] and result["submission_authorized"] is False
    assert not lab.calls and lab.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"age": 901}, "stale_or_short_window"),
        ({"expires": 599}, "stale_or_short_window"),
        ({"published": 1}, "stale_or_short_window"),
        ({"published": -73 * 3600}, "stale_or_short_window"),
        ({"permission": "unknown"}, "account_permission_required"),
        ({"account_ids": []}, "account_permission_required"),
        ({"account_ids": ["another-account"]}, "account_permission_required"),
        ({"reviewed": False}, "current_revision_review_required"),
        ({"draft": ""}, "contextual_draft_required"),
    ],
)
def test_pool_fails_closed_without_current_context_permission_and_review(lab, changes, reason):
    account_id = candidate(lab, **changes)
    result = shortlist(lab.engine, account_id)
    assert not result["candidates"] and result["excluded"][reason] == 1


def test_changed_content_revision_invalidates_prior_review(lab):
    account_id = candidate(lab)
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE opportunities SET revision=2")
    assert not shortlist(lab.engine, account_id)["candidates"]


def test_already_scheduled_target_is_excluded_without_sending(lab):
    account_id = candidate(lab)
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE tasks SET state='active' WHERE id='task-x'")
        db.execute(
            "UPDATE items SET payload=json_set(payload,'$.target',?) WHERE id='item-x'",
            ("https://x.com/source/status/one",),
        )
    result = shortlist(lab.engine, account_id)
    assert result["excluded"]["target_already_scheduled"] == 1 and not lab.calls


def test_preflight_reports_all_six_blockers_without_native_work(lab):
    round_id = create(lab)["id"]
    result = preflight(lab.rounds, round_id)
    assert len(result["members"]) == 6 and result["ready_count"] == 0
    assert all("PhoneBindingRequired" in row["blockers"] for row in result["members"])
    assert result["submission_authorized"] is False and not lab.calls
