"""Selected phone queues, stop races and recovery. All outbound work here is simulated."""

import json
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from threading import Event

import pytest
from test_phone_round_preflight import services
from test_phone_rounds import finish, make_lab

from auto_promotion.core import Conflict, NotFound
from auto_promotion.phone_rounds import PLATFORMS, PhoneRounds, submission_blocker

SCOPES = [scope for n in range(1, 7) for scope in combinations(sorted(PLATFORMS), n)]


@pytest.fixture
def lab(tmp_path):
    return make_lab(tmp_path)


@pytest.mark.parametrize("platforms", SCOPES, ids=lambda s: "+".join(s))
def test_every_selection_freezes_only_its_tasks(lab, platforms):
    ids = ["task-" + platform for platform in platforms]
    created = lab.rounds.create(ids, "all-selections")
    assert created["selected_platforms"] == list(platforms)
    assert lab.rounds.create(ids[::-1], "all-selections")["id"] == created["id"]
    assert not lab.calls
    if len(ids) == 1:
        lab.rounds.start(created["id"])
        lab.rounds.tick()
        finish(lab, platforms[0])
        lab.rounds.tick()
        result = lab.rounds.get(created["id"])
        assert lab.calls == ids and result["confirmed_count"] == 1
        assert result["state"] == "completed" and result["all_members_within_budget"]
        assert not result["all_six_within_budget"]


def test_aliases_share_unknown_history_and_delivery_limits_on_the_original_account(lab):
    from auto_promotion import quotas
    from auto_promotion.models import canonical, digest
    from auto_promotion.readiness import telegram_blocker

    finish(lab, "telegram", outcome="unknown", verified=False)
    with lab.store.connect(write=True) as db:
        db.execute(
            "UPDATE items SET payload=json_set(payload,'$.target','@Test_Group') WHERE id='item-telegram'"
        )
        db.execute(
            "UPDATE attempts SET target_key=? WHERE item_id='item-telegram'",
            (digest(["telegram", "@test_group"]),),
        )
        acct = dict(db.execute("SELECT * FROM accounts WHERE platform='telegram'").fetchone())
        policy = {
            "execution_method": "phone",
            "max_joins": 0,
            "daily_limit": 5,
            "target_daily_limit": 1,
            "user_daily_limit": 5,
        }
        task = {"id": "other-original", "policy": canonical(policy), "campaign_id": None}
        payload = {
            "platform": "telegram",
            "action": "send_message",
            "text": "new text",
            "account_id": acct["id"],
            "target": "https://t.me/test_group/",
        }
        assert (
            telegram_blocker(db, task, payload, 10000) == "PhoneTelegramPriorSubmissionUnresolved"
        )
        assert quotas.check_claim(db, acct, task, payload, 10000, 10000, "other-item") == "activity_limit"
        # When the historical attempt was made earlier, its delivery reservation still counts.
        db.execute("UPDATE attempts SET started_at=started_at-90000 WHERE item_id='item-telegram'")
        assert quotas.check_claim(db, acct, task, payload, 10000, 10000, "other-item") == "delivery_limit"
        payload["target"] = "https://t.me/different_group"
        assert telegram_blocker(db, task, payload, 10000) is None
        assert quotas.check_claim(db, acct, task, payload, 10000, 10000, "other-item") is None


@pytest.mark.asyncio
async def test_chat_control_survives_client_reconnect_and_scopes_the_start(lab):
    pytest.importorskip("mcp")
    from auto_promotion.mcp_server import create_server

    services(lab)
    server = create_server(lab.engine, phone_rounds_enabled=True)

    async def call(s, name, **args):
        reply = await s.call_tool(name, args)
        return (
            reply
            if isinstance(reply, dict)
            else reply[1]
            if isinstance(reply, tuple)
            else json.loads(reply[0].text)
        )

    created = await call(
        server,
        "create_phone_round",
        task_ids=["task-telegram", "task-reddit"],
        request_key="same-chat-round",
    )
    reconnected = create_server(lab.engine, phone_rounds_enabled=True)
    running = await call(
        reconnected, "start_phone_round", round_id=created["id"], platform="reddit"
    )
    assert {m["platform"]: m["stage"] for m in running["members"]} == {
        "reddit": "pending",
        "telegram": "unstarted",
    }
    assert not lab.calls
    lab.rounds.tick()
    stopped = await call(
        reconnected, "stop_phone_round", round_id=created["id"], platform="telegram"
    )
    assert stopped["id"] == created["id"] and lab.calls == ["task-reddit"]


