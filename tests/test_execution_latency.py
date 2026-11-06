"""Deterministic time and synthetic services only; no external publication."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_account_control_readiness import channels as control_fixture
from test_account_control_readiness import state
from test_phone_round_preflight import services
from test_phone_rounds import create, make_lab
from test_phone_social import pilot as phone_fixture

from auto_promotion.cloud_control import CloudUnavailable
from auto_promotion.core import Conflict
from auto_promotion.execution_timing import report, stage
from auto_promotion.models import AccountInput
from auto_promotion.queue_store import JobQueue
from auto_promotion.task_channels import TaskChannelRouter

channels = control_fixture
pilot = phone_fixture


def failed_control(t):
    task, account = t.fixture.ready(channel="local")
    state(t.store, "cloud_control_required", "1")
    job = t.claim(task)
    try:
        TaskChannelRouter(t.engine).prepare(job)
    except CloudUnavailable as exc:
        assert str(exc) == "CloudControlConfigurationMissing"
        t.queue.settle(job, exc)
    else:
        pytest.fail("missing configuration was admitted")
    return task, account, job


def test_unchanged_configuration_never_retries_and_other_account_changes_do_not_wake(channels):
    t = channels
    task, _, job = failed_control(t)
    initial = t.store.one("SELECT * FROM execution_jobs WHERE id=?", (job["id"],))
    for _ in range(4):
        t.now += 35
        t.queue.sync()
        assert t.queue.claim("simulation.x", "next") is None
    state(
        t.store,
        "execution_channel",
        {
            "active": "local",
            "offline_only": True,
            "account_ids": ["unrelated-app"],
            "until": t.now + 1000,
        },
    )
    assert t.queue.claim("simulation.x", "next") is None
    result = t.store.one("SELECT * FROM execution_jobs WHERE id=?", (job["id"],))
    assert result["available_at"] == result["expires_at"] == initial["expires_at"]
    assert json.loads(result["result"])["control_wait"]["failures"] == 1
    assert t.store.one("SELECT count(*) n FROM attempts")["n"] == 0
    assert len(t.queue.pools(task["id"])["control_waits"]) == 1


def test_configuration_change_wakes_original_job_only(channels):
    t = channels
    task, _, job = failed_control(t)
    state(t.store, "cloud_control_required", "0")
    resumed = t.queue.claim("simulation.x", "next")
    assert resumed and resumed["id"] == job["id"] and resumed["version"] == job["version"]
    assert t.store.one("SELECT count(*) n FROM execution_jobs")["n"] == 1
    assert t.store.one("SELECT count(*) n FROM attempts")["n"] == 0
    assert t.engine.task(task["id"])["state"] == "active"


@pytest.mark.parametrize("blocked", ["paused", "unknown", "expired"])
def test_configuration_change_never_replays_paused_unknown_or_expired_work(channels, blocked):
    t = channels
    task, _, job = failed_control(t)
    with t.store.connect(write=True) as db:
        if blocked == "paused":
            db.execute("UPDATE tasks SET state='paused' WHERE id=?", (task["id"],))
        elif blocked == "unknown":
            db.execute("UPDATE items SET status='unknown' WHERE id=?", (job["reference_id"],))
        else:
            t.now = job["expires_at"] + 1
    state(t.store, "cloud_control_required", "0")
    assert t.queue.claim("simulation.x", "next") is None
    assert t.store.one("SELECT count(*) n FROM attempts")["n"] == 0
    if blocked == "unknown":
        assert (
            t.store.one("SELECT status FROM items WHERE id=?", (job["reference_id"],))["status"]
            == "unknown"
        )


def test_transient_backoff_is_bounded_and_honors_remote_retry_after(channels):
    t = channels
    task, _ = t.fixture.ready(channel="local")
    job = t.claim(task)
    for delay in (30, 60, 120, 240, 300):
        exc = CloudUnavailable("CloudTransportUnavailable")
        exc.account_control_failure = True
        t.queue.settle(job, exc)
        row = t.store.one("SELECT * FROM execution_jobs WHERE id=?", (job["id"],))
        assert row["available_at"] == t.now + delay
        assert t.queue.claim("simulation.x", "early") is None
        t.now += delay
        job = t.queue.claim("simulation.x", "retry")
        assert job
    exc = CloudUnavailable("CloudTransportUnavailable", retry_after_seconds=1000)
    exc.account_control_failure = True
    t.queue.settle(job, exc)
    row = t.store.one("SELECT * FROM execution_jobs WHERE id=?", (job["id"],))
    assert row["available_at"] == row["expires_at"]
    assert json.loads(row["result"])["control_wait"]["retry_at"] is None
    assert t.store.one("SELECT count(*) n FROM attempts")["n"] == 0


def test_parked_account_does_not_prevent_another_account_claim(channels):
    t = channels
    _, _, parked = failed_control(t)
    account = t.engine.add_account(
        AccountInput(
            platform="x",
            label="Independent account",
            backend_label="x-independent",
            mode="simulation",
            min_interval=0,
            daily_limit=50,
            hourly_limit=20,
        )
    )
    other, _ = t.fixture.ready(account=account, channel="local", target_id=98765)
    t.queue.sync(other["id"])
    job = t.queue.claim("simulation.x", "other")
    assert job and job["id"] != parked["id"] and job["task_id"] == other["id"]


def test_round_cutoff_shortens_existing_queue_and_never_restarts_clock(tmp_path):
    lab = make_lab(tmp_path)
    identifier = lab.rounds.create(["task-x"], "cutoff-fixture")["id"]
    started = lab.rounds.start(identifier)
    lab.rounds.tick()
    queue = JobQueue(lab.engine)
    queue.sync("task-x")
    job = lab.store.one("SELECT * FROM execution_jobs WHERE task_id='task-x'")
    assert job["expires_at"] == started["submit_deadline"]
    # Simulate an older worker's longer queue expiration and recover in place.
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE execution_jobs SET expires_at=expires_at+400 WHERE id=?", (job["id"],))
    queue.recover()
    assert (
        lab.store.one("SELECT expires_at FROM execution_jobs WHERE id=?", (job["id"],))[
            "expires_at"
        ]
        == started["submit_deadline"]
    )
    lab.now[0] = started["submit_deadline"] + 1
    queue.recover()
    assert lab.store.one("SELECT status FROM items WHERE id='item-x'")["status"] == "missed"
    seconds = lab.rounds.get(identifier)["members"][0]["queue_seconds"]
    lab.now[0] += 2000
    assert lab.rounds.get(identifier)["members"][0]["queue_seconds"] == seconds


@pytest.mark.parametrize("stop", [False, True])
def test_group_start_admits_fast_apps_before_slow_service_returns(tmp_path, stop):
    lab = make_lab(tmp_path)
    services(lab)
    lab.rounds.preflight_check = None
    identifier = create(lab)["id"]
    entered, release, admitted = Event(), Event(), Event()
    service = lab.engine.adapters.phone_reddit
    original = service.request

    def slow(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    service.request = slow
    apply = lab.rounds._apply_check

    def watched(*args, **kwargs):
        result = apply(*args, **kwargs)
        if "telegram" in args[2] and not kwargs["final"]:
            admitted.set()
        return result

    lab.rounds._apply_check = watched
    with patch("auto_promotion.cloud_control.CloudControl.configured", return_value=None):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lab.rounds.start, identifier)
            try:
                assert entered.wait(3) and admitted.wait(3)
                assert not future.done()
                if stop:
                    lab.rounds.stop(identifier)
                for _ in range(6):
                    lab.rounds.tick()
                assert len(lab.calls) == (0 if stop else 5)
                assert "task-reddit" not in lab.calls
            finally:
                release.set()
            result = future.result(timeout=5)
    assert result["started_at"] == 10000 and result["deadline"] == 10600
    if stop:
        assert result["state"] == "stopped"


def test_slow_provider_timeout_only_blocks_its_members(tmp_path, monkeypatch):
    lab = make_lab(tmp_path)
    services(lab)
    lab.rounds.preflight_check = None
    release = Event()
    service = lab.engine.adapters.phone_reddit
    original = service.request

    def slow(*args):
        assert release.wait(5)
        return original(*args)

    service.request = slow
    monkeypatch.setattr("auto_promotion.phone_round_readiness.FLEET_CHECK_SECONDS", 0.05)
    try:
        with patch("auto_promotion.cloud_control.CloudControl.configured", return_value=None):
            result = lab.rounds.start(create(lab)["id"])
        assert result["preflight"]["ready_count"] == 5
        assert next(m for m in result["preflight"]["members"] if m["platform"] == "reddit")[
            "blockers"
        ] == ["PhoneFleetReadinessTimeout"]
    finally:
        release.set()


def test_stage_timing_records_durations_without_claiming_business_success(tmp_path, monkeypatch):
    lab = make_lab(tmp_path)
    ticks = iter([2.0, 6.25])
    monkeypatch.setattr(
        "auto_promotion.execution_timing.time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    with stage(lab.engine, "task-x", "receipt_verification", item_id="item-x"):
        lab.now[0] += 4
    with lab.store.connect() as db:
        result = report(db, "task-x")
    assert result["spans"][0]["duration_seconds"] == 4.25
    assert result["spans"][0]["call_outcome"] == "returned"
    assert lab.store.one("SELECT status FROM items WHERE id='item-x'")["status"] == "not_attempted"


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_expired_new_phone_operation_does_not_create_intent_or_post(pilot):
    s = pilot
    request = s.service.make_request(
        s.account["id"],
        "social_prepare",
        {
            **s.metadata,
            "text": s.item["payload"]["text"],
            "target_url": s.item["payload"]["target"],
        },
        task_id=s.task["id"],
        item_id=s.item["id"],
    ).model_copy(update={"deadline": 1})
    before = len(s.fake.requests)
    with pytest.raises(Conflict, match="PhoneExecutionDeadlineElapsed"):
        s.service.dispatch(request, "expired-test", s.account["id"], s.item["id"])
    assert len(s.fake.requests) == before
    assert not s.store.one("SELECT id FROM phone_social_operations WHERE id='expired-test'")


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
@pytest.mark.parametrize("clock_rollback", [False, True])
def test_phone_polling_backs_off_avoids_duplicate_writes_and_keeps_original_lookup(
    pilot, monkeypatch, clock_rollback
):
    s = pilot
    request = s.service.make_request(
        s.account["id"],
        "social_prepare",
        {
            **s.metadata,
            "text": s.item["payload"]["text"],
            "target_url": s.item["payload"]["target"],
        },
        task_id=s.task["id"],
        item_id=s.item["id"],
    )
    now = [request.not_before]
    request = request.model_copy(update={"deadline": now[0] + 4})
    sleeps, calls, snapshots = [], [], []
    elapsed = [0.0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        elapsed[0] += seconds
        if clock_rollback and len(sleeps) == 1:
            now[0] -= 1000

    original = s.service.request

    def transport(method, path, body=None, key=None):
        calls.append((method, path, now[0]))
        if method == "POST":
            value = original(method, path, body, key)
            value.update(status="running", result=None)
            snapshots.append(value)
            return value
        if path.startswith("/executions/"):
            return snapshots[0]
        return original(method, path, body, key)

    monkeypatch.setattr(
        "auto_promotion.phone_reddit.time",
        SimpleNamespace(time=lambda: now[0], sleep=sleep, monotonic=lambda: elapsed[0]),
    )
    monkeypatch.setattr(s.service, "request", transport)
    with s.store.connect(write=True) as db:
        db.executescript("""
            CREATE TABLE synthetic_snapshot_writes(n INTEGER);
            CREATE TRIGGER synthetic_snapshot_counter AFTER UPDATE OF result ON phone_social_operations
            BEGIN INSERT INTO synthetic_snapshot_writes VALUES(1); END;
        """)
    result = s.service.dispatch(request, "poll-fixture", s.account["id"], s.item["id"])
    assert result["status"] == "running"
    assert elapsed[0] == pytest.approx(4)
    assert sleeps[:4] == pytest.approx([0.2, 0.4, 0.8, 1.6])
    assert len([c for c in calls if c[0] == "GET" and c[1].startswith("/executions/")]) == 4
    assert all(at < request.deadline for _, _, at in calls)
    assert s.store.one("SELECT count(*) n FROM synthetic_snapshot_writes")["n"] == 1
    before = len(calls)
    now[0] = request.deadline
    s.service.dispatch(request, "poll-fixture", s.account["id"], s.item["id"])
    assert len(calls) == before + 1 and calls[-1][0] == "GET"
    assert sum(method == "POST" for method, _, _ in calls) == 1
    assert s.store.one("SELECT count(*) n FROM synthetic_snapshot_writes")["n"] == 1

    # A successful query must clear a transport-uncertain marker even if its
    # snapshot is unchanged. It still must not issue another create request.
    with s.store.connect(write=True) as db:
        db.execute("UPDATE phone_social_operations SET phase='uncertain' WHERE id='poll-fixture'")
    s.service.dispatch(request, "poll-fixture", s.account["id"], s.item["id"])
    assert (
        s.store.one("SELECT phase FROM phone_social_operations WHERE id='poll-fixture'")["phase"]
        == "linked"
    )
    assert s.store.one("SELECT count(*) n FROM synthetic_snapshot_writes")["n"] == 2
    assert sum(method == "POST" for method, _, _ in calls) == 1


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_capability_delay_cannot_create_an_expired_phone_intent(pilot, monkeypatch):
    s = pilot
    request = s.service.make_request(
        s.account["id"],
        "social_prepare",
        {
            **s.metadata,
            "text": s.item["payload"]["text"],
            "target_url": s.item["payload"]["target"],
        },
        task_id=s.task["id"],
        item_id=s.item["id"],
    )
    now = [request.not_before]
    original = s.service.request

    def delayed(method, path, body=None, key=None):
        value = original(method, path, body, key)
        now[0] = request.deadline + 1
        return value

    monkeypatch.setattr("auto_promotion.phone_reddit.time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(s.service, "request", delayed)
    before = sum(r.method == "POST" for r in s.fake.requests)
    with pytest.raises(Conflict, match="PhoneExecutionDeadlineElapsed"):
        s.service.dispatch(request, "capability-timeout", s.account["id"], s.item["id"])
    assert sum(r.method == "POST" for r in s.fake.requests) == before
    assert not s.store.one("SELECT id FROM phone_social_operations WHERE id='capability-timeout'")


def test_failed_stage_is_timed_and_does_not_suppress_error(tmp_path):
    lab = make_lab(tmp_path)
    with pytest.raises(TimeoutError):
        with stage(lab.engine, "task-x", "account_control"):
            raise TimeoutError("synthetic timeout")
    with lab.store.connect() as db:
        value = report(db, "task-x")
    assert value["spans"][0]["call_outcome"] == "raised"
    assert value["spans"][0]["duration_seconds"] >= 0
    assert value["spans"][0]["in_progress"] is False


def test_retry_diagnostic_respects_existing_account_wait(channels):
    t = channels
    task, account = t.fixture.ready(channel="local")
    job = t.claim(task)
    with t.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET next_at=? WHERE id=?", (t.now + 100, account["id"]))
    exc = CloudUnavailable("CloudTransportUnavailable")
    exc.account_control_failure = True
    t.queue.settle(job, exc)
    row = t.store.one("SELECT * FROM execution_jobs WHERE id=?", (job["id"],))
    assert row["available_at"] == t.now + 100
    assert json.loads(row["result"])["control_wait"]["retry_at"] == row["available_at"]
