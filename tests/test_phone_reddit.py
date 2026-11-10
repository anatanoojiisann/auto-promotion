"""Synthetic AP queue and HTTP responses; never a real social submission."""

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from phone_fleet_protocol import ExecutionRequest, fingerprint

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.backends.common import (
    BackendError,
    read_profile,
    reddit_phone_enabled,
    save_profile,
)
from auto_promotion.cloud_control import CloudControl
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import AccountInput, ItemInput, TaskInput
from auto_promotion.phone_reddit import PhoneReddit, attach_worker
from auto_promotion.queue_store import JobQueue, job_scope
from auto_promotion.store import Store
from auto_promotion.task_channels import TaskChannelRouter

META = {
    "post_id": "abc123",
    "subreddit": "test",
    "title": "AI video workflow",
    "author": "source_author",
}
URL = "https://www.reddit.com/r/test/comments/abc123/ai_video/"
BODY = "A specific test question about AI video."


class SyntheticFleet:
    def __init__(self):
        self.requests, self.snapshots, self.lose_next_create = [], {}, False
        self.submit_authorize = None

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/capabilities"):
            from phone_fleet_protocol import VERSION

            return httpx.Response(
                200,
                json={
                    "version": VERSION,
                    "operations": {
                        x: "supported"
                        for x in (
                            "observe",
                            "draft_validate",
                            "social_identity",
                            "social_prepare",
                            "social_submit",
                            "social_membership",
                            "social_join",
                            "reddit_identity",
                            "reddit_prepare",
                            "reddit_submit",
                        )
                    },
                },
            )
        if request.method == "POST":
            body = json.loads(request.content)
            typed = ExecutionRequest.model_validate(body)
            identifier = "execution_" + str(len(self.snapshots))
            result = {
                "execution_id": identifier,
                "command_id": "command-test",
                "device_id": "device-test",
                "status": "succeeded",
                "simulated": False,
                "external_effect": "none",
                "quiescent": True,
                "observed_at": time.time(),
                "observation_sha256": "a" * 64,
                "observed_identity_sha256": typed.expected_identity_sha256,
                "observed_draft_sha256": fingerprint({"text": typed.reddit.text}),
                "reddit": {
                    "identity": typed.reddit.identity,
                    "post_id": typed.reddit.post_id,
                    "text_sha256": fingerprint({"text": typed.reddit.text}),
                    "editor_verified": True,
                    "submit_control_observed": True,
                    "discarded": typed.reddit.discard_after_prepare,
                    "submission_started": False,
                },
            }
            snapshot = {
                "id": identifier,
                "request": body,
                "status": "succeeded",
                "desired": "run",
                "revision": 1,
                "created_at": time.time(),
                "updated_at": time.time(),
                "blocked_reason": None,
                "command_id": "command-test",
                "device_id": "device-test",
                "node_id": "node-test",
                "external_effect": "none",
                "result": result,
            }
            self.snapshots[identifier] = snapshot
            if typed.operation == "reddit_submit" and self.submit_authorize:
                snapshot.update(status="running", result=None)
            if self.lose_next_create:
                self.lose_next_create = False
                raise httpx.ReadTimeout("Synthetic create response loss")
            return httpx.Response(200, json=snapshot)
        if request.url.path.endswith("lookup"):
            rows = [
                r
                for r in self.snapshots.values()
                if r["request"]["business_task_id"] == request.url.params["business_task_id"]
                and r["request"]["task_item_id"] == request.url.params["task_item_id"]
                and r["request"]["operation"] == request.url.params["operation"]
            ]
            return httpx.Response(200 if rows else 404, json=rows[0] if rows else {})
        snapshot = self.snapshots[request.url.path.rsplit("/", 1)[1]]
        if snapshot["status"] == "running" and self.submit_authorize:
            value = snapshot["request"]
            self.submit_authorize(
                {
                    "authorization_id": value["reddit"]["authorization_id"],
                    "request_sha256": fingerprint(value),
                    "native_command_id": "native-submit-synthetic",
                    "execution_id": snapshot["id"],
                    "device_id": value["device_id"],
                }
            )
            at = time.time()
            receipt = {
                "comment_id": "t1_def456",
                "post_id": "t3_abc123",
                "author": "test_account",
                "text_sha256": fingerprint({"text": BODY}),
                "permalink": "https://www.reddit.com/r/test/comments/abc123/ai_video/def456/",
                "created_utc": at,
                "observed_at": at + 0.1,
            }
            result = {
                "execution_id": snapshot["id"],
                "command_id": "command-test",
                "device_id": value["device_id"],
                "status": "succeeded",
                "simulated": False,
                "external_effect": "confirmed",
                "quiescent": True,
                "reddit": {
                    "identity": "u/test_account",
                    "post_id": "abc123",
                    "editor_verified": True,
                    "submission_started": True,
                    "submitted_at": at - 0.1,
                    "receipt": receipt,
                },
            }
            snapshot.update(
                status="succeeded", result=result, external_effect="confirmed", revision=2
            )
        return httpx.Response(200, json=snapshot)


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ACCOUNTS_DIR", str(tmp_path / "private-accounts"))
    monkeypatch.setenv("AUTO_PROMOTION_LIVE_WRITES", "1")
    monkeypatch.setenv("AP_REDDIT_OWNER", "auto-promotion")
    monkeypatch.delenv("AP_PHONE_REDDIT_CONFIG", raising=False)
    monkeypatch.setattr(CloudControl, "configured", lambda: None)
    store = Store(tmp_path / "queue.db")
    engine = Engine(store, AdapterRegistry(tmp_path), live_enabled=True)
    with store.connect(write=True) as db:
        db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
        db.execute("INSERT INTO meta VALUES('execution_runtime','pooled')")
    account = engine.add_account(
        AccountInput(
            platform="reddit",
            label="Synthetic phone test",
            backend_label="synthetic-reddit-phone",
            mode="live",
            min_interval=120,
        )
    )
    fake = SyntheticFleet()
    config = {
        "enabled": True,
        "submit_enabled": False,
        "fleet_url": "http://127.0.0.1:18440",
        "service_token": "synthetic-business-token",
        "runtime_token": "synthetic-runtime-token",
        "bindings": {account["id"]: {"device_id": "device-test", "identity": "u/test_account"}},
    }
    service = PhoneReddit(engine, config, transport=httpx.MockTransport(fake))
    engine.adapters.phone_reddit = service
    service.register(account["id"], {**META, "target_url": URL, "text": BODY})
    engine.check_account(account["id"], force=True)
    now = time.time()

    def iso(n):
        return datetime.fromtimestamp(n, timezone.utc).isoformat()

    task = engine.create_task(
        TaskInput(
            name="Synthetic phone pilot",
            account_ids=[account["id"]],
            policy={
                "execution_channel": "local",
                "execution_method": "phone",
                "starts_at": iso(now - 5),
                "ends_at": iso(now + 1800),
                "min_interval": 120,
                "max_joins": 0,
            },
        )
    )
    engine.set_items(
        task["id"],
        [
            ItemInput(
                account_id=account["id"],
                action="reply",
                target=URL,
                text=BODY,
                scheduled_at=iso(now),
            )
        ],
    )
    task = engine.task(task["id"])
    yield SimpleNamespace(
        store=store,
        engine=engine,
        service=service,
        fake=fake,
        account=account,
        task=task,
        item=task["items"][0],
        monkeypatch=monkeypatch,
    )


