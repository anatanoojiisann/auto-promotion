"""Isolated executable acceptance fixture. Never opens an account profile or network socket."""

import tempfile
from pathlib import Path

from .adapters import AdapterRegistry
from .core import Engine
from .interaction_connectors import InteractionConnectors
from .interaction_models import CandidateAction, ConfigSave, InteractionConfig
from .interactions import Interactions
from .models import AccountInput
from .queue_store import JobQueue, job_scope
from .store import Store


class SimulationConnectors(InteractionConnectors):
    def __init__(self, engine):
        super().__init__(engine)
        self.sources = []
        self.reads = []
        self.media_orders = 0
        self.notices = []
        self.fail_classify = False
        self.notify_result = {"status": "sent", "message_id": "simulation-notice-1"}

    def discover(self, account, query, lower, upper, cursor, config):
        assert account["mode"] == "simulation"
        self.reads.append({"query": query, "lower": lower, "upper": upper, "cursor": cursor})
        return {
            "candidates": [s for s in self.sources if lower <= s["source_event_at"] <= upper],
            "coverage_complete": True,
            "next_cursor": None,
            "actual_cost": 0,
        }

    def classify(self, source, config, account):
        if self.fail_classify:
            raise RuntimeError("SimulatedClassifierUnavailable")
        return super().classify(source, config, account)

    def notify(self, notice, idempotency_key):
        self.notices.append(
            {"id": notice["id"], "revision": notice["revision"], "key": idempotency_key}
        )
        return self.notify_result


class OfflineRegistry(AdapterRegistry):
    offline_simulation = True


def source(event, priority, now, *, kind="explicit_unresolved_need", media="none"):
    text = "Need help creating a coherent video scene " + str(event)
    return {
        "id": str(event),
        "url": "https://x.com/example/status/" + str(event),
        "text": text,
        "author_id": "synthetic-user-" + str(event),
        "source_event_at": now,
        "context_status": "complete",
        "context": "",
        "simulation_classification": {
            "priority": priority,
            "priority_reason": "Synthetic explicit creator need",
            "intent_evidence": [text],
            "product_fit": "Verified synthetic scene workflow",
            "verified_fact_ids": ["demo-fact"],
            "contribution": "Try locking the camera and keeping the subject description consistent between shots.",
            "language": "en",
            "target_account_fit": True,
            "context_status": "complete",
            "opportunity_type": kind,
            "recommended_media": media,
            "quality_status": "passed",
            "value": 0.8,
        },
    }


def drain_sender(engine):
    """Consume only the simulation pool through the existing durable sender boundary."""
    queue = JobQueue(engine)
    queue.sync()
    outcomes = []
    for _ in range(20):
        job = queue.claim("simulation.x", "simulation-acceptance")
        if not job:
            break
        with job_scope(job), engine.adapters.execution_job(job):
            outcomes.append(engine.run_reference_job(job))
        queue.settle(job)
    return outcomes


def simulate():
    with tempfile.TemporaryDirectory(prefix="ap-hf-simulation-") as folder:
        now = [1792544400.0]
        engine = Engine(
            Store(Path(folder) / "acceptance.db"),
            OfflineRegistry(Path(folder)),
            clock=lambda: now[0],
        )
        account = engine.add_account(
            AccountInput(
                platform="x",
                label="HF acceptance simulation",
                backend_label="hf-acceptance",
                mode="simulation",
                min_interval=0,
                daily_limit=20,
                hourly_limit=20,
            )
        )
        engine.check_account(account["id"])
        engine.set_stop(False)
        io = SimulationConnectors(engine)
        service = Interactions(engine, connectors=io)
        cfg = InteractionConfig(
            enabled=True,
            run_mode="live",
            project_id="acceptance-simulation",
            account_ids=[account["id"]],
            queries=["synthetic"],
            facts=[
                {
                    "id": "demo-fact",
                    "text": "A synthetic scene workflow",
                    "source": "simulation://verified-fact",
                    "verified_at": now[0] - 100,
                    "valid_until": now[0] + 604800,
                }
            ],
        )
        policy = service.save(ConfigSave(config=cfg))
        io.sources = [
            source(1001, "P0", now[0]),
            source(1002, "P1", now[0]),
            source(1003, "P2", now[0], media="video"),
            source(1004, "P3", now[0]),
        ]
        service.trigger(policy["id"], manual=True)
        for stage in ("scan", "classify", "generate", "publish"):
            service.run_stage(stage)
        drain_sender(engine)
        service.schedule()
        candidates = service.candidates(policy["id"])
        checks = {
            "P0/P1 自动进入原发送队列并得到模拟回执": sum(
                c["state"] == "SIMULATED" for c in candidates
            )
            == 2,
            "未选 P2 不生成付费媒体、不发布": next(c for c in candidates if c["priority"] == "P2")[
                "state"
            ]
            == "REVIEW_PENDING"
            and io.media_orders == 0,
            "P3 在空轮条件之前保持候补": next(c for c in candidates if c["priority"] == "P3")[
                "state"
            ]
            == "DISCOVERED",
        }
        for _ in range(5):
            now[0] += 600
            service.trigger(policy["id"], manual=True)
            service.run_stage("scan")
            service.run_stage("classify")
            service.schedule()
        checks["连续五轮完整空扫描后 P3 进入人工列表"] = (
            next(c for c in service.candidates(policy["id"]) if c["priority"] == "P3")["state"]
            == "REVIEW_PENDING"
        )
        batch = service.status(policy["id"])["batches"][0]
        now[0] = batch["due_at"] - 60
        io.sources = [source(1005, "P2", now[0])]
        service.trigger(policy["id"], manual=True)
        service.run_stage("scan")
        service.run_stage("classify")
        restarted = Interactions(engine, connectors=io)
        checks["新增 P2 与服务重建不重置首批五小时"] = (
            restarted.status(policy["id"])["batches"][0]["due_at"] == batch["due_at"]
        )
        now[0] += 60
        restarted.schedule()
        restarted.run_stage("notify")
        notices = restarted.status(policy["id"])["notices"]
        checks["五小时摘要实际通过持久通知发送一次"] = (
            len(notices) == 1 and notices[0]["state"] == "sent"
        )
        chosen = next(
            c for c in restarted.candidates(policy["id"]) if c["source_event_id"] == "1003"
        )
        restarted.action(
            CandidateAction(action="adopt", versions={chosen["id"]: chosen["version"]})
        )
        restarted.run_stage("generate")
        restarted.run_stage("publish")
        drain_sender(engine)
        restarted.sync_results()
        checks["只采用一条时其他 P2 保持待选"] = (
            next(c for c in restarted.candidates(policy["id"]) if c["source_event_id"] == "1005")[
                "state"
            ]
            == "REVIEW_PENDING"
        )
        checks["缺少媒体连接时明确记录并完成文字模拟"] = (
            restarted.candidate(chosen["id"])["state"] == "SIMULATED"
        )
        evidence = {
            "runs": [
                {k: r[k] for k in ("seq", "state", "new_high", "fetched", "duplicates")}
                for r in restarted.status(policy["id"])["runs"]
            ],
            "candidates": [
                {k: c[k] for k in ("source_event_id", "priority", "state", "external_reference")}
                for c in restarted.candidates(policy["id"])
            ],
            "notices": [{k: n[k] for k in ("state", "revision", "attempts")} for n in notices],
            "batch_opened_at": batch["opened_at"],
            "batch_due_at": batch["due_at"],
        }
        return {
            "passed": all(checks.values()),
            "checks": checks,
            "external_calls": 0,
            "database": "isolated_temporary_database",
            "evidence": evidence,
        }
