"""Synthetic fleet/native receipts; never accesses real social accounts."""

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from phone_fleet_protocol import ExecutionRequest, fingerprint

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.backends.common import BackendError, read_profile, save_profile
from auto_promotion.cloud_control import CloudControl
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import AccountInput, ItemInput, TaskInput
from auto_promotion.phone_social import ACTIONS, PhoneSocial, attach_worker
from auto_promotion.queue_store import JobQueue, job_scope
from auto_promotion.store import Store
from auto_promotion.task_channels import TaskChannelRouter

URLS = {
    "telegram": "https://t.me/test_group",
    "x": "https://x.com/source/status/12345",
    "instagram": "https://www.instagram.com/p/ABC123/",
    "facebook": "https://www.facebook.com/source/posts/23456",
    "tiktok": "https://www.tiktok.com/@source/video/34567",
}
IDS = {
    "telegram": "test_group",
    "x": "12345",
    "instagram": "ABC123",
    "facebook": "23456",
    "tiktok": "34567",
}
TEXT = "Specific AI video test question."


class SyntheticFleet:
    def __init__(self):
        self.requests, self.snapshots, self.lose_next, self.authorize = [], {}, False, None
        self.bad_receipt = False

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
            c = typed.social
            identifier = "execution_" + str(len(self.snapshots))
            result = {
                "execution_id": identifier,
                "command_id": "command-test",
                "device_id": typed.device_id,
                "status": "succeeded",
                "simulated": False,
                "external_effect": "none",
                "quiescent": True,
                "observed_at": time.time(),
                "observation_sha256": "a" * 64,
                "observed_identity_sha256": typed.expected_identity_sha256,
                "observed_draft_sha256": fingerprint({"text": c.text})
                if hasattr(c, "text")
                else None,
                "social": {
                    "platform": c.platform,
                    "identity": c.identity,
                    "target_id": getattr(c, "target_id", None),
                    "adapter_id": c.platform + "-synthetic",
                    "editor_verified": True,
                    "adapter_sha256": "a" * 64,
                    "submit_control_observed": True,
                    "receipt_capable": True,
                    "discarded": getattr(c, "discard_after_prepare", False),
                    "submission_started": False,
                },
            }
            row = {
                "id": identifier,
                "request": body,
                "status": "succeeded",
                "desired": "run",
                "revision": 1,
                "created_at": time.time(),
                "updated_at": time.time(),
                "blocked_reason": None,
                "command_id": "command-test",
                "device_id": typed.device_id,
                "node_id": "node-test",
                "external_effect": "none",
                "result": result,
            }
            self.snapshots[identifier] = row
            if typed.operation == "social_submit":
                row.update(status="running", result=None)
            if self.lose_next:
                self.lose_next = False
                raise httpx.ReadTimeout("Synthetic lost response")
            return httpx.Response(200, json=row)
        if request.url.path.endswith("lookup"):
            rows = [
                r
                for r in self.snapshots.values()
                if r["request"]["business_task_id"] == request.url.params["business_task_id"]
                and r["request"]["task_item_id"] == request.url.params["task_item_id"]
                and r["request"]["operation"] == request.url.params["operation"]
            ]
            return httpx.Response(200 if rows else 404, json=rows[0] if rows else {})
        row = self.snapshots[request.url.path.rsplit("/", 1)[1]]
        if row["status"] == "running":
            value, at = row["request"], time.time()
            c = value["social"]
            if self.authorize:
                self.authorize(
                    {
                        "authorization_id": c["authorization_id"],
                        "request_sha256": fingerprint(value),
                        "native_command_id": "single-native-click",
                        "execution_id": row["id"],
                        "device_id": value["device_id"],
                    }
                )
            if c["platform"] == "telegram":
                link = c["target_url"] + "/98765"
            elif c["platform"] == "x":
                link = "https://x.com/test_sender/status/98765"
            else:
                link = c["target_url"] + "?comment_id=98765"
            receipt = {
                "platform": c["platform"],
                "action": c["action"],
                "native_id": "98765",
                "target_id": c["target_id"],
                "author": "@wrong" if self.bad_receipt else c["identity"],
                "text_sha256": fingerprint({"text": c["text"]}),
                "permalink": link,
                "created_at": at,
                "observed_at": at + 0.1,
            }
            proof = {
                "platform": c["platform"],
                "identity": c["identity"],
                "adapter_id": c["platform"] + "-synthetic",
                "adapter_sha256": "a" * 64,
                "submission_started": True,
                "submitted_at": at - 0.1,
                "receipt": receipt,
            }
            row.update(
                status="succeeded",
                external_effect="confirmed",
                revision=2,
                result={
                    "execution_id": row["id"],
                    "command_id": "command-test",
                    "device_id": value["device_id"],
                    "status": "succeeded",
                    "simulated": False,
                    "external_effect": "confirmed",
                    "quiescent": True,
                    "social": proof,
                },
            )
        return httpx.Response(200, json=row)