def test_binding_and_preparation_do_not_start_task_or_consume_attempt(pilot):
    s = pilot
    assert reddit_phone_enabled(s.engine.account(s.account["id"]), "reply")
    result = s.service.prepare(s.item["id"], META, discard=True)
    assert result["status"] == "succeeded" and result["result"]["reddit"]["discarded"]
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    assert s.engine.task(s.task["id"])["state"] != "active"
    assert not s.service.backend(s.account, "preflight", {"payload": s.item["payload"]})["allowed"]
    assert not any(
        json.loads(r.content).get("operation") == "reddit_submit"
        for r in s.fake.requests
        if r.method == "POST"
    )


def test_disabled_submit_rejects_even_a_direct_execute_without_creating_fleet_work(pilot):
    s = pilot
    count = len(s.fake.requests)
    result = s.service.backend(s.account, "execute", {})
    assert result["confirmed_not_submitted"] and not result["submission_started"]
    assert len(s.fake.requests) == count


def test_explicit_api_policy_never_silently_falls_back_to_phone(pilot):
    s = pilot
    s.service.config = s.service.config.model_copy(
        update={"submit_enabled": True, "authorized_task_ids": [s.task["id"]]}
    )
    with s.store.connect(write=True) as db:
        row = db.execute("SELECT policy FROM tasks WHERE id=?", (s.task["id"],)).fetchone()
        policy = json.loads(row["policy"])
        policy["execution_method"] = "api"
        db.execute("UPDATE tasks SET policy=? WHERE id=?", (json.dumps(policy), s.task["id"]))
    count = len(s.fake.requests)
    with pytest.raises(BackendError, match="MethodOrNodeMismatch"):
        s.service.execute(
            s.account,
            {"item_id": s.item["id"], "task_id": s.task["id"], "payload": s.item["payload"]},
            {},
        )
    assert len(s.fake.requests) == count


