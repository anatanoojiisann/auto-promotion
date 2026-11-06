"""Isolated synthetic control/preflight tests: no platform networking or sends."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
import test_task_channels as channel_cases
from test_phone_round_preflight import services
from test_phone_rounds import create, make_lab

from auto_promotion.cloud_control import CloudUnavailable
from auto_promotion.local_authorization import authorization_route, validate_scope
from auto_promotion.models import canonical
from auto_promotion.phone_round_readiness import preflight
from auto_promotion.task_channels import TaskChannelRouter, control_preflight

pytest_plugins = ("test_phone_social",)


@pytest.fixture
def channels():
    test = channel_cases.TaskChannelTests()
    test.setUp()
    try:
        yield test
    finally:
        test.doCleanups()


def state(store, key, value):
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value if isinstance(value, str) else canonical(value)),
        )


def dump(store):
    with store.connect() as db:
        return list(db.iterdump())


@pytest.mark.parametrize("expired", [False, True])
def test_other_accounts_offline_intent_does_not_disable_this_control_route(channels, expired):
    t = channels
    task, account = t.fixture.ready(channel="local")
    _, _, request, _ = t.fake_cloud()
    offline = dict(
        active="local",
        offline_only=True,
        authorization="local_queue",
        account_ids=["other-platform-account"],
        until=t.now + (-1 if expired else 600),
    )
    state(t.store, "execution_channel", offline)
    state(t.store, "cloud_control_required", "1")
    before = dump(t.store)
    result = control_preflight(t.engine, task["id"], account["id"], task["policy"])
    assert result["ownership"] == "coordination_required"
    assert dump(t.store) == before
    request.assert_not_called()
    context, cloud = TaskChannelRouter(t.engine).prepare(t.claim(task))
    assert context["channel"] == "local" and cloud is not None
    assert any(c.args[0] == "/v1/account-channel" for c in request.call_args_list)
    assert (
        json.loads(t.store.one("SELECT value FROM meta WHERE key='execution_channel'")["value"])
        == offline
    )
    assert t.store.one("SELECT count(*) n FROM attempts")["n"] == 0


def test_missing_control_fails_identically_before_claim_or_phone_work(channels):
    t = channels
    task, account = t.fixture.ready(channel="local")
    state(t.store, "cloud_control_required", "1")
    before = dump(t.store)
    with pytest.raises(CloudUnavailable, match="^CloudControlConfigurationMissing$"):
        control_preflight(t.engine, task["id"], account["id"], task["policy"])
    assert dump(t.store) == before
    with pytest.raises(CloudUnavailable, match="^CloudControlConfigurationMissing$"):
        TaskChannelRouter(t.engine).prepare(t.claim(task))
    assert t.store.one("SELECT count(*) n FROM account_task_channels")["n"] == 0
    assert t.store.one("SELECT count(*) n FROM attempts")["n"] == 0


def test_scoped_expired_offline_intent_never_turns_itself_online(channels):
    t = channels
    task, account = t.fixture.ready(channel="local")
    state(t.store, "cloud_control_required", "1")
    state(
        t.store,
        "execution_channel",
        dict(
            active="local",
            offline_only=True,
            authorization="local_queue",
            account_ids=[account["id"]],
            until=t.now - 1,
        ),
    )
    t.cloud_config.side_effect = AssertionError("must not construct cloud transport")
    with pytest.raises(CloudUnavailable, match="CloudControlConfigurationMissing"):
        control_preflight(t.engine, task["id"], account["id"], task["policy"])
    with pytest.raises(CloudUnavailable, match="CloudControlConfigurationMissing"):
        TaskChannelRouter(t.engine).prepare(t.claim(task))


def test_control_change_after_preflight_is_rechecked_at_actual_dispatch(channels):
    t = channels
    task, account = t.fixture.ready(channel="local")
    assert (
        control_preflight(t.engine, task["id"], account["id"], task["policy"])["ownership"]
        == "local_fence"
    )
    state(t.store, "cloud_control_required", "1")
    with pytest.raises(CloudUnavailable, match="CloudControlConfigurationMissing"):
        TaskChannelRouter(t.engine).prepare(t.claim(task))


def block_account(lab, platform):
    account_id = lab.store.one("SELECT account_id FROM items WHERE id=?", ("item-" + platform,))[
        "account_id"
    ]
    with lab.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO account_task_channels VALUES(?,?,?,?,?,'handoff','pending',?,?,?)",
            (
                account_id,
                "local",
                "blocked-generation",
                lab.now[0] + 600,
                "task-" + platform,
                lab.now[0],
                "ControlPlaneError",
                lab.now[0] + 60,
            ),
        )


def test_bad_reddit_control_skips_remote_read_and_other_five_apps_can_start(tmp_path):
    lab = make_lab(tmp_path)
    calls = services(lab)
    block_account(lab, "reddit")
    lab.rounds.preflight_check = None
    with patch("auto_promotion.cloud_control.CloudControl.configured", return_value=None):
        result = lab.rounds.start(create(lab)["id"])
    assert result["preflight"]["ready_count"] == 5
    assert calls == [(False, "GET", "/capabilities")]
    assert next(m for m in result["preflight"]["members"] if m["platform"] == "reddit")[
        "blockers"
    ] == ["ControlPlaneError"]
    for _ in range(6):
        lab.rounds.tick()
    assert len(lab.calls) == 5 and "task-reddit" not in lab.calls
    assert lab.store.one("SELECT count(*) n FROM attempts")["n"] == 0


def test_all_missing_control_skips_both_capability_services(tmp_path):
    lab = make_lab(tmp_path)
    calls = services(lab)
    identifier = create(lab)["id"]
    state(lab.store, "cloud_control_required", "1")
    before = dump(lab.store)
    with patch("auto_promotion.cloud_control.CloudControl.configured", return_value=None):
        result = preflight(lab.rounds, identifier)
    assert result["ready_count"] == result["capability_sources_checked"] == 0
    assert not calls and not lab.calls and dump(lab.store) == before


def test_individual_app_start_does_not_wait_for_other_app_provider(tmp_path):
    lab = make_lab(tmp_path)
    services(lab)
    lab.rounds.preflight_check = None
    identifier = create(lab)["id"]
    entered, release = Event(), Event()
    service = lab.engine.adapters.phone_reddit
    original = service.request

    def slow(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    service.request = slow
    with patch("auto_promotion.cloud_control.CloudControl.configured", return_value=None):
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow_task = pool.submit(lab.rounds.start, identifier, "reddit")
            try:
                assert entered.wait(5)
                fast = pool.submit(lab.rounds.start, identifier, "telegram").result(timeout=3)
                assert not slow_task.done()
                assert (
                    next(m for m in fast["members"] if m["platform"] == "telegram")[
                        "admission_state"
                    ]
                    == "pending"
                )
                lab.rounds.tick()
                assert lab.calls == ["task-telegram"]
            finally:
                release.set()
            slow_task.result(timeout=5)


def test_phone_coordination_uses_registered_native_route_without_new_phone_requests(pilot):
    s = pilot
    before = len(s.fake.requests)
    account = s.engine.account(s.account["id"])
    if s.app == "telegram":
        s.monkeypatch.setattr("auto_promotion.core.telegram_blocker", lambda *a, **k: None)
    s.engine.start_task(s.task["id"], source="synthetic_test_explicit_start")
    before = len(s.fake.requests)
    with s.store.connect() as db:
        route = authorization_route(s.engine, db, account, s.task["policy"])
        validate_scope(s.engine, db, s.task["id"], account["id"], s.engine.clock())
    assert route.pool == s.app + ".phone" and route.resource.startswith("phone:")
    assert s.engine.task(s.task["id"])["channel"]["local_authorization_accounts"] == [account["id"]]
    assert len(s.fake.requests) == before
    assert s.store.one("SELECT count(*) n FROM attempts")["n"] == 0
    with patch.object(s.service, "fresh", return_value=False):
        with (
            s.store.connect() as db,
            pytest.raises(CloudUnavailable, match="FreshPhoneIdentityRequired"),
        ):
            authorization_route(s.engine, db, account, s.task["policy"])


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_phone_support_does_not_authorize_unreviewed_telegram_desktop_route(pilot):
    s = pilot
    with (
        s.store.connect() as db,
        pytest.raises(CloudUnavailable, match="LocalAuthorizationRouteUnsupported"),
    ):
        authorization_route(
            s.engine, db, s.engine.account(s.account["id"]), {"execution_method": "api"}
        )


def test_scoped_phone_authorization_reaches_same_queue_without_submission(pilot):
    from types import SimpleNamespace

    from auto_promotion.cloud_control import CloudControl
    from auto_promotion.local_authorization import authorize_local_channel
    from auto_promotion.queue_store import JobQueue

    s = pilot
    s.monkeypatch.setenv("AP_NODE_ID", "synthetic-control-node")
    if s.app == "telegram":
        s.monkeypatch.setattr("auto_promotion.core.telegram_blocker", lambda *a, **k: None)
    s.engine.start_task(s.task["id"], source="synthetic_explicit_start")
    state(s.store, "cloud_control_required", "1")
    state(
        s.store,
        "execution_channel",
        dict(
            active="local",
            offline_only=True,
            account_ids=[s.account["id"]],
            until=s.engine.clock() - 1,
        ),
    )
    remote, calls = {}, []

    def request(path, payload=None, **kwargs):
        calls.append(path)
        if path == "/v1/account-channel":
            remote.update(
                {
                    k: payload[k]
                    for k in ("account_id", "task_id", "node_id", "generation", "channel")
                }
            )
            remote.update(store_id=payload["local_store_id"], until_at=payload["until"])
        return {"channel": dict(remote) if remote else None}

    cloud = SimpleNamespace(
        node_id="synthetic-control-node",
        request=request,
        sync_channel_authorization=lambda *a, **k: None,
    )
    before = len(s.fake.requests)
    with patch.object(CloudControl, "configured", return_value=cloud):
        result = authorize_local_channel(
            s.engine,
            s.task["id"],
            s.account["id"],
            coordinate_once=True,
            source="synthetic_explicit_coordination",
        )
    assert result["authorized"] and not result["attempt_created"]
    assert calls.count("/v1/account-channel") == 1
    queue = JobQueue(s.engine)
    queue.sync(s.task["id"])
    with patch.object(
        CloudControl, "configured", side_effect=AssertionError("local proof must not call cloud")
    ):
        assert (
            control_preflight(s.engine, s.task["id"], s.account["id"], s.task["policy"])[
                "ownership"
            ]
            == "confirmed"
        )
        job = queue.claim(s.app + ".phone", "test-owner", reference_id=s.item["id"])
        assert job
        context, transport = TaskChannelRouter(s.engine).prepare(job)
    assert context["channel"] == "local" and transport is None
    assert context["generation"] == result["generation"]
    assert len(s.fake.requests) == before
    assert s.store.one("SELECT count(*) n FROM attempts")["n"] == 0
    assert s.store.one("SELECT count(*) n FROM phone_social_authorizations")["n"] == 0
