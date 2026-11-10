"""Synthetic admission, shared deadline, read-only readiness and stop races."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from test_phone_rounds import create, make_lab

from auto_promotion.phone_protocol import expected_version
from auto_promotion.phone_round_readiness import preflight
from auto_promotion.phone_rounds import PLATFORMS, PhoneRounds


@pytest.fixture
def lab(tmp_path):
    return make_lab(tmp_path)


def ready(rounds, identifier):
    return {
        "round_id": identifier,
        "submission_authorized": False,
        "members": [
            {"platform": m["platform"], "ready": True, "blockers": []}
            for m in rounds.get(identifier)["members"]
        ],
    }


def services(lab, *, observe="supported", submit="supported", fail_social=False):
    calls = []

    class Service:
        def __init__(self, reddit):
            self.reddit = reddit
            accounts = lab.store.rows("SELECT * FROM accounts")
            bindings = {
                a["id"]: SimpleNamespace(receipt_reader=None)
                for a in accounts
                if (a["platform"] == "reddit") == reddit
            }
            self.config = SimpleNamespace(
                bindings=bindings,
                submit_enabled=True,
                authorized_task_ids=lab.ids,
                telegram_observers={},
                reddit_read_access_token="synthetic-read-token",
            )

        def request(self, method, path):
            calls.append((self.reddit, method, path))
            if fail_social and not self.reddit:
                raise RuntimeError("private provider response must never be logged")
            return {
                "version": expected_version(),
                "operations": {
                    "observe": observe,
                    "social_submit": submit,
                    "reddit_submit": submit,
                },
            }

        def fresh(self, account):
            return True

        def preparation(self, *args, **kwargs):
            return (
                None,
                None,
                {
                    "status": "succeeded",
                    "result": {"simulated": False, "social": {"receipt_capable": True}},
                },
            )

    with lab.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET status='healthy'")
    lab.engine.adapters.phone_social = Service(False)
    lab.engine.adapters.phone_reddit = Service(True)
    return calls


def test_authenticated_capabilities_are_fetched_once_per_service_for_all_six_apps(lab):
    calls = services(lab)
    identifier = create(lab)["id"]
    result = preflight(lab.rounds, identifier)
    assert result["ready_count"] == 6 and result["capability_sources_checked"] == 2
    assert sorted(calls) == [(False, "GET", "/capabilities"), (True, "GET", "/capabilities")]
    assert not lab.calls and lab.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0


@pytest.mark.parametrize(
    "observe,submit,reason",
    [
        ("unsupported", "supported", "PhoneFleetDeviceNotReady"),
        ("supported", "unsupported", "PhoneFleetSubmissionUnsupported"),
    ],
)
def test_cached_local_identity_cannot_mask_missing_live_fleet_capability(
    lab, observe, submit, reason
):
    services(lab, observe=observe, submit=submit)
    result = preflight(lab.rounds, create(lab)["id"])
    assert result["ready_count"] == 0
    assert all(reason in m["blockers"] for m in result["members"])


def test_one_provider_error_does_not_hide_the_other_platforms(lab):
    services(lab, fail_social=True)
    result = preflight(lab.rounds, create(lab)["id"])
    assert result["ready_count"] == 1 and len(result["members"]) == 6
    assert next(m for m in result["members"] if m["platform"] == "reddit")["ready"]
    assert "private provider" not in json.dumps(result)


def test_default_start_blocks_all_missing_prerequisites_before_original_tasks_start(lab):
    identifier = create(lab)["id"]
    lab.rounds = PhoneRounds(lab.engine)
    result = lab.rounds.start(identifier)
    assert result["state"] == "blocked" and result["preflight_checks"] == 1
    assert result["preflight"]["ready_count"] == 0
    assert all("PhoneBindingRequired" in m["blockers"] for m in result["preflight"]["members"])
    assert not lab.calls and lab.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    lab.rounds.tick()
    assert not lab.calls


def test_explicit_recheck_preserves_the_original_clock_and_each_check_record(lab):
    identifier = create(lab)["id"]
    lab.rounds.preflight_check = None
    blocked = lab.rounds.start(identifier)
    lab.now[0] += 45
    assert lab.rounds.get(identifier)["elapsed_seconds"] == 45
    lab.rounds.preflight_check = ready
    resumed = lab.rounds.start(identifier)
    assert resumed["state"] == "running" and resumed["deadline"] == blocked["deadline"] == 10600
    assert resumed["preflight_checks"] == 2 and resumed["elapsed_seconds"] == 45
    assert lab.store.one("SELECT COUNT(*) n FROM phone_round_preflights")["n"] == 2
    assert not lab.calls


def test_preflight_time_is_included_and_does_not_claim_cold_start_acceptance(lab):
    identifier = create(lab)["id"]

    def costly(rounds, rid):
        lab.now[0] += 12
        return ready(rounds, rid)

    lab.rounds.preflight_check = costly
    result = lab.rounds.start(identifier)
    assert result["state"] == "running" and result["preflight_seconds"] == 12
    assert result["deadline"] == 10600 and result["remaining_seconds"] == 588
    assert result["timing_scope"] == "prepared_tasks_including_preflight"
    assert result["preparation_included"] is False and result["end_to_end_six_app_pass"] is False


def test_excessive_preflight_cost_blocks_before_any_original_start(lab):
    identifier = create(lab)["id"]

    def late(rounds, rid):
        lab.now[0] += 31
        return ready(rounds, rid)

    lab.rounds.preflight_check = late
    result = lab.rounds.start(identifier)
    assert result["state"] == "blocked"
    assert "PhoneRoundPreflightBudgetElapsed" in result["preflight"]["blockers"]
    lab.rounds.tick()
    assert not lab.calls


def test_stop_during_readiness_wins_and_no_task_can_be_started(lab):
    identifier = create(lab)["id"]

    def stopped(rounds, rid):
        rounds.stop(rid)
        return ready(rounds, rid)

    lab.rounds.preflight_check = stopped
    assert lab.rounds.start(identifier)["state"] == "stopped"
    lab.rounds.tick()
    assert not lab.calls


def test_parallel_start_checks_once_and_never_restarts_the_deadline(lab):
    identifier = create(lab)["id"]
    entered, release = Event(), Event()
    checks = []

    def checking(rounds, rid):
        checks.append(rid)
        entered.set()
        assert release.wait(5)
        return ready(rounds, rid)

    lab.rounds.preflight_check = checking
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lab.rounds.start, identifier)
        assert entered.wait(5)
        second = pool.submit(lab.rounds.start, identifier).result(timeout=5)
        assert second["state"] == "checking" and not lab.calls
        release.set()
        done = first.result(timeout=5)
    assert done["state"] == "running" and checks == [identifier] and done["preflight_checks"] == 1


def test_stop_on_blocked_round_is_terminal_and_retains_evidence(lab):
    identifier = create(lab)["id"]
    lab.rounds.preflight_check = None
    lab.rounds.start(identifier)
    lab.rounds.stop(identifier)
    lab.rounds.preflight_check = ready
    assert lab.rounds.start(identifier)["state"] == "stopped"
    assert lab.rounds.get(identifier)["preflight_checks"] == 1 and not lab.calls


def test_preflight_failure_is_sanitized_and_keeps_original_tasks_unattempted(lab):
    identifier = create(lab)["id"]

    def failed(*args):
        raise RuntimeError("secret provider token")

    lab.rounds.preflight_check = failed
    result = lab.rounds.start(identifier)
    assert result["state"] == "blocked" and "secret provider" not in json.dumps(result)
    assert result["preflight"]["blockers"] == ["PhoneRoundPreflightUnavailable"]
    assert not lab.calls


def test_blocked_time_expires_without_readiness_retry_or_native_work(lab):
    identifier = create(lab)["id"]
    lab.rounds.preflight_check = None
    lab.rounds.start(identifier)
    lab.now[0] += 601
    lab.rounds.tick()
    result = lab.rounds.get(identifier)
    assert result["state"] == "expired" and result["elapsed_seconds"] == 600
    assert result["preflight_checks"] == 1 and not lab.calls


def test_clock_rollback_and_inconsistent_ready_flags_cannot_admit_tasks(lab):
    identifier = create(lab)["id"]

    def rollback(rounds, rid):
        lab.now[0] -= 1
        return ready(rounds, rid)

    lab.rounds.preflight_check = rollback
    result = lab.rounds.start(identifier)
    assert (
        result["state"] == "blocked" and "PhoneRoundClockInvalid" in result["preflight"]["blockers"]
    )
    lab.now[0] += 2

    def inconsistent(rounds, rid):
        result = ready(rounds, rid)
        result["members"][0]["blockers"] = ["MissingPermission"]
        return result

    lab.rounds.preflight_check = inconsistent
    result = lab.rounds.start(identifier)
    assert result["state"] == "running" and result["preflight"]["ready_count"] == 5
    blocked = next(m for m in result["members"] if m["platform"] == sorted(PLATFORMS)[0])
    assert blocked["stage"] == "blocked" and blocked["reason"] == "MissingPermission"
    assert not lab.calls


def test_platform_with_ready_prerequisites_continues_while_others_remain_blocked(lab):
    services(lab, fail_social=True)
    identifier = create(lab)["id"]
    lab.rounds.preflight_check = None
    result = lab.rounds.start(identifier)
    assert result["state"] == "running" and result["preflight"]["ready_count"] == 1
    for _ in range(12):
        lab.rounds.tick()
    result = lab.rounds.get(identifier)
    assert lab.calls == ["task-reddit"]
    assert sum(m["stage"] == "blocked" for m in result["members"]) == 5
    assert result["all_six_within_budget"] is False


def test_explicit_start_refreshes_only_stale_account_and_preserves_readonly_checks(lab):
    from auto_promotion.phone_round_readiness import preflight

    services(lab)
    service = lab.engine.adapters.phone_social
    stale = {"value": True}
    refreshed = []
    service.fresh = lambda account: account["platform"] != "telegram" or not stale["value"]

    def ensure(account_id, **kwargs):
        refreshed.append(account_id)
        stale["value"] = False

    service.ensure_identity = ensure
    identifier = create(lab)["id"]
    snapshot = preflight(lab.rounds, identifier)
    assert not refreshed
    assert next(m for m in snapshot["members"] if m["platform"] == "telegram")["blockers"] == [
        "FreshPhoneIdentityRequired"
    ]
    lab.rounds.preflight_check = None
    snapshot = lab.rounds.start(identifier)
    assert len(refreshed) == 1
    assert snapshot["preflight"]["ready_count"] == 6