@pytest.mark.asyncio
async def test_authenticated_operator_can_start_one_app_without_starting_its_peer(lab):
    import httpx

    from auto_promotion.api import create_app

    services(lab)
    app = create_app(
        db_path=lab.store.path,
        registry=lab.engine.adapters,
        clock=lab.engine.clock,
        token="test-operator",
        worker=False,
        phone_rounds_enabled=True,
    )
    app.state.engine.live_enabled = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        h = {"Authorization": "Bearer test-operator"}
        r = await client.post(
            "/api/phone-rounds",
            headers=h,
            json={"task_ids": ["task-reddit", "task-telegram"], "request_key": "route-scope"},
        )
        assert r.status_code == 200
        base = "/api/phone-rounds/" + r.json()["id"] + "/platforms/"
        assert (await client.post(base + "reddit/start")).status_code == 401
        assert (await client.get(base + "facebook/preflight", headers=h)).status_code == 404
        r = await client.post(base + "reddit/start", headers=h)
        assert r.status_code == 200
        assert {m["platform"]: m["stage"] for m in r.json()["members"]} == {
            "reddit": "pending",
            "telegram": "unstarted",
        }
        r = await client.post(base + "telegram/stop", headers=h)
        assert r.json()["state"] == "running" and r.json()["confirmed_count"] == 0
    assert not lab.calls


def test_rejects_empty_duplicate_and_unselected_control(lab):
    for ids in ([], ["task-x", "task-x"]):
        with pytest.raises(Conflict):
            lab.rounds.create(ids, "bad-selection")
    rid = lab.rounds.create(["task-x"], "selected-x")["id"]
    for action in (lab.rounds.start, lab.rounds.stop_platform):
        with pytest.raises(NotFound):
            action(rid, "tiktok")
    assert not lab.calls and lab.rounds.get(rid)["started_at"] is None


def test_one_slow_or_stopped_platform_does_not_delay_a_separate_start(lab):
    rid = lab.rounds.create(["task-reddit", "task-telegram"], "parallel-members")["id"]
    entered, release = Event(), Event()

    def check(rounds, ident, *, platforms):
        assert len(platforms) == 1
        platform = next(iter(platforms))
        if platform == "telegram":
            entered.set()
            assert release.wait(8)
        return {
            "round_id": ident,
            "submission_authorized": False,
            "members": [{"platform": platform, "ready": True, "blockers": []}],
        }

    lab.rounds.preflight_check = check
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(lab.rounds.start, rid, "telegram")
        assert entered.wait(5)
        try:
            reddit = lab.rounds.start(rid, "reddit")
            assert reddit["state"] == "running"
            lab.rounds.tick()
            assert lab.calls == ["task-reddit"]
            lab.rounds.stop_platform(rid, "telegram")
        finally:
            release.set()
        slow.result(timeout=5)
    assert lab.rounds.start(rid, "telegram")["preflight_checks"] == 2
    lab.rounds.tick()
    members = {m["platform"]: m for m in lab.rounds.get(rid)["members"]}
    assert members["telegram"]["stage"] == "stopped" and members["reddit"]["stage"] == "queued"
    with lab.store.connect() as db:
        assert submission_blocker(db, "task-telegram", lab.now[0]) == "PhoneRoundNotRunning"
        assert submission_blocker(db, "task-reddit", lab.now[0]) is None


def test_partial_recheck_keeps_clock_and_queued_or_unknown_originals(lab):
    services(lab, fail_social=True)
    lab.rounds.preflight_check = None
    rid = lab.rounds.create(["task-reddit", "task-tiktok", "task-telegram"], "recheck-selection")[
        "id"
    ]
    initial = lab.rounds.start(rid)
    lab.rounds.tick()
    finish(lab, "reddit", outcome="unknown", verified=False)
    lab.rounds.stop_platform(rid, "reddit")
    lab.now[0] += 60
    checks = services(lab)
    later = lab.rounds.start(rid, "telegram")
    assert later["deadline"] == initial["deadline"] and later["preflight"]["platforms"] == [
        "telegram"
    ]
    assert checks == [(False, "GET", "/capabilities")]
    lab.rounds.tick()
    assert lab.calls == ["task-reddit", "task-telegram"]
    members = {m["platform"]: m for m in later["members"]}
    assert members["reddit"]["stage"] == "unknown" and members["reddit"]["attempt_id"]
    assert members["tiktok"]["stage"] == "blocked"
    assert not later["all_members_within_budget"]