def test_unavailable_primary_read_blocks_before_any_outbound_attempt(pilot):
    s = pilot
    s.service.prepare(s.item["id"], META)
    s.service.config = s.service.config.model_copy(update={"submit_enabled": True})

    class DeniedReader:
        async def target(self, context):
            raise RuntimeError("Synthetic official read denied")

    s.service.reader = DeniedReader()
    result = s.service.backend(s.account, "preflight", {"payload": s.item["payload"]})
    assert not result["allowed"] and result["reason"] == "PhoneRedditTargetReadUnavailable"
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0


def test_lost_prepare_response_is_only_looked_up_never_reposted(pilot):
    s = pilot
    request = s.service.make_request(
        s.account["id"], "reddit_prepare", {**META, "target_url": URL, "text": BODY}
    )
    s.fake.lose_next_create = True
    assert s.service.dispatch(request, "lost-test", s.account["id"])["status"] == "unknown"
    assert s.service.dispatch(request, "lost-test", s.account["id"])["status"] == "succeeded"
    matching = [
        r
        for r in s.fake.requests
        if r.method == "POST" and r.headers.get("idempotency-key") == "lost-test"
    ]
    assert len(matching) == 1
    with pytest.raises(Conflict, match="ImmutableRequestChanged"):
        s.service.dispatch(
            request.model_copy(update={"deadline": request.deadline + 1}),
            "lost-test",
            s.account["id"],
        )


def claim_from_original_queue(s):
    s.service.config = s.service.config.model_copy(
        update={"submit_enabled": True, "authorized_task_ids": [s.task["id"]]}
    )
    s.service.prepare(s.item["id"], META)
    s.engine.start_task(s.task["id"], source="synthetic_test_explicit_start")
    queue = JobQueue(s.engine)
    queue.sync(s.task["id"])
    job = queue.claim(
        "reddit.phone", "synthetic-worker", lease_seconds=180, reference_id=s.item["id"]
    )
    assert job
    context, _ = TaskChannelRouter(s.engine).prepare(job)
    with job_scope(job), s.engine.adapters.execution_job(context):
        claim = s.engine._claim(s.item["id"], {"allowed": True})
    assert claim
    return claim, context


def seed_submit(s):
    claim, context = claim_from_original_queue(s)
    result = s.service.execute(s.engine.account(s.account["id"]), claim, context)
    assert result["status"] == "unknown"  # Synthetic server has no public comment receipt.
    row = s.store.one("SELECT * FROM phone_reddit_authorizations")
    operation = s.store.one("SELECT * FROM phone_reddit_operations WHERE id=?", (row["id"],))
    body = {
        "authorization_id": row["id"],
        "request_sha256": row["request_hash"],
        "native_command_id": "native-submit-test",
        "execution_id": operation["execution_id"],
        "device_id": row["device_id"],
    }
    return body


def test_normal_once_quota_channel_claim_and_final_permission_are_reused(pilot):
    s = pilot
    body = seed_submit(s)
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 1
    assert s.service.authorize(body, "synthetic-runtime-token")["submit_once"]
    with pytest.raises(Conflict, match="Fenced"):
        s.service.authorize(body, "synthetic-runtime-token")
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 1


def test_stop_or_wrong_runtime_blocks_immediate_submit_authority(pilot):
    s = pilot
    body = seed_submit(s)
    with pytest.raises(Conflict, match="Authentication"):
        s.service.authorize(body, "wrong-token")
    s.engine.set_stop(True)
    with pytest.raises((Conflict, BackendError), match="Stopped|stopped"):
        s.service.authorize(body, "synthetic-runtime-token")
    assert s.store.one("SELECT used_at FROM phone_reddit_authorizations")["used_at"] is None


def test_synthetic_verified_native_receipt_finishes_original_item_once(pilot):
    s = pilot
    claim, context = claim_from_original_queue(s)
    s.fake.submit_authorize = lambda body: s.service.authorize(body, "synthetic-runtime-token")
    result = s.service.execute(s.engine.account(s.account["id"]), claim, context)
    assert result["verified"] and result["external_reference"].endswith("/def456/")
    s.engine.finish(claim, result)
    assert (
        s.store.one("SELECT status FROM items WHERE id=?", (s.item["id"],))["status"] == "confirmed"
    )
    stored = s.store.one("SELECT result FROM items WHERE id=?", (s.item["id"],))
    assert json.loads(stored["result"])["comment_id"] == "t1_def456"
    count = len([r for r in s.fake.requests if r.method == "POST"])
    again = s.service.verify(s.engine.account(s.account["id"]), claim)
    assert again["verified"]
    assert len([r for r in s.fake.requests if r.method == "POST"]) == count