@pytest.fixture(params=list(ACTIONS))
def pilot(request, tmp_path, monkeypatch):
    app = request.param
    monkeypatch.setenv("AP_ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setenv("AUTO_PROMOTION_LIVE_WRITES", "1")
    monkeypatch.setenv("AP_" + app.upper() + "_OWNER", "auto-promotion")
    monkeypatch.delenv("AP_PHONE_SOCIAL_CONFIG", raising=False)
    monkeypatch.setattr(CloudControl, "configured", lambda: None)
    store = Store(tmp_path / "queue.db")
    engine = Engine(store, AdapterRegistry(tmp_path), live_enabled=True)
    with store.connect(write=True) as db:
        db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
        db.execute("INSERT INTO meta VALUES('execution_runtime','pooled')")
    account = engine.add_account(
        AccountInput(
            platform=app,
            label="Synthetic phone " + app,
            backend_label="synthetic-phone-" + app,
            mode="live",
            min_interval=120,
        )
    )
    fake = SyntheticFleet()
    identity = "facebook:45678" if app == "facebook" else "@test_sender"
    config = {
        "enabled": True,
        "submit_enabled": False,
        "fleet_url": "http://127.0.0.1:18440",
        "service_token": "synthetic-service",
        "runtime_token": "synthetic-runtime-token-of-sufficient-length",
        "bindings": {
            account["id"]: {"device_id": "device-test", "platform": app, "identity": identity}
        },
    }
    service = PhoneSocial(engine, config, transport=httpx.MockTransport(fake))
    metadata = {
        "target_id": IDS[app],
        "target_author": "source"
        if app == "facebook"
        else "@test_group"
        if app == "telegram"
        else "@source",
        "target_text": "Frozen AI video target",
    }
    service.register(account["id"], {**metadata, "target_url": URLS[app], "text": TEXT})
    now = time.time()

    def iso(n):
        return datetime.fromtimestamp(n, timezone.utc).isoformat()

    task = engine.create_task(
        TaskInput(
            name="Synthetic five-App phone test",
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
                action=ACTIONS[app],
                target=URLS[app],
                text=TEXT,
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
        app=app,
        config=config,
        account=account,
        task=task,
        item=task["items"][0],
        metadata=metadata,
        monkeypatch=monkeypatch,
        root=tmp_path,
    )


def test_prepare_does_not_start_or_consume_attempt_or_permission(pilot):
    s = pilot
    result = s.service.prepare(s.item["id"], s.metadata)
    assert result["status"] == "succeeded" and result["result"]["social"]["discarded"]
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    assert s.store.one("SELECT COUNT(*) n FROM phone_social_authorizations")["n"] == 0
    assert s.engine.task(s.task["id"])["state"] != "active"
    assert not s.service.backend(s.account, "preflight", {"payload": s.item["payload"]})["allowed"]
    assert s.engine.account(s.account["id"])["execution_backend"] == "phone"


def test_disabled_submit_and_wrong_method_do_not_dispatch(pilot):
    s = pilot
    count = len(s.fake.requests)
    result = s.service.backend(s.account, "execute", {})
    assert result["confirmed_not_submitted"] and result["submission_started"] is False
    assert len(s.fake.requests) == count
    with pytest.raises(BackendError, match="MethodOrNodeMismatch"):
        s.service.check_method(
            {"execution_method": "api"}, s.service.config.bindings[s.account["id"]]
        )


def test_lost_create_is_read_only_and_immutable_after_recovery(pilot):
    s = pilot
    req = s.service.make_request(
        s.account["id"], "social_prepare", {**s.metadata, "target_url": URLS[s.app], "text": TEXT}
    )
    s.fake.lose_next = True
    assert s.service.dispatch(req, "lost-create", s.account["id"])["status"] == "unknown"
    creates = len([r for r in s.fake.requests if r.method == "POST"])
    assert s.service.dispatch(req, "lost-create", s.account["id"])["status"] == "succeeded"
    assert len([r for r in s.fake.requests if r.method == "POST"]) == creates
    with pytest.raises(Conflict, match="ImmutableRequestChanged"):
        s.service.dispatch(
            req.model_copy(update={"deadline": req.deadline + 1}), "lost-create", s.account["id"]
        )


def test_registration_preserves_an_existing_other_connection(pilot):
    s = pilot
    original = read_profile(s.app, s.account["backend_label"])["identity"]
    save_profile(
        s.app,
        s.account["backend_label"],
        {"identity": original, "connection_type": "browser", "sentinel": True},
    )
    count = len(s.fake.requests)
    with pytest.raises(Conflict, match="ExistingConnectionPreserved"):
        s.service.register(s.account["id"], {**s.metadata, "target_url": URLS[s.app], "text": TEXT})
    assert read_profile(s.app, s.account["backend_label"])["sentinel"]
    assert count == len(s.fake.requests)


def test_worker_config_is_database_bound_and_opt_in(pilot):
    s = pilot
    s.monkeypatch.setenv("AP_PHONE_PRIVATE_DIR", str(s.root))
    s.monkeypatch.setattr("auto_promotion.phone_private.private_directory_is_safe", lambda _: True)
    s.monkeypatch.setenv("AP_PHONE_SOCIAL_CONFIG", s.service.worker_config())
    other = Engine(Store(s.root / "other.db"), AdapterRegistry(s.root))
    with pytest.raises(Conflict, match="DatabaseMismatch"):
        attach_worker(other)
    attach_worker(s.engine)
    assert not s.engine.adapters.phone_social.config.submit_enabled


def test_reregistration_preserves_private_phone_profile_fields(pilot):
    s = pilot
    previous = read_profile(s.app, s.account["backend_label"])
    previous["preserved_marker"] = "existing-private-setting"
    previous["phone_fleet"]["preserved_binding_marker"] = True
    save_profile(s.app, s.account["backend_label"], previous)
    s.service.register(s.account["id"], {**s.metadata, "target_url": URLS[s.app], "text": TEXT})
    after = read_profile(s.app, s.account["backend_label"])
    assert after["preserved_marker"] == "existing-private-setting"
    assert after["phone_fleet"]["preserved_binding_marker"] is True


def claim_original(s):
    s.service.config = s.service.config.model_copy(
        update={"submit_enabled": True, "authorized_task_ids": [s.task["id"]]}
    )
    s.service.prepare(s.item["id"], s.metadata)
    # Existing Telegram membership guard remains in force; a fixture represents a verified test membership.
    if s.app == "telegram":
        s.monkeypatch.setattr("auto_promotion.core.telegram_blocker", lambda *a, **k: None)
    s.engine.start_task(s.task["id"], source="synthetic_test_explicit_start")
    queue = JobQueue(s.engine)
    queue.sync(s.task["id"])
    job = queue.claim(
        s.app + ".phone", "synthetic-worker", lease_seconds=180, reference_id=s.item["id"]
    )
    assert job
    context, _ = TaskChannelRouter(s.engine).prepare(job)
    with job_scope(job), s.engine.adapters.execution_job(context):
        claim = s.engine._claim(s.item["id"], {"allowed": True})
    assert claim
    return claim, context


def test_original_queue_single_permission_and_native_receipt_finish_once(pilot):
    s = pilot
    claim, context = claim_original(s)
    s.fake.authorize = lambda body: s.service.authorize(
        body, "synthetic-runtime-token-of-sufficient-length"
    )
    result = s.service.execute(s.engine.account(s.account["id"]), claim, context)
    assert result["verified"] and result["external_reference"]
    s.engine.finish(claim, result)
    assert (
        s.store.one("SELECT status FROM items WHERE id=?", (s.item["id"],))["status"] == "confirmed"
    )
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 1
    row = s.store.one("SELECT * FROM phone_social_authorizations")
    body = {
        "authorization_id": row["id"],
        "request_sha256": row["request_hash"],
        "native_command_id": row["native_id"],
        "execution_id": row["execution_id"],
        "device_id": row["device_id"],
    }
    with pytest.raises(Conflict, match="Fenced"):
        s.service.authorize(body, "synthetic-runtime-token-of-sufficient-length")
    creates = len([r for r in s.fake.requests if r.method == "POST"])
    assert s.service.verify(s.engine.account(s.account["id"]), claim)["verified"]
    assert len([r for r in s.fake.requests if r.method == "POST"]) == creates
    if s.app in {"instagram", "x"}:
        field = "reply_id" if s.app == "x" else "comment_id"
        # A previous sender stored only the verified link. Read its original native receipt once.
        with s.store.connect(write=True) as db:
            db.execute("UPDATE items SET result=json_remove(result,?,'$.publish_id') WHERE id=?", ("$." + field, s.item["id"]))
        assert s.engine.reconcile(s.item["id"])[field] == result[field]
        assert s.engine.reconcile(s.item["id"])[field] == result[field]
        assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 1
        assert len([r for r in s.fake.requests if r.method == "POST"]) == creates


def test_global_stop_blocks_final_permission_and_retains_unused_grant(pilot):
    s = pilot
    claim, context = claim_original(s)
    s.engine.set_stop(True)
    s.fake.authorize = lambda body: s.service.authorize(
        body, "synthetic-runtime-token-of-sufficient-length"
    )
    with pytest.raises((Conflict, BackendError), match="Stop|stop"):
        s.service.execute(s.engine.account(s.account["id"]), claim, context)
    assert (
        s.store.one("SELECT COUNT(*) n FROM phone_social_authorizations WHERE used_at IS NOT NULL")[
            "n"
        ]
        == 0
    )


def test_preparation_lookup_retains_original_beyond_thousand_other_operations(pilot):
    s = pilot
    s.service.prepare(s.item["id"], s.metadata)
    original, request, _ = s.service.preparation(
        s.account["id"], s.item["payload"], item_id=s.item["id"], task_id=s.task["id"]
    )
    assert original
    with s.store.connect(write=True) as db:
        for index in range(1001):
            other = {
                **request,
                "business_task_id": "other-task-" + str(index),
                "social": {**request["social"], "text": "other immutable draft " + str(index)},
            }
            db.execute(
                "INSERT INTO phone_social_operations(id,account_id,item_id,kind,request,phase,result,created_at) VALUES(?,?,?,'social_prepare',?,'linked','{}',?)",
                (
                    "synthetic-unrelated-" + str(index),
                    s.account["id"],
                    "other-item",
                    json.dumps(other),
                    time.time() + index,
                ),
            )
    selected, _, _ = s.service.preparation(
        s.account["id"], s.item["payload"], item_id=s.item["id"], task_id=s.task["id"]
    )
    assert selected["id"] == original["id"]
    assert (
        s.service.preparation(
            s.account["id"], s.item["payload"], item_id="wrong-item", task_id=s.task["id"]
        )[0]
        is None
    )


def test_round_stop_between_claim_check_and_native_grant_is_fenced(pilot):
    import auto_promotion.phone_social as module
    from auto_promotion.phone_rounds import PhoneRounds

    s = pilot
    claim, context = claim_original(s)
    rounds = PhoneRounds(s.engine)
    now = time.time()
    task = s.store.one("SELECT manifest_hash FROM tasks WHERE id=?", (s.task["id"],))
    with s.store.connect(write=True) as db:
        # A single member is sufficient to exercise the original final-grant transaction.
        db.execute(
            "INSERT INTO phone_rounds(id,request_key,manifest_hash,state,created_at,started_at,submit_deadline,deadline) VALUES('synthetic-race','synthetic-race','fixture','running',?,?,?,?)",
            (now, now, now + 480, now + 600),
        )
        db.execute(
            "INSERT INTO phone_round_members(round_id,task_id,item_id,platform,position,payload_hash,manifest_hash,state) VALUES('synthetic-race',?,?,?,0,?,?,'queued')",
            (s.task["id"], s.item["id"], s.app, s.item["payload_hash"], task["manifest_hash"]),
        )
    original = module.verify_claim

    def authorize(body):
        def stop_after_read(request):
            original(request)
            rounds.stop("synthetic-race")

        with s.monkeypatch.context() as patch:
            patch.setattr(module, "verify_claim", stop_after_read)
            return s.service.authorize(body, "synthetic-runtime-token-of-sufficient-length")

    s.fake.authorize = authorize
    with pytest.raises(Conflict, match="PhoneRoundNotRunning"):
        s.service.execute(s.engine.account(s.account["id"]), claim, context)
    grant = s.store.one("SELECT * FROM phone_social_authorizations")
    assert grant and grant["used_at"] is None


def test_protocol_mismatch_blocks_before_new_intent_or_post(pilot):
    s = pilot
    original = s.service.request

    def request(method, path, body=None, key=None):
        if path == "/capabilities":
            return {"version": "fleet.internal/0.1", "operations": {"observe": "supported"}}
        return original(method, path, body, key)

    s.service.request = request
    before = len([r for r in s.fake.requests if r.method == "POST"])
    request = s.service.make_request(
        s.account["id"], "social_prepare", {**s.metadata, "target_url": URLS[s.app], "text": TEXT}
    )
    with pytest.raises(BackendError, match="ProtocolMismatch"):
        s.service.dispatch(request, "blocked-version", s.account["id"])
    assert len([r for r in s.fake.requests if r.method == "POST"]) == before
    assert not s.store.one("SELECT id FROM phone_social_operations WHERE id='blocked-version'")


def test_legacy_social_receipt_only_queries_original_execution(pilot):
    s = pilot
    claim, context = claim_original(s)
    s.service.execute(s.engine.account(s.account["id"]), claim, context)
    row = s.store.one("SELECT * FROM phone_social_operations WHERE kind='social_submit'")
    request = json.loads(row["request"])
    request["version"] = "fleet.internal/0.1"
    s.fake.snapshots[row["execution_id"]]["request"] = request
    with s.store.connect(write=True) as db:
        db.execute(
            "UPDATE phone_social_operations SET request=? WHERE id=?",
            (json.dumps(request), row["id"]),
        )
    before = len([r for r in s.fake.requests if r.method == "POST"])
    verified = s.service.verify(s.engine.account(s.account["id"]), claim)
    assert verified["legacy_read_only"] and not verified["verified"]
    assert verified["original_execution_id"] == row["execution_id"]
    assert len([r for r in s.fake.requests if r.method == "POST"]) == before


def test_legacy_identity_observation_is_not_fresh_after_upgrade(pilot):
    s = pilot
    assert s.service.fresh(s.engine.account(s.account["id"]))
    row = s.store.one("SELECT * FROM phone_social_accounts WHERE account_id=?", (s.account["id"],))
    observation = json.loads(row["observation"])
    observation["request"]["version"] = "fleet.internal/0.1"
    with s.store.connect(write=True) as db:
        db.execute(
            "UPDATE phone_social_accounts SET observation=? WHERE account_id=?",
            (json.dumps(observation), s.account["id"]),
        )
    assert not s.service.fresh(s.engine.account(s.account["id"]))


def dynamic_pilot(s):
    s.service.config = s.service.config.model_copy(
        update={
            "submit_enabled": True,
            "dynamic_task_authorization_enabled": True,
            "authorized_task_ids": [],
            "single_pass_task_ids": [],
        }
    )
    from auto_promotion.temporal_tasks import commission_dynamic_admission

    with s.monkeypatch.context() as patch:
        patch.setattr(s.engine, "clock", lambda: s.task["created_at"])
        commission_dynamic_admission(s.service)
    s.service.prepare(s.item["id"], s.metadata)
    if s.app == "telegram":
        s.monkeypatch.setattr("auto_promotion.core.telegram_blocker", lambda *a, **k: None)
    return s.service


def test_dynamic_phone_prepare_never_authorizes_execution(pilot):
    s = pilot
    service = dynamic_pilot(s)
    s.engine.prepare(s.task["id"])
    link = s.store.one("SELECT * FROM temporal_task_links WHERE task_id=?", (s.task["id"],))
    assert link["state"] == "registered" and link["requested_at"] is None
    assert not service.task_authorized(s.task["id"], s.account["id"])
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    assert s.store.one("SELECT COUNT(*) n FROM phone_social_authorizations")["n"] == 0
    assert s.engine.task(s.task["id"])["state"] == "draft"


def test_dynamic_phone_start_admits_original_queue_without_reloading(pilot):
    from auto_promotion.temporal_tasks import admit

    s = pilot
    service = dynamic_pilot(s)
    # Start directly, without a manual prepare, task-ID config edit or new service.
    s.engine.start_task(s.task["id"], source="synthetic_test_explicit_start")
    link = s.store.one("SELECT * FROM temporal_task_links WHERE task_id=?", (s.task["id"],))
    assert link["state"] == "requested" and link["requested_at"] is not None
    assert service is s.engine.adapters.phone_social
    assert service.config.authorized_task_ids == [] and service.config.single_pass_task_ids == []
    assert service.task_authorized(s.task["id"], s.account["id"])
    assert not service.task_authorized(s.task["id"], "unbound-other-account")
    queue = JobQueue(s.engine)
    queue.sync(s.task["id"])
    assert (
        queue.claim(s.app + ".phone", "synthetic-dynamic-worker", reference_id=s.item["id"]) is None
    )
    result = admit(s.engine, s.task["id"], link["manifest_hash"], link["workflow_id"])
    assert result["admitted"] is True
    assert queue.claim(s.app + ".phone", "synthetic-dynamic-worker", reference_id=s.item["id"])
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    assert s.store.one("SELECT COUNT(*) n FROM phone_social_authorizations")["n"] == 0


def test_dynamic_phone_authorization_reads_pause_expiry_and_manifest_changes(pilot):
    s = pilot
    service = dynamic_pilot(s)
    s.engine.start_task(s.task["id"], source="synthetic_test_explicit_start")
    assert service.task_authorized(s.task["id"], s.account["id"])
    with s.store.connect(write=True) as db:
        db.execute("UPDATE tasks SET state='paused' WHERE id=?", (s.task["id"],))
    assert not service.task_authorized(s.task["id"], s.account["id"])
    with s.store.connect(write=True) as db:
        db.execute("UPDATE tasks SET state='active' WHERE id=?", (s.task["id"],))
        db.execute(
            "UPDATE temporal_task_links SET manifest_hash=? WHERE task_id=?",
            ("f" * 64, s.task["id"]),
        )
    assert not service.task_authorized(s.task["id"], s.account["id"])


def test_dynamic_phone_never_imports_or_replays_old_unknown_task(pilot):
    from auto_promotion.temporal_tasks import auto_register_new_phone_task

    s = pilot
    s.engine.prepare(s.task["id"])
    with s.store.connect(write=True) as db:
        db.execute("UPDATE tasks SET state='active',phase='ready' WHERE id=?", (s.task["id"],))
        db.execute("UPDATE items SET status='unknown' WHERE id=?", (s.item["id"],))
    before = s.store.one("SELECT * FROM items WHERE id=?", (s.item["id"],))
    posts = sum(r.method == "POST" for r in s.fake.requests)
    s.service.config = s.service.config.model_copy(
        update={"dynamic_task_authorization_enabled": True}
    )
    assert auto_register_new_phone_task(s.engine, s.task["id"], freeze=True) is None
    assert not s.service.task_authorized(s.task["id"], s.account["id"])
    assert s.store.one("SELECT * FROM items WHERE id=?", (s.item["id"],)) == before
    assert sum(r.method == "POST" for r in s.fake.requests) == posts


def test_dynamic_phone_older_connected_client_still_hits_original_start_fence(pilot):
    s = pilot
    dynamic_pilot(s)
    s.monkeypatch.setattr(
        "auto_promotion.temporal_tasks.auto_register_new_phone_task", lambda *args, **kwargs: None
    )
    s.engine.start_task(s.task["id"], source="synthetic_older_client_explicit_start")
    link = s.store.one("SELECT * FROM temporal_task_links WHERE task_id=?", (s.task["id"],))
    assert (
        link["state"] == "requested"
        and link["manifest_hash"] == s.engine.task(s.task["id"])["manifest_hash"]
    )
    queue = JobQueue(s.engine)
    queue.sync(s.task["id"])
    assert queue.claim(s.app + ".phone", "older-client", reference_id=s.item["id"]) is None
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0


def test_dynamic_phone_cold_identity_refresh_precedes_freezing_new_task(pilot):
    s = pilot
    dynamic_pilot(s)
    with s.store.connect(write=True) as db:
        db.execute(
            "UPDATE phone_social_accounts SET observed_at=0 WHERE account_id=?", (s.account["id"],)
        )
    calls = []

    def refreshed(account_id, *, task_id=None):
        assert s.engine.task(s.task["id"])["manifest_hash"] is None
        calls.append(account_id)
        with s.store.connect(write=True) as db:
            db.execute(
                "UPDATE phone_social_accounts SET observed_at=? WHERE account_id=?",
                (time.time(), account_id),
            )

    s.monkeypatch.setattr(s.service, "ensure_identity", refreshed)
    s.engine.start_task(s.task["id"], source="synthetic_cold_explicit_start")
    assert calls == [s.account["id"]]
    assert (
        s.store.one("SELECT state FROM temporal_task_links WHERE task_id=?", (s.task["id"],))[
            "state"
        ]
        == "requested"
    )


@pytest.mark.parametrize("disabled", [False, True])
def test_phone_start_uses_native_proof_while_unrelated_health_lane_is_busy(pilot, disabled):
    from auto_promotion.dispatcher import AccountLanes

    s = pilot
    if disabled:
        with s.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (s.account["id"],))
    before = s.store.one("SELECT status,health FROM accounts WHERE id=?", (s.account["id"],))
    s.monkeypatch.setattr(
        s.engine,
        "check_account",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("duplicate desktop health probe")),
    )
    with AccountLanes(s.store).acquire(s.account["id"]) as existing_owner:
        if disabled:
            with pytest.raises(Conflict):
                s.engine.start_task(s.task["id"], source="synthetic_explicit_start")
        else:
            assert (
                s.engine.start_task(s.task["id"], source="synthetic_explicit_start")["state"]
                == "active"
            )
        assert (
            s.store.one("SELECT owner FROM account_lanes WHERE account_id=?", (s.account["id"],))[
                "owner"
            ]
            == existing_owner
        )
    assert before == s.store.one(
        "SELECT status,health FROM accounts WHERE id=?", (s.account["id"],)
    )

@pytest.mark.parametrize("bound", [False, True])
def test_explicit_phone_route_uses_only_the_verified_shared_x_account(monkeypatch, tmp_path, bound):
    from types import SimpleNamespace
    from auto_promotion.adapters import AdapterRegistry
    from auto_promotion import routing

    registry = AdapterRegistry(tmp_path)
    monkeypatch.setattr(routing, "route_for", lambda account: SimpleNamespace(transport="edge"))
    registry.phone_social = SimpleNamespace(shared_account_binding=lambda account: bound,
        backend=lambda account, operation, payload, job: {"healthy": True, "reason": "verified_phone_binding"})
    result = registry.call_phone({"id": "test-x", "platform": "x"}, "health", {})
    assert result["healthy"] is bound
    assert result["reason"] == ("verified_phone_binding" if bound else "PhoneFleetPreparationOnly")