def test_slow_fleet_metadata_blocks_only_its_service(lab, monkeypatch):
    from auto_promotion import phone_round_readiness as readiness

    services(lab)
    lab.rounds.preflight_check = None
    rid = lab.rounds.create(["task-reddit", "task-telegram"], "timeout-isolation")["id"]
    parked = Event()
    old = lab.engine.adapters.phone_social.request

    def slow(*args):
        assert parked.wait(6)
        return old(*args)

    monkeypatch.setattr(lab.engine.adapters.phone_social, "request", slow)
    monkeypatch.setattr(readiness, "FLEET_CHECK_SECONDS", 0.25)
    try:
        result = lab.rounds.start(rid)
        assert result["preflight"]["ready_count"] == 1
        member = next(m for m in result["members"] if m["platform"] == "telegram")
        assert member["reason"] == "PhoneFleetReadinessTimeout"
        lab.rounds.tick()
        assert lab.calls == ["task-reddit"]
    finally:
        parked.set()


def test_waiting_original_cannot_hold_other_apps_or_reduce_completion_denominator(lab):
    rid = lab.rounds.create(["task-instagram", "task-telegram"], "independent-originals")["id"]
    lab.rounds.start(rid)
    with lab.store.connect(write=True) as db:
        db.execute(
            "UPDATE phone_round_members SET state='starting',start_requested_at=10000,start_lease_until=10120 WHERE task_id='task-instagram'"
        )
    assert lab.rounds.tick() == "task-telegram"
    finish(lab, "telegram")
    with lab.store.connect(write=True) as db:
        db.execute("DELETE FROM items WHERE id='item-instagram'")
    lab.rounds.tick()
    state = lab.rounds.get(rid)
    assert state["expected_platform_count"] == 2 and state["confirmed_count"] == 1
    assert state["state"] != "completed" and not state["all_members_within_budget"]


def test_old_inflight_preflight_requires_explicit_recheck(lab):
    rid = lab.rounds.create(["task-telegram", "task-reddit"], "upgrade-originals")["id"]
    with lab.store.connect(write=True) as db:
        db.execute("ALTER TABLE phone_round_members DROP COLUMN check_id")
        db.execute("UPDATE phone_round_members SET state='pending' WHERE round_id=?", (rid,))
        db.execute(
            "UPDATE phone_rounds SET state='checking',started_at=10000,submit_deadline=10480,deadline=10600 WHERE id=?",
            (rid,),
        )
    upgraded = PhoneRounds(lab.engine)
    assert upgraded.get(rid)["state"] == "blocked"
    assert upgraded.tick() is None and not lab.calls
    services(lab)
    upgraded.start(rid, "reddit")
    upgraded.tick()
    assert lab.calls == ["task-reddit"] and upgraded.get(rid)["deadline"] == 10600


@pytest.mark.parametrize("binding", ["social", "reddit"])
def test_normal_service_cli_loads_only_requested_phone_binding(tmp_path, monkeypatch, binding):
    import sys

    import uvicorn

    from auto_promotion import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTO_PROMOTION_LIVE_WRITES", "0")
    conf = tmp_path / "synthetic-binding.json"
    conf.write_text(
        json.dumps(
            {
                "enabled": True,
                "runtime_token": "synthetic-only-at-least-24",
                "bindings": {},
                "fleet_url": "http://127.0.0.1:18888",
                "service_token": "synthetic-only",
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "auto-promotion",
            "--db",
            str(tmp_path / "test.db"),
            "serve",
            "--no-worker",
            "--phone-rounds",
            f"--phone-{binding}-config",
            str(conf),
        ],
    )
    apps = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: apps.append(app))
    cli.main()
    engine = apps[0].state.engine
    assert getattr(engine.adapters, "phone_" + binding).config.submit_enabled is False
    assert (
        getattr(engine.adapters, "phone_" + ("reddit" if binding == "social" else "social"), None)
        is None
    )
    assert not engine.live_enabled