def test_worker_configuration_is_explicit_and_bound_to_one_database(pilot, tmp_path):
    s = pilot
    s.monkeypatch.setenv("AP_PHONE_PRIVATE_DIR", str(tmp_path))
    s.monkeypatch.setattr("auto_promotion.phone_private.private_directory_is_safe", lambda _: True)
    config = s.service.worker_config()
    s.monkeypatch.setenv("AP_PHONE_REDDIT_CONFIG", config)
    another = Engine(Store(tmp_path / "other.db"), AdapterRegistry(tmp_path))
    with pytest.raises(Conflict, match="DatabaseMismatch"):
        attach_worker(another)
    attach_worker(s.engine)
    assert not s.engine.adapters.phone_reddit.config.submit_enabled


def test_phone_registration_preserves_an_existing_other_connection(pilot):
    s = pilot
    save_profile(
        "reddit",
        s.account["backend_label"],
        {
            "identity": "u/test_account",
            "connection_type": "browser",
            "preserved_marker": "synthetic-original",
        },
    )
    before = len(s.fake.requests)
    with pytest.raises(Conflict, match="ExistingConnectionPreserved"):
        s.service.register(s.account["id"], {**META, "target_url": URL, "text": BODY})
    assert (
        read_profile("reddit", s.account["backend_label"])["preserved_marker"]
        == "synthetic-original"
    )
    assert len(s.fake.requests) == before


def test_fleet_oauth_refreshes_before_expiry_without_retrying_mutations(pilot):
    s = pilot
    calls = []

    def respond(request):
        if request.url.path.endswith("/token"):
            calls.append(request)
            return httpx.Response(
                200, json={"access_token": "synthetic-fresh-token", "expires_in": 300}
            )
        return s.fake(request)

    s.service.config = s.service.config.model_copy(
        update={
            "oauth_issuer": "http://127.0.0.1:18888/realms/test",
            "oauth_client_id": "business",
            "oauth_client_secret": __import__("pydantic").SecretStr("synthetic-secret"),
        }
    )
    s.service.transport = httpx.MockTransport(respond)
    assert s.service.fleet_token() == s.service.fleet_token() == "synthetic-fresh-token"
    assert len(calls) == 1
    s.service.token_until = 0
    assert s.service.fleet_token() == "synthetic-fresh-token" and len(calls) == 2


def test_round_stop_between_claim_check_and_reddit_native_grant_is_fenced(pilot):
    import auto_promotion.phone_reddit as module
    from auto_promotion.phone_rounds import PhoneRounds

    s = pilot
    claim, context = claim_from_original_queue(s)
    rounds = PhoneRounds(s.engine)
    now = time.time()
    task = s.store.one("SELECT manifest_hash FROM tasks WHERE id=?", (s.task["id"],))
    with s.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO phone_rounds(id,request_key,manifest_hash,state,created_at,started_at,submit_deadline,deadline) VALUES('synthetic-race','synthetic-race','fixture','running',?,?,?,?)",
            (now, now, now + 480, now + 600),
        )
        db.execute(
            "INSERT INTO phone_round_members(round_id,task_id,item_id,platform,position,payload_hash,manifest_hash,state) VALUES('synthetic-race',?,?,'reddit',0,?,?,'queued')",
            (s.task["id"], s.item["id"], s.item["payload_hash"], task["manifest_hash"]),
        )
    original = module.verify_claim

    def authorize(body):
        def stop_after_read(request):
            original(request)
            rounds.stop("synthetic-race")

        with s.monkeypatch.context() as patch:
            patch.setattr(module, "verify_claim", stop_after_read)
            return s.service.authorize(body, "synthetic-runtime-token")

    s.fake.submit_authorize = authorize
    with pytest.raises(Conflict, match="PhoneRoundNotRunning"):
        s.service.execute(s.engine.account(s.account["id"]), claim, context)
    grant = s.store.one("SELECT * FROM phone_reddit_authorizations")
    assert grant and grant["used_at"] is None


def test_legacy_reddit_receipt_queries_original_without_resubmission(pilot):
    s = pilot
    claim, context = claim_from_original_queue(s)
    s.service.execute(s.engine.account(s.account["id"]), claim, context)
    row = s.store.one("SELECT * FROM phone_reddit_operations WHERE kind='reddit_submit'")
    original = json.loads(row["request"])
    original["version"] = "fleet.internal/0.1"
    s.fake.snapshots[row["execution_id"]]["request"] = original
    with s.store.connect(write=True) as db:
        db.execute(
            "UPDATE phone_reddit_operations SET request=? WHERE id=?",
            (json.dumps(original), row["id"]),
        )
    before = len([r for r in s.fake.requests if r.method == "POST"])
    result = s.service.verify(s.engine.account(s.account["id"]), claim)
    assert result["legacy_read_only"] and not result["verified"]
    assert result["original_execution_id"] == row["execution_id"]
    assert len([r for r in s.fake.requests if r.method == "POST"]) == before
