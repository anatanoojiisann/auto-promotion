"""Durable high-frequency discovery and routing on the existing task/publishing engine."""

import json
import math
import random
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .core import Conflict, NotFound
from .interaction_connectors import InteractionConnectors, source_hash
from .interaction_joint import JointInteractions
from .interaction_models import Classification, InteractionConfig
from .interaction_queue import InteractionQueue, LostLease
from .interaction_schedule import InteractionSchedule
from .models import ItemInput, TaskInput, canonical, digest
from .store import Store, decode


def uid(prefix):
    return prefix + uuid.uuid4().hex[:24]


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def within_hours(at, zone, hours):
    from zoneinfo import ZoneInfo

    start, end = hours
    hour = datetime.fromtimestamp(at, ZoneInfo(zone)).hour
    return start == end or (start <= hour < end if start < end else hour >= start or hour < end)


class Interactions(InteractionSchedule, JointInteractions):
    stages = (
        "scan",
        "readonly_scan",
        "classify",
        "readonly_classify",
        "assign",
        "generate",
        "media",
        "publish",
        "notify",
        "verify",
        "observe",
    )

    def __init__(self, engine, *, connectors=None, worker_available=None, randint=None):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock
        self.io = connectors or InteractionConnectors(engine)
        self.randint = randint or random.SystemRandom().randint
        self.queue = InteractionQueue(self.store, self.clock)
        self.worker_available = worker_available or (lambda: False)
        self.draw_delay = random.SystemRandom().randint
        self._one_shot = False

    @contextmanager
    def one_shot_execution(self):
        """The reviewed CLI actually consumes stages here; this is not daemon health."""
        previous = self._one_shot
        self._one_shot = True
        try:
            yield
        finally:
            self._one_shot = previous

    def policy(self, policy_id):
        row = self.store.one("SELECT * FROM hf_policies WHERE id=?", (policy_id,))
        if not row:
            raise NotFound("高频互动策略不存在")
        policy = decode(row, "config")
        mapping = self.store.one(
            "SELECT scope_id FROM hf_dedupe_projects WHERE project_id=?",
            (policy["config"].get("dedupe_scope_id") or policy["project_id"],),
        )
        if mapping and mapping["scope_id"] != policy["project_id"]:
            policy["config"]["dedupe_scope_id"] = mapping["scope_id"]
        return policy

    def candidate(self, candidate_id):
        row = self.store.one("SELECT * FROM hf_candidates WHERE id=?", (candidate_id,))
        if not row:
            raise NotFound("候选不存在")
        candidate = decode(row, "source", "classification", "media_plan", "matched_product_ids")
        timing = self.store.one(
            "SELECT * FROM hf_enqueue_times WHERE candidate_id=?", (candidate_id,)
        )
        candidate["not_before"] = timing["not_before"] if timing else None
        candidate["discoveries"] = [
            decode(r, "product_ids")
            for r in self.store.rows(
                "SELECT * FROM hf_discoveries WHERE candidate_id=? ORDER BY discovered_at",
                (candidate_id,),
            )
        ]
        candidate["generations"] = self.store.rows(
            "SELECT id,version,kind,state,provider,provider_job_id,source,real_product_output,started_at,finished_at,estimated_cost,actual_cost FROM hf_generation WHERE candidate_id=? ORDER BY started_at",
            (candidate_id,),
        )
        return candidate

    def list(self):
        return [
            self.policy(r["id"])
            for r in self.store.rows("SELECT id FROM hf_policies ORDER BY created_at")
        ]

    def save(self, value):
        config = value.config.model_copy(deep=True)
        self._resolve_accounts(config)
        config = InteractionConfig.model_validate(config.model_dump())
        accounts = [self.engine.account(a) for a in config.account_ids]
        readiness = self.readiness(config, accounts)
        if config.enabled and config.run_mode == "live" and readiness["blockers"]:
            raise Conflict("interaction_not_ready:" + ",".join(readiness["blockers"]))
        policy_id = "HF-" + digest([config.project_id, config.platform, config.policy_key])[:24]
        now = self.clock()
        with self.store.connect(write=True) as db:
            from .interaction_intents import scope_id

            effective_scope = scope_id(db, config.scope_id)
            if effective_scope != config.project_id:
                config.dedupe_scope_id = effective_scope
            self._register_scope(db, config)
            row = db.execute("SELECT * FROM hf_policies WHERE id=?", (policy_id,)).fetchone()
            if value.expected_version != (row["version"] if row else 0):
                raise Conflict("配置已改变，请刷新后保存")
            version = row["version"] + 1 if row else 1
            epoch = row["count_epoch"] if row else 1
            reset = False
            if row:
                old = InteractionConfig.model_validate_json(row["config"]).model_dump()
                keys = (
                    "queries",
                    "audience",
                    "languages",
                    "classifier_version",
                    "product_knowledge_version",
                    "facts",
                    "account_ids",
                    "products",
                    "strategy_profile",
                )
                reset = any(
                    old.get(k, config.model_dump()[k]) != config.model_dump()[k] for k in keys
                )
                reset = reset or old["scan"].get("source", "x_api") != config.scan.source
                epoch += int(reset)
                db.execute(
                    "UPDATE hf_policies SET config=?,version=?,count_epoch=?,empty_streak=CASE WHEN ? THEN 0 ELSE empty_streak END,"
                    "next_scan_at=next_scan_at WHERE id=?",
                    (
                        canonical(config.model_dump()),
                        version,
                        epoch,
                        reset,
                        policy_id,
                    ),
                )
                if reset:
                    db.execute(
                        "UPDATE hf_assessments SET state='superseded' WHERE policy_id=? AND state='assessed'",
                        (policy_id,),
                    )
                if value.apply_timing_to_open_batches:
                    db.execute(
                        "UPDATE hf_batches SET due_at=opened_at+? WHERE policy_id=? AND state='open'",
                        (config.review.batch_wait_seconds, policy_id),
                    )
                if old["run_mode"] == "dry_run" and config.run_mode == "live":
                    db.execute(
                        "UPDATE hf_candidates SET state='DISCOVERED',version=version+1,selected_version=NULL,external_reference=NULL WHERE policy_id=? AND state='SIMULATED' AND external_reference LIKE 'simulation://dry-run/%'",
                        (policy_id,),
                    )
                if reset:
                    old_products = {p["product_id"]: p for p in old.get("products", [])}
                    new_products = {p.product_id: p.model_dump() for p in config.products}
                    changed_products = {
                        p
                        for p in old_products.keys() | new_products.keys()
                        if old_products.get(p) != new_products.get(p)
                    }
                    global_change = any(
                        old.get(k) != config.model_dump()[k]
                        for k in (
                            "audience",
                            "languages",
                            "classifier_version",
                            "product_knowledge_version",
                            "facts",
                            "strategy_profile",
                        )
                    )
                    for candidate in db.execute(
                        "SELECT * FROM hf_candidates WHERE policy_id=? AND state NOT IN ('CLOSED','PUBLISHED','SIMULATED','PUBLISHING','VERIFYING')",
                        (policy_id,),
                    ).fetchall():
                        if config.joint:
                            related = (
                                global_change
                                or not candidate["priority"]
                                or bool(
                                    set(json.loads(candidate["matched_product_ids"]))
                                    & changed_products
                                )
                            )
                            if not related:
                                db.execute(
                                    "UPDATE hf_candidates SET config_version=? WHERE id=?",
                                    (version, candidate["id"]),
                                )
                                continue
                            item = (
                                db.execute(
                                    "SELECT attempt_id FROM items WHERE id=?",
                                    (candidate["item_id"],),
                                ).fetchone()
                                if candidate["item_id"]
                                else None
                            )
                            if item and item[0]:
                                continue
                            self._retire_unsubmitted(db, candidate)
                            db.execute(
                                "UPDATE hf_candidates SET version=version+1,selected_version=CASE WHEN selected_version=version AND draft_origin='human' THEN version+1 ELSE NULL END,draft=CASE WHEN draft_origin='human' THEN draft ELSE '' END,media_plan='{}',task_id=NULL,item_id=NULL,state='DISCOVERED',classification_status='pending_context',block_reason='classification_pending' WHERE id=?",
                                (candidate["id"],),
                            )
                        self.queue.put(
                            db,
                            "readonly_classify"
                            if config.run_mode == "discovery_only"
                            else "classify",
                            "current|" + candidate["id"] + "|" + str(version),
                        )
                else:
                    db.execute(
                        "UPDATE hf_candidates SET config_version=? WHERE policy_id=? AND state NOT IN ('PUBLISHED','SIMULATED','CLOSED','PUBLISHING','VERIFYING')",
                        (version, policy_id),
                    )
            else:
                db.execute(
                    "INSERT INTO hf_policies(id,project_id,platform,policy_key,version,count_epoch,config,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        policy_id,
                        config.project_id,
                        config.platform,
                        config.policy_key,
                        version,
                        epoch,
                        canonical(config.model_dump()),
                        now,
                    ),
                )
            db.execute(
                "INSERT INTO hf_config_versions VALUES(?,?,?,?)",
                (policy_id, version, canonical(config.model_dump()), now),
            )
            Store.audit(
                db,
                "interaction.policy_saved",
                {
                    "policy_id": policy_id,
                    "version": version,
                    "count_epoch": epoch,
                    "enabled": config.enabled,
                    "run_mode": config.run_mode,
                },
                now=now,
            )
        return self.status(policy_id)

    def readiness(self, config, accounts=None):
        accounts = (
            accounts
            if accounts is not None
            else [self.engine.account(a) for a in config.account_ids]
        )
        synthetic = bool(accounts) and all(a["mode"] == "simulation" for a in accounts)
        caps = [self.io.capabilities(a) for a in accounts]
        if config.joint and config.run_mode == "discovery_only":
            blockers = []
            for account, cap in zip(accounts, caps):
                if account["disabled"] or account["status"] != "healthy":
                    blockers.append("account_unavailable")
                if cap.get("transport") not in {"browser", "x_api", "simulation"}:
                    blockers.append("scan_source_unavailable")
                elif cap.get("transport") == "x_api" and cap.get("blockers"):
                    blockers.extend(cap["blockers"])
            if not accounts:
                blockers.append("accounts_missing")
            return {
                "ready": not blockers,
                "blockers": list(dict.fromkeys(blockers)),
                "source": "browser_read_only",
                "coverage": "partial_browser_only",
                "worker": "external_observer",
                "model_connectivity": "unused",
                "simulation": synthetic,
                "publishing": "manual_task_start_required",
                "accounts": [
                    {
                        "account_id": account["id"],
                        "transport": cap.get("transport"),
                        "blockers": cap.get("blockers", []),
                    }
                    for account, cap in zip(accounts, caps)
                ],
                "retry_at": None,
            }
        blockers = []
        if not accounts:
            blockers.append("accounts_missing")
        if not synthetic and not all(c.get("model", {}).get("configured") is True for c in caps):
            blockers.append("model_connection_unavailable")
        if not synthetic:
            for cap in caps:
                error = cap.get("model", {}).get("configuration_error")
                if (
                    error in {"model_endpoint_invalid", "model_identifier_missing"}
                    and error not in blockers
                ):
                    blockers.append(error)
        try:
            worker = (
                "one_shot"
                if self._one_shot
                else ("running" if self.worker_available() is True else "unavailable")
            )
        except Exception:
            worker = "unverified"
        if not synthetic and worker not in {"running", "one_shot"}:
            blockers.append("worker_" + worker)
        choices = []
        retry_at = 0
        for account, cap in zip(accounts, caps):
            reasons = list(cap.get("blockers", []))
            if account["mode"] != "simulation" and cap.get("transport") != "x_api":
                if "x_api_account_required" not in reasons:
                    reasons.append("x_api_account_required")
            choices.append(
                {
                    "account_id": account["id"],
                    "transport": cap.get("transport"),
                    "blockers": reasons,
                }
            )
            if config.scan.source == "x_api":
                for reason in reasons:
                    if reason not in blockers:
                        blockers.append(reason)
        if config.scan.source == "x_api" and not synthetic:
            if any(c.get("transport") != "x_api" for c in caps):
                blockers.append("scan_source_unavailable")
            if accounts:
                retry_at = self._read_after(accounts[0]["id"])
                if retry_at > self.clock():
                    blockers.append("x_api_read_cooldown")
        return {
            "ready": not blockers,
            "blockers": blockers,
            "source": config.scan.source,
            "coverage": "partial_manual_import"
            if config.scan.source == "imported_results"
            else "requires_complete_api_scan",
            "worker": worker,
            "model_connectivity": "unverified" if not synthetic else "simulation",
            "simulation": synthetic,
            "publishing": "requires_target_eligibility",
            "accounts": choices,
            "retry_at": retry_at if retry_at > self.clock() else None,
        }

    def account_options(self):
        """Safe local selectors for the settings UI; no credential/identity export."""
        return [
            {
                "id": a["id"],
                "label": a["label"],
                "mode": a["mode"],
                "capabilities": self.io.capabilities(a),
            }
            for a in self.engine.accounts()
            if a["platform"] == "x"
        ]

    def _read_after(self, account_id):
        row = self.store.one(
            "SELECT value FROM meta WHERE key=?", ("hf_x_read_after:" + account_id,)
        )
        return float(row["value"]) if row else 0

    def status(self, policy_id):
        policy = self.policy(policy_id)
        config = InteractionConfig.model_validate(policy["config"])
        accounts = [
            self.engine.account(a)
            for a in dict.fromkeys(
                config.account_ids + [a for p in config.products for a in p.execution_account_ids]
            )
        ]
        effective = []
        for account in accounts:
            effective.append(
                {
                    "account_id": account["id"],
                    "label": account["label"],
                    "mode": account["mode"],
                    "min_interval": max(
                        account["min_interval"], config.publishing.min_account_interval_seconds or 0
                    ),
                    "hourly_limit": min(
                        account["hourly_limit"],
                        config.publishing.max_per_account_hour or account["hourly_limit"],
                    ),
                    "daily_limit": min(
                        account["daily_limit"],
                        config.publishing.max_per_account_day or account["daily_limit"],
                    ),
                    "capabilities": self.io.capabilities(account),
                }
            )
        warnings = []
        if not config.review.expiry_alert_enabled and config.review.batch_wait_seconds >= min(
            config.ttl.model_dump().values()
        ):
            warnings.append("部分候选会在首次摘要前失效；临期提醒已关闭")
        from .interaction_observer import account_review_status, fallback_status

        policy.update(
            {
                "effective_accounts": effective,
                "readiness": self.readiness(config, accounts),
                "delivery_summary": self.delivery_summary(policy_id),
                "readonly_fallback": (
                    fallback_status(self.store, policy_id, now=self.clock())
                    if config.joint
                    and config.run_mode == "discovery_only"
                    and config.policy_key == "aigram_aurax_joint"
                    else None
                ),
                "account_review": (
                    account_review_status(self.store, policy_id)
                    if config.run_mode == "discovery_only"
                    and config.policy_key.endswith("_observer")
                    else None
                ),
                "routing": {
                    "P0": "auto",
                    "P1": "auto",
                    "P2": "record_only" if config.joint else "manual_review",
                    "P3": "record_only" if config.joint else "manual_review",
                },
                "fallback_after_seconds": config.scan.interval_seconds
                * config.fallback.empty_rounds_required,
                "notification_channel": self.channel(config),
                "media": self.io.media_status(),
                "warnings": warnings,
                "counts": self.store.rows(
                    "SELECT c.priority,c.state,count(DISTINCT c.id) count FROM hf_candidates c LEFT JOIN hf_discoveries d ON d.candidate_id=c.id WHERE c.policy_id=? OR d.policy_id=? GROUP BY c.priority,c.state",
                    (policy_id, policy_id),
                ),
                "block_reasons": self.store.rows(
                    "SELECT c.block_reason,count(DISTINCT c.id) count FROM hf_candidates c LEFT JOIN hf_discoveries d ON d.candidate_id=c.id WHERE (c.policy_id=? OR d.policy_id=?) AND c.block_reason IS NOT NULL GROUP BY c.block_reason",
                    (policy_id, policy_id),
                ),
                "runs": self.store.rows(
                    "SELECT * FROM hf_scan_runs WHERE policy_id=? ORDER BY seq DESC LIMIT 30",
                    (policy_id,),
                ),
                "batches": self.store.rows(
                    "SELECT * FROM hf_batches WHERE policy_id=? ORDER BY opened_at DESC LIMIT 30",
                    (policy_id,),
                ),
                "notices": [
                    decode(r, "snapshot")
                    for r in self.store.rows(
                        "SELECT * FROM hf_notices WHERE policy_id=? ORDER BY created_at DESC LIMIT 50",
                        (policy_id,),
                    )
                ],
                "usage": self.store.rows(
                    "SELECT kind,count(*) calls,sum(duration_ms) duration_ms,CASE WHEN count(estimated_cost)=count(*) THEN sum(estimated_cost) END estimated_cost,CASE WHEN count(actual_cost)=count(*) THEN sum(actual_cost) END actual_cost FROM hf_usage WHERE project_id=? GROUP BY kind",
                    (config.project_id,),
                ),
                "metrics": self.metrics(policy_id),
                "global_stop": self.store.one("SELECT value FROM meta WHERE key='global_stop'")[
                    "value"
                ]
                != "0",
            }
        )
        # Run configs are accessible through version history, not duplicated on every refresh.
        for run in policy["runs"]:
            run.pop("config", None)
            run["queries"] = self.store.rows(
                "SELECT * FROM hf_query_progress WHERE run_id=?", (run["id"],)
            )
            run["classification_counts"] = self.store.rows(
                "SELECT state,priority,count(*) count FROM hf_scan_candidates WHERE run_id=? GROUP BY state,priority",
                (run["id"],),
            )
            run["product_coverage"] = [
                {
                    "product_id": p.product_id,
                    "partitions": sum(
                        p.product_id in json.loads(q["product_ids"]) for q in run["queries"]
                    ),
                    "complete": all(
                        q["complete"]
                        for q in run["queries"]
                        if p.product_id in json.loads(q["product_ids"])
                    ),
                    "errors": [
                        q["error"]
                        for q in run["queries"]
                        if p.product_id in json.loads(q["product_ids"]) and q["error"]
                    ],
                }
                for p in config.products
                if p.enabled
            ]
        policy["rotation"] = {
            "order": config.account_ids,
            "cursor": policy["rotation_cursor"],
            "current_search_account_id": next(
                (
                    r["search_account_id"]
                    for r in policy["runs"]
                    if r["id"] == policy["active_run_id"]
                ),
                None,
            ),
            "last_started_account_id": policy["last_search_account_id"],
            "previous_search_account_id": policy["runs"][1]["search_account_id"]
            if len(policy["runs"]) > 1
            else None,
            "require_different": config.joint
            and config.search_accounts.require_different_from_previous,
            "pool_version": policy["account_pool_version"],
        }
        return policy

    def delivery_summary(self, policy_id):
        from .delivery_summary import summarize

        rows = self.store.rows(
            "SELECT c.state,c.closed_reason,i.status,i.external_reference,i.result,t.mode FROM hf_candidates c "
            "LEFT JOIN items i ON i.id=c.item_id LEFT JOIN tasks t ON t.id=i.task_id WHERE c.policy_id=?",
            (policy_id,),
        )
        result = summarize([], "live")
        result["closed_before_submission"] = 0
        for row in rows:
            if row["status"]:
                item = {**row, "result": json.loads(row["result"])}
                for key, count in summarize([item], row["mode"]).items():
                    result[key] += count
            elif row["state"] == "SIMULATED":
                result["simulated"] += 1
            elif row["state"] == "CLOSED":
                result["closed_before_submission"] += 1
        return result

    def import_sources(self, policy_id, value):
        policy = self.policy(policy_id)
        config = InteractionConfig.model_validate(policy["config"])
        if config.scan.source != "imported_results" or not config.account_ids:
            raise Conflict("select_imported_results_source_first")
        account, now = self.engine.account(config.account_ids[0]), self.clock()
        from .interaction_intents import event_id

        with self.store.connect(write=True) as db:
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM hf_scan_runs WHERE policy_id=?", (policy_id,)
            ).fetchone()[0]
            run_id = uid("import-")
            db.execute(
                "INSERT INTO hf_scan_runs(id,policy_id,seq,config_version,count_epoch,config,upper_at,started_at,deadline_at,state,coverage_incomplete,error) "
                "VALUES(?,?,?,?,?,?,?,?,?,'classifying',1,'manual_import_coverage_incomplete')",
                (
                    run_id,
                    policy_id,
                    seq,
                    policy["version"],
                    policy["count_epoch"],
                    canonical(config.model_dump()),
                    now,
                    now,
                    now + config.scan.round_deadline_seconds,
                ),
            )
            run = db.execute("SELECT * FROM hf_scan_runs WHERE id=?", (run_id,)).fetchone()
            for imported in value.sources:
                source = imported.model_dump(exclude={"legacy_candidate_id"}, exclude_none=True)
                if imported.legacy_candidate_id:
                    legacy = db.execute(
                        "SELECT c.payload,c.target,t.policy,a.platform FROM candidates c "
                        "JOIN tasks t ON t.id=c.task_id JOIN accounts a ON a.id=c.account_id WHERE c.id=?",
                        (imported.legacy_candidate_id,),
                    ).fetchone()
                    if (
                        not legacy
                        or legacy["platform"] != config.platform
                        or (
                            json.loads(legacy["policy"]).get("project_id", "default")
                            != config.project_id
                        )
                    ):
                        raise Conflict("legacy_source_scope_mismatch")
                    payload = json.loads(legacy["payload"])
                    if payload.get("is_public") is not True:
                        raise Conflict("legacy_source_not_public")
                    original = {
                        "id": event_id(
                            config.platform, {"action": "reply", "target": legacy["target"]}
                        ),
                        "url": legacy["target"],
                        "text": payload["text"],
                    }
                    if any(k in source and source[k] != v for k, v in original.items()):
                        raise Conflict("legacy_source_content_mismatch")
                    source.update(original)
                self._ingest(db, run, source, config, account)
            Store.audit(
                db,
                "interaction.sources_imported",
                {
                    "policy_id": policy_id,
                    "run_id": run_id,
                    "count": len(value.sources),
                    "coverage": "incomplete",
                    "external_actions": 0,
                },
                now=now,
            )
        return {"run_id": run_id, "coverage_complete": False, "imported": len(value.sources)}

    def import_options(self, policy_id):
        config = InteractionConfig.model_validate(self.policy(policy_id)["config"])
        return [
            {"id": r["id"], "target": r["target"], "text": json.loads(r["payload"])["text"]}
            for r in self.store.rows(
                "SELECT c.id,c.target,c.payload,t.policy FROM candidates c "
                "JOIN tasks t ON t.id=c.task_id JOIN accounts a ON a.id=c.account_id "
                "WHERE a.platform=? AND COALESCE(json_extract(t.policy,'$.project_id'),'default')=? "
                "AND json_extract(c.payload,'$.is_public')=1 ORDER BY c.id LIMIT 100",
                (config.platform, config.project_id),
            )
        ]

    def candidates(self, policy_id, *, offset=0, limit=100):
        self.policy(policy_id)
        if not 0 <= offset or not 1 <= limit <= 500:
            raise Conflict("分页范围无效")
        return [
            self.candidate(r["id"])
            for r in self.store.rows(
                "SELECT DISTINCT c.id,c.first_seen_at FROM hf_candidates c LEFT JOIN hf_discoveries d ON d.candidate_id=c.id WHERE c.policy_id=? OR d.policy_id=? ORDER BY c.first_seen_at DESC LIMIT ? OFFSET ?",
                (policy_id, policy_id, limit, offset),
            )
        ]

    @staticmethod
    def channel(config):
        return (
            "workbench"
            if config.review.notification_channel == "inherit"
            else config.review.notification_channel
        )

    def schedule(self):
        """Short coordinator; external calls happen only in independently consumed stage jobs."""
        now = self.clock()
        for policy in self.list():
            config = InteractionConfig.model_validate(policy["config"])
            if (
                config.enabled
                and config.scan.source != "imported_results"
                and (policy["next_scan_at"] <= now or policy["pending_manual"])
                and policy["deferred_until"] <= now
            ):
                self.trigger(policy["id"])
        self.sync_results()
        with self.store.connect(write=True) as db:
            for candidate in db.execute(
                "SELECT * FROM hf_candidates WHERE state='READY' AND item_id IS NULL"
            ).fetchall():
                self._recover_handoff(db, candidate)
            for run in db.execute(
                "SELECT * FROM hf_scan_runs WHERE state IN ('collecting','classifying') AND deadline_at<=?",
                (now,),
            ).fetchall():
                if (
                    run["state"] == "collecting"
                    and db.execute(
                        "SELECT 1 FROM execution_jobs WHERE kind='hf_scan' AND reference_id=? AND state='leased' AND lease_until>?",
                        (run["id"], now),
                    ).fetchone()
                ):
                    continue
                db.execute(
                    "UPDATE hf_scan_runs SET state='failed',error='round_deadline',finished_at=? WHERE id=?",
                    (now, run["id"]),
                )
                self._release_scan(db, run)
            # Expiry never releases uncertain publication locks.
            db.execute(
                "UPDATE hf_candidates SET state='CLOSED',closed_reason=CASE WHEN state='REVIEW_PENDING' THEN 'expired_waiting_review' ELSE 'expired_before_submission' END,updated_at=? WHERE expires_at<=? AND state NOT IN ('CLOSED','PUBLISHED','SIMULATED','PUBLISHING','VERIFYING')",
                (now, now),
            )
            for row in db.execute(
                "SELECT * FROM hf_candidates WHERE state='REVIEW_PENDING' AND priority='P2' AND expires_at>?",
                (now,),
            ).fetchall():
                cfg = InteractionConfig.model_validate_json(
                    db.execute(
                        "SELECT config FROM hf_policies WHERE id=?", (row["policy_id"],)
                    ).fetchone()[0]
                )
                batch = db.execute(
                    "SELECT * FROM hf_batches WHERE id=?", (row["batch_id"],)
                ).fetchone()
                if (
                    cfg.review.expiry_alert_enabled
                    and batch
                    and row["expires_at"] < batch["due_at"]
                    and now >= row["expires_at"] - cfg.review.expiry_alert_lead_seconds
                ):
                    self._notice(
                        db,
                        row["policy_id"],
                        "expiry",
                        f"expiry:{row['id']}:{row['expires_at']}",
                        [row["id"]],
                        cfg,
                    )
            for batch in db.execute(
                "SELECT * FROM hf_batches WHERE state='open' AND due_at<=?", (now,)
            ).fetchall():
                cfg = InteractionConfig.model_validate_json(
                    db.execute(
                        "SELECT config FROM hf_policies WHERE id=?", (batch["policy_id"],)
                    ).fetchone()[0]
                )
                members = [
                    r[0]
                    for r in db.execute(
                        "SELECT candidate_id FROM hf_batch_members WHERE batch_id=?", (batch["id"],)
                    )
                ]
                self._notice(
                    db,
                    batch["policy_id"],
                    "summary",
                    "batch:" + batch["id"],
                    members,
                    cfg,
                    batch_id=batch["id"],
                )
            # Definitively blocked work stays dormant; a fresh scan/config/manual choice may wake it.
            for row in db.execute(
                "SELECT id,version,state FROM hf_candidates WHERE state IN ('READY','PUBLISHING','VERIFYING')"
            ).fetchall():
                if row["state"] == "READY":
                    self.queue.put(db, "publish", row["id"] + "|" + str(row["version"]))
            for row in db.execute(
                "SELECT candidate_id,offset_seconds,due_at FROM hf_observations WHERE state='pending'"
            ).fetchall():
                self.queue.put(
                    db,
                    "observe",
                    row["candidate_id"] + "|" + str(row["offset_seconds"]),
                    at=row["due_at"],
                )
        self.sync_results()
        self.settle_rounds()
        self.assess_fallback()
        self.sync_processing()

    def _release_scan(self, db, run):
        db.execute(
            "UPDATE hf_policies SET active_run_id=NULL,next_scan_at=CASE WHEN pending_trigger=1 THEN MIN(next_scan_at,?) ELSE next_scan_at END WHERE id=? AND active_run_id=?",
            (self.clock(), run["policy_id"], run["id"]),
        )

    def _read_budget(self, db, config):
        now = self.clock()
        used = db.execute(
            "SELECT count(*) FROM hf_usage WHERE project_id=? AND kind='read' AND at>=?",
            (config.project_id, now - now % 86400),
        ).fetchone()[0]
        if used >= config.scan.max_reads_per_day:
            raise Conflict("read_budget_exhausted")
        usage = uid("usage-")
        db.execute(
            "INSERT INTO hf_usage(id,project_id,kind,at,details) VALUES(?,?,'read',?,'{}')",
            (usage, config.project_id, now),
        )
        return usage

    def _scan(self, job):
        run = self.store.one("SELECT * FROM hf_scan_runs WHERE id=?", (job["reference_id"],))
        if not run or run["state"] != "collecting":
            return
        config = InteractionConfig.model_validate_json(run["config"])
        if config.run_mode != "discovery_only":
            readiness = self.readiness(config)
            if readiness["blockers"]:
                if readiness["retry_at"]:
                    with self.store.connect(write=True) as db:
                        self.queue.guard(db, job)
                        db.execute(
                            "UPDATE hf_policies SET next_scan_at=MAX(next_scan_at,?) WHERE id=?",
                            (readiness["retry_at"], run["policy_id"]),
                        )
                raise Conflict("interaction_not_ready:" + ",".join(readiness["blockers"]))
        account = self.engine.account(run["search_account_id"] or config.account_ids[0])
        if config.joint:
            return self._scan_joint(job, run, config, account)
        for progress in self.store.rows(
            "SELECT * FROM hf_query_progress WHERE run_id=? AND complete=0", (run["id"],)
        ):
            while not progress["complete"]:
                until = self._read_after(account["id"])
                if until > self.clock():
                    with self.store.connect(write=True) as db:
                        self.queue.guard(db, job)
                        db.execute(
                            "UPDATE hf_policies SET next_scan_at=MAX(next_scan_at,?) WHERE id=?",
                            (until, run["policy_id"]),
                        )
                    raise Conflict("x_api_read_cooldown")
                if self.clock() >= run["deadline_at"]:
                    raise Conflict("round_deadline")
                if progress["pages"] >= config.scan.max_pages_per_query:
                    raise Conflict("coverage_incomplete:page_cap")
                with self.store.connect(write=True) as db:
                    self.queue.guard(db, job)
                    usage = self._read_budget(db, config)
                started = time.monotonic()
                result = self.io.discover(
                    account,
                    progress["query"],
                    progress["lower_at"],
                    progress["upper_at"],
                    progress["cursor"],
                    config,
                )
                if result.get("coverage_complete") is not True:
                    reason = result.get("reason")
                    if reason == "XAPIRateLimited":
                        delay = result.get("retry_after_seconds", 900)
                        delay = max(900, delay) if type(delay) is int and 0 < delay < 1e12 else 900
                        until = max(self._read_after(account["id"]), self.clock() + delay)
                        with self.store.connect(write=True) as db:
                            self.queue.guard(db, job)
                            db.execute(
                                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) "
                                "DO UPDATE SET value=CAST(MAX(CAST(value AS REAL),CAST(excluded.value AS REAL)) AS TEXT)",
                                ("hf_x_read_after:" + account["id"], str(until)),
                            )
                            db.execute(
                                "UPDATE hf_policies SET next_scan_at=MAX(next_scan_at,?) WHERE id=?",
                                (until, run["policy_id"]),
                            )
                    if reason in {
                        "XAPIRateLimited",
                        "XAPIAuthenticationRejected",
                        "XAPIAccessDenied",
                        "XAPISearchRejected",
                        "XAPIPartialSearch",
                    }:
                        raise Conflict(reason)
                    raise Conflict("coverage_incomplete:adapter_coverage_unknown")
                cursor = result.get("next_cursor")
                if cursor and cursor == progress["cursor"]:
                    raise Conflict("coverage_incomplete:cursor_not_advancing")
                with self.store.connect(write=True) as db:
                    self.queue.guard(db, job)
                    current = db.execute(
                        "SELECT state FROM hf_scan_runs WHERE id=?", (run["id"],)
                    ).fetchone()
                    if current[0] != "collecting":
                        return
                    for source in result.get("candidates", []):
                        self._ingest(db, run, source, config, account)
                    db.execute(
                        "UPDATE hf_usage SET duration_ms=?,actual_cost=? WHERE id=?",
                        (
                            int((time.monotonic() - started) * 1000),
                            result.get("actual_cost"),
                            usage,
                        ),
                    )
                    db.execute(
                        "UPDATE hf_query_progress SET cursor=?,pages=pages+1,complete=? WHERE run_id=? AND query=?",
                        (cursor, not cursor, run["id"], progress["query"]),
                    )
                    if not cursor:
                        db.execute(
                            "INSERT INTO hf_watermarks VALUES(?,?,?) ON CONFLICT(policy_id,query) DO UPDATE SET watermark=MAX(watermark,excluded.watermark)",
                            (run["policy_id"], progress["query"], progress["upper_at"]),
                        )
                progress = self.store.one(
                    "SELECT * FROM hf_query_progress WHERE run_id=? AND query=?",
                    (run["id"], progress["query"]),
                )
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            db.execute(
                "UPDATE hf_scan_runs SET state='classifying' WHERE id=? AND state='collecting'",
                (run["id"],),
            )
            self._release_scan(db, run)

    def _readonly_scan(self, job):
        return self._scan(job)

    def _ingest(self, db, run, source, config, account, *, partition=None):
        # Whitelist public source fields; discard arbitrary provider data and secrets.
        fields = (
            "id",
            "url",
            "text",
            "author_id",
            "source_event_at",
            "context",
            "media",
            "language",
            "context_status",
            "conversation_id",
            "mentioned_user_ids",
            "in_reply_to_user_id",
            "in_reply_to_event_id",
        )
        allowed = fields + (
            ("simulation_classification", "simulation_eligibility", "simulation_current")
            if account["mode"] == "simulation"
            else ()
        )
        source = {k: source[k] for k in allowed if k in source}
        if (
            not isinstance(source.get("id"), str)
            or not source["id"]
            or not isinstance(source.get("source_event_at"), (int, float))
            or not source.get("url")
            or not isinstance(source.get("text"), str)
        ):
            raise Conflict("invalid_source_event")
        from .interaction_intents import event_id, scope_id

        if (
            event_id(config.platform, {"action": "reply", "target": source["url"]}) != source["id"]
            or isinstance(source["source_event_at"], bool)
            or not math.isfinite(source["source_event_at"])
        ):
            raise Conflict("source_identity_or_timestamp_invalid")
        if not run["upper_at"] >= source["source_event_at"] or source["source_event_at"] < 1:
            raise Conflict("invalid_source_timestamp")
        if len(source["text"]) > 30000 or len(canonical(source)) > 150000:
            raise Conflict("source_too_large")
        hashed, now = source_hash(source), self.clock()
        row = db.execute(
            "SELECT * FROM hf_candidates WHERE project_id IN (SELECT project_id FROM hf_dedupe_projects WHERE scope_id=?) AND platform=? AND source_event_id=? AND action_kind='reply' ORDER BY CASE WHEN state IN ('CLOSED','PUBLISHED','VERIFYING','PUBLISHING') THEN 0 ELSE 1 END,first_seen_at LIMIT 1",
            (scope_id(db, config.scope_id), config.platform, source["id"]),
        ).fetchone()
        db.execute(
            "UPDATE hf_scan_runs SET fetched=fetched+1,duplicates=duplicates+? WHERE id=?",
            (int(bool(row)), run["id"]),
        )
        candidate_id = row["id"] if row else uid("hfc-")
        db.execute(
            "INSERT OR IGNORE INTO hf_discoveries VALUES(?,?,?,?,?,?,?,?)",
            (
                candidate_id,
                run["policy_id"],
                run["id"],
                partition["query"] if partition else "legacy",
                account["id"],
                partition["product_ids"] if partition else "[]",
                hashed,
                now,
            ),
        )
        if not row:
            db.execute(
                "INSERT INTO hf_candidates(id,policy_id,project_id,platform,source_event_id,source,source_hash,source_event_at,first_seen_at,owner_account_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    run["policy_id"],
                    config.project_id,
                    config.platform,
                    source["id"],
                    canonical(source),
                    hashed,
                    source["source_event_at"],
                    now,
                    account["id"],
                    now,
                ),
            )
        elif row["source_hash"] != hashed and row["state"] not in {
            "CLOSED",
            "PUBLISHED",
            "SIMULATED",
            "PUBLISHING",
            "VERIFYING",
        }:
            self._retire_unsubmitted(db, row)
            db.execute(
                "UPDATE hf_candidates SET source=?,source_hash=?,source_event_at=?,version=version+1,selected_version=NULL,state='DISCOVERED',task_id=NULL,item_id=NULL,draft=CASE WHEN draft_origin='human' THEN draft ELSE '' END,updated_at=? WHERE id=?",
                (canonical(source), hashed, source["source_event_at"], now, candidate_id),
            )
        db.execute(
            "UPDATE hf_candidates SET discovery_account_id=COALESCE(discovery_account_id,?) WHERE id=?",
            (account["id"], candidate_id),
        )
        terminal = row and row["state"] in {
            "CLOSED",
            "PUBLISHED",
            "SIMULATED",
            "PUBLISHING",
            "VERIFYING",
        }
        same = (
            row
            and row["source_hash"] == hashed
            and row["policy_id"] == run["policy_id"]
            and row["config_version"] == run["config_version"]
            and row["priority"] is not None
        )
        db.execute(
            "INSERT OR IGNORE INTO hf_scan_candidates(run_id,candidate_id,source_hash,state,priority) VALUES(?,?,?,?,?)",
            (
                run["id"],
                candidate_id,
                hashed,
                "complete" if terminal or same else "pending",
                row["priority"] if row else None,
            ),
        )
        if not terminal and not same:
            self.queue.put(
                db,
                "readonly_classify" if config.run_mode == "discovery_only" else "classify",
                run["id"] + "|" + candidate_id + "|" + hashed,
            )

    def _retire_unsubmitted(self, db, row):
        if not row["item_id"]:
            return
        item = db.execute(
            "SELECT attempt_id,status FROM items WHERE id=?", (row["item_id"],)
        ).fetchone()
        if item and item["attempt_id"]:
            raise Conflict("submitted_intent_cannot_be_replaced")
        db.execute(
            "UPDATE tasks SET state='cancelled',updated_at=? WHERE id=?",
            (self.clock(), row["task_id"]),
        )
        db.execute(
            "UPDATE items SET status='cancelled' WHERE id=? AND attempt_id IS NULL",
            (row["item_id"],),
        )
        db.execute(
            "DELETE FROM quota_reservations WHERE item_id=? AND state='reserved'", (row["item_id"],)
        )
        db.execute(
            "UPDATE interaction_intents SET item_id=NULL,state='reserved' WHERE candidate_id=?",
            (row["id"],),
        )

    def _quality(self, classification, source, config):
        if config.joint:
            return self._joint_quality(classification, source, config)
        now = self.clock()
        if classification.priority == "EXCLUDED":
            return None if classification.exclusion_reason else "exclusion_reason_missing"
        if (
            classification.context_status != "complete"
            or source.get("context_status") != "complete"
        ):
            return "context_incomplete"
        if classification.quality_status != "passed" or not classification.target_account_fit:
            return "quality_pending"
        if (
            not classification.product_fit
            or not classification.contribution
            or not classification.intent_evidence
        ):
            return "contribution_or_intent_missing"
        context = source["text"] + "\n" + source.get("context", "")
        if any(
            not isinstance(evidence, str) or not evidence or evidence not in context
            for evidence in classification.intent_evidence
        ):
            return "intent_evidence_invalid"
        facts = {f.id for f in config.facts if f.verified_at <= now < f.valid_until}
        if (
            not classification.verified_fact_ids
            or not set(classification.verified_fact_ids) <= facts
        ):
            return "facts_unverified"
        if classification.language not in config.languages:
            return "language_not_authorized"
        return None

    def _classify(self, job):
        run_id, candidate_id, suffix = job["reference_id"].split("|", 2)
        candidate = self.candidate(candidate_id)
        if candidate["state"] in {"CLOSED", "PUBLISHED", "SIMULATED", "PUBLISHING", "VERIFYING"}:
            return
        origin_run = (
            self.store.one("SELECT policy_id FROM hf_scan_runs WHERE id=?", (run_id,))
            if run_id != "current"
            else None
        )
        policy = self.policy(origin_run["policy_id"] if origin_run else candidate["policy_id"])
        if candidate["processing_task_id"] or candidate["item_id"]:
            policy = self.policy(candidate["policy_id"])
        config = InteractionConfig.model_validate(policy["config"])
        if config.run_mode == "discovery_only":
            with self.store.connect(write=True) as db:
                self.queue.guard(db, job)
                db.execute(
                    "UPDATE hf_scan_candidates SET state='complete',error='manual_review_required' WHERE run_id=? AND candidate_id=? AND state='pending'",
                    (run_id, candidate_id),
                )
                db.execute(
                    "UPDATE hf_candidates SET block_reason='manual_review_required',classification_status='pending_context' WHERE id=? AND state='DISCOVERED'",
                    (candidate_id,),
                )
            return
        account = self.engine.account(
            candidate["discovery_account_id"] or candidate["owner_account_id"]
        )
        started = time.monotonic()
        classification = Classification.model_validate(
            self.io.classify(candidate["source"], config, account)
        )
        if (
            config.joint
            and candidate["priority"] == "P0"
            and classification.priority == "P1"
            and self._conversation_owner(candidate["source"])
        ):
            classification.priority = "P0"
        reason = self._quality(classification, candidate["source"], config)
        if reason:
            raise Conflict(reason)
        expiry = candidate["source_event_at"] + getattr(
            config.ttl, classification.opportunity_type + "_seconds"
        )
        now = self.clock()
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            current = db.execute(
                "SELECT * FROM hf_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            current_policy = db.execute(
                "SELECT version FROM hf_policies WHERE id=?", (policy["id"],)
            ).fetchone()[0]
            if (
                current["version"] != candidate["version"]
                or current["classification_revision"] != candidate["classification_revision"]
                or current["source_hash"] != candidate["source_hash"]
                or current_policy != policy["version"]
                or current["state"]
                in {"CLOSED", "PUBLISHING", "VERIFYING", "PUBLISHED", "SIMULATED"}
            ):
                return
            high_priorities = {"P0", "P1"} if config.joint else {"P0", "P1", "P2"}
            new_high = (
                classification.priority in high_priorities
                and current["priority"] not in high_priorities
                and expiry > now
            )
            if new_high:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO hf_admissions VALUES(?,?,?)",
                    (candidate_id, candidate["source_hash"], now),
                ).rowcount
                target_run = db.execute(
                    "SELECT r.id FROM hf_scan_runs r JOIN hf_scan_candidates c ON c.run_id=r.id WHERE c.candidate_id=? AND c.source_hash=? AND r.policy_id=? AND r.count_epoch=? AND r.state IN ('collecting','classifying') AND r.seq>? ORDER BY r.seq LIMIT 1",
                    (
                        candidate_id,
                        candidate["source_hash"],
                        policy["id"],
                        policy["count_epoch"],
                        policy["last_settled_seq"],
                    ),
                ).fetchone()
                if inserted and target_run:
                    db.execute(
                        "UPDATE hf_scan_runs SET new_high=new_high+1 WHERE id=?", (target_run[0],)
                    )
                # Late failed/old-epoch runs may route real opportunities, but cannot rewrite newer counters.
                can_count = run_id == "current" or target_run is not None
                if can_count:
                    db.execute("UPDATE hf_policies SET empty_streak=0 WHERE id=?", (policy["id"],))
                db.execute(
                    "UPDATE hf_assessments SET state='superseded' WHERE policy_id=? AND state='assessed'",
                    (policy["id"],),
                )
            db.execute(
                "INSERT INTO hf_classification_history(candidate_id,policy_id,config_version,source_hash,classification,at) VALUES(?,?,?,?,?,?)",
                (
                    candidate_id,
                    policy["id"],
                    policy["version"],
                    candidate["source_hash"],
                    canonical(classification.model_dump()),
                    now,
                ),
            )
            if current["policy_id"] != policy["id"] and (
                current["processing_task_id"] or current["item_id"]
            ):
                db.execute(
                    "UPDATE hf_scan_candidates SET state='complete',priority=?,error=NULL WHERE run_id=? AND candidate_id=?",
                    (classification.priority, run_id, candidate_id),
                )
                return
            if config.joint:
                if current["policy_id"] != policy["id"] and json.loads(current["classification"]):
                    db.execute(
                        "INSERT INTO hf_classification_history(candidate_id,policy_id,config_version,source_hash,classification,at) VALUES(?,?,?,?,?,?)",
                        (
                            candidate_id,
                            current["policy_id"],
                            current["config_version"],
                            current["source_hash"],
                            current["classification"],
                            now,
                        ),
                    )
                db.execute(
                    "UPDATE hf_candidates SET policy_id=?,classification_status=? WHERE id=?",
                    (policy["id"], classification.classification_status, candidate_id),
                )
            state = "QUEUED" if classification.priority in {"P0", "P1"} else "REVIEW_PENDING"
            closed = classification.priority == "EXCLUDED" or expiry <= now
            if closed:
                state = "CLOSED"
            # A timing-only config change preserves a valid human selection and edited draft.
            if current["selected_version"] == current["version"] and not closed:
                state = "QUEUED"
            if classification.priority == "P3" and not current["p3_assessment_id"]:
                state = "DISCOVERED"
            db.execute(
                "UPDATE hf_candidates SET priority=?,classification=?,config_version=?,expires_at=?,state=?,closed_reason=?,block_reason=NULL,classification_revision=classification_revision+1,updated_at=? WHERE id=?",
                (
                    classification.priority,
                    canonical(
                        {
                            **classification.model_dump(),
                            "classifier_version": config.classifier_version,
                            "product_knowledge_version": config.product_knowledge_version,
                            "config_version": policy["version"],
                        }
                    ),
                    policy["version"],
                    expiry,
                    state,
                    classification.exclusion_reason
                    if classification.priority == "EXCLUDED"
                    else "expired_on_discovery"
                    if expiry <= now
                    else None,
                    now,
                    candidate_id,
                ),
            )
            db.execute(
                "UPDATE hf_scan_candidates SET state='complete',priority=?,error=NULL WHERE candidate_id=? AND source_hash=? AND state='pending'",
                (classification.priority, candidate_id, candidate["source_hash"]),
            )
            if config.joint and classification.priority in {"P2", "P3"}:
                db.execute(
                    "UPDATE hf_candidates SET state='DISCOVERED',block_reason='joint_priority_not_dispatched' WHERE id=?",
                    (candidate_id,),
                )
                state = "DISCOVERED"
            if (
                state == "REVIEW_PENDING"
                and classification.priority == "P2"
                and config.review.enabled
            ):
                self._batch(db, policy, candidate_id, config)
            elif classification.priority in {"P0", "P1"}:
                db.execute("DELETE FROM hf_batch_members WHERE candidate_id=?", (candidate_id,))
                db.execute("UPDATE hf_candidates SET batch_id=NULL WHERE id=?", (candidate_id,))
            if state == "QUEUED" and config.joint:
                self._create_processing(db, candidate_id, config, classification)
            elif state == "QUEUED":
                self.queue.put(
                    db,
                    "generate",
                    candidate_id + "|" + str(current["version"]) + "|" + str(policy["version"]),
                )
            usage = uid("usage-")
            db.execute(
                "INSERT INTO hf_usage(id,project_id,kind,at,duration_ms,details) VALUES(?,?,'classify',?,?,?)",
                (
                    usage,
                    config.project_id,
                    now,
                    int((time.monotonic() - started) * 1000),
                    canonical({"candidate_id": candidate_id}),
                ),
            )

    def _readonly_classify(self, job):
        return self._classify(job)

    def _batch(self, db, policy, candidate_id, config):
        current = db.execute(
            "SELECT batch_id FROM hf_candidates WHERE id=?", (candidate_id,)
        ).fetchone()[0]
        if current:
            return
        batch = db.execute(
            "SELECT id FROM hf_batches WHERE policy_id=? AND state='open'", (policy["id"],)
        ).fetchone()
        batch_id = batch[0] if batch else uid("batch-")
        if not batch:
            now = self.clock()
            db.execute(
                "INSERT INTO hf_batches(id,policy_id,opened_at,due_at,config_version) VALUES(?,?,?,?,?)",
                (
                    batch_id,
                    policy["id"],
                    now,
                    now + config.review.batch_wait_seconds,
                    policy["version"],
                ),
            )
        db.execute("INSERT OR IGNORE INTO hf_batch_members VALUES(?,?)", (batch_id, candidate_id))
        db.execute("UPDATE hf_candidates SET batch_id=? WHERE id=?", (batch_id, candidate_id))

    def settle_rounds(self):
        now = self.clock()
        with self.store.connect(write=True) as db:
            for policy in db.execute("SELECT * FROM hf_policies").fetchall():
                for run in db.execute(
                    "SELECT * FROM hf_scan_runs WHERE policy_id=? AND settled_at IS NULL ORDER BY seq",
                    (policy["id"],),
                ).fetchall():
                    if run["state"] == "collecting":
                        break
                    pending = db.execute(
                        "SELECT count(*) FROM hf_scan_candidates WHERE run_id=? AND state='pending'",
                        (run["id"],),
                    ).fetchone()[0]
                    if run["state"] == "classifying" and pending:
                        break
                    complete = run["state"] == "classifying" and not run["coverage_incomplete"]
                    partial = (
                        run["state"] == "classifying"
                        and bool(run["coverage_incomplete"])
                        and json.loads(policy["config"]).get("run_mode") == "discovery_only"
                    )
                    db.execute(
                        "UPDATE hf_scan_runs SET state=?,finished_at=COALESCE(finished_at,?),settled_at=? WHERE id=? AND settled_at IS NULL",
                        (
                            "complete" if complete else "partial" if partial else "failed",
                            now,
                            now,
                            run["id"],
                        ),
                    )
                    if run["count_epoch"] == policy["count_epoch"]:
                        if complete:
                            db.execute(
                                "UPDATE hf_policies SET empty_streak=CASE WHEN ? > 0 THEN 0 ELSE empty_streak+1 END,last_complete_at=?,last_settled_seq=? WHERE id=?",
                                (run["new_high"], now, run["seq"], policy["id"]),
                            )
                        else:
                            db.execute(
                                "UPDATE hf_policies SET empty_streak=0,last_settled_seq=? WHERE id=?",
                                (run["seq"], policy["id"]),
                            )

    def assess_fallback(self):
        now = self.clock()
        ready = {}
        for policy in self.list():
            config = InteractionConfig.model_validate(policy["config"])
            ready[policy["id"]] = (policy["version"], self.readiness(config)["ready"])
        with self.store.connect(write=True) as db:
            for policy in db.execute("SELECT * FROM hf_policies").fetchall():
                config = InteractionConfig.model_validate_json(policy["config"])
                if (
                    not config.fallback.enabled
                    or config.joint
                    or ready.get(policy["id"]) != (policy["version"], True)
                    or config.scan.source == "imported_results"
                ):
                    continue
                last = db.execute(
                    "SELECT state,count_epoch FROM hf_scan_runs WHERE policy_id=? ORDER BY seq DESC LIMIT 1",
                    (policy["id"],),
                ).fetchone()
                if (
                    not config.enabled
                    or policy["empty_streak"] < config.fallback.empty_rounds_required
                    or not last
                    or last["state"] != "complete"
                    or last["count_epoch"] != policy["count_epoch"]
                    or (
                        policy["last_p3_at"] is not None
                        and now - policy["last_p3_at"] < config.fallback.evaluation_cooldown_seconds
                    )
                ):
                    continue
                if db.execute(
                    "SELECT 1 FROM hf_candidates WHERE project_id=? AND priority IN ('P0','P1') AND state IN ('QUEUED','GENERATING','READY','PUBLISHING') LIMIT 1",
                    (config.project_id,),
                ).fetchone():
                    continue
                rows = db.execute(
                    "SELECT * FROM hf_candidates WHERE policy_id=? AND priority='P3' AND state='DISCOVERED' AND p3_assessment_id IS NULL AND expires_at>? ORDER BY CAST(json_extract(classification,'$.value') AS REAL) DESC,source_event_at DESC LIMIT ?",
                    (policy["id"], now, config.fallback.max_candidates_per_evaluation),
                ).fetchall()
                rows = [
                    r
                    for r in rows
                    if json.loads(r["classification"]).get("quality_status") == "passed"
                    and json.loads(r["classification"]).get("value", 0) > 0
                ]
                if not rows:
                    continue
                # This writer transaction is the atomic cooldown reservation.
                assessment = uid("p3-")
                ids = [r["id"] for r in rows]
                db.execute("UPDATE hf_policies SET last_p3_at=? WHERE id=?", (now, policy["id"]))
                db.execute(
                    "INSERT INTO hf_assessments(id,policy_id,created_at,count_epoch,candidates) VALUES(?,?,?,?,?)",
                    (assessment, policy["id"], now, policy["count_epoch"], canonical(ids)),
                )
                for candidate_id in ids:
                    db.execute(
                        "UPDATE hf_candidates SET state='REVIEW_PENDING',p3_assessment_id=? WHERE id=?",
                        (assessment, candidate_id),
                    )
                batch = db.execute(
                    "SELECT * FROM hf_batches WHERE policy_id=? AND state='open' ORDER BY opened_at LIMIT 1",
                    (policy["id"],),
                ).fetchone()
                if batch:
                    for candidate_id in ids:
                        db.execute(
                            "INSERT OR IGNORE INTO hf_batch_members VALUES(?,?)",
                            (batch["id"], candidate_id),
                        )
                    continue
                if db.execute(
                    "SELECT 1 FROM hf_candidates WHERE policy_id=? AND priority='P2' AND state='REVIEW_PENDING' AND expires_at>? LIMIT 1",
                    (policy["id"], now),
                ).fetchone():
                    continue
                self._notice(db, policy["id"], "fallback", "p3:" + assessment, ids, config)

    def _notice(self, db, policy_id, kind, key, ids, config, *, batch_id=None):
        notice_id = "notice-" + digest([key, self.channel(config)])[:24]
        snapshot = []
        for candidate_id in ids:
            row = db.execute("SELECT * FROM hf_candidates WHERE id=?", (candidate_id,)).fetchone()
            if row and row["state"] == "REVIEW_PENDING" and row["expires_at"] > self.clock():
                if kind == "summary" and self._expiry_noticed(db, candidate_id):
                    continue
                snapshot.append(self._snapshot(row))
        # Keep an empty regular notice so a due batch can be closed atomically.
        if not snapshot and not batch_id:
            return
        db.execute(
            "INSERT OR IGNORE INTO hf_notices(id,policy_id,dedupe_key,channel,notice_type,batch_id,snapshot,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                notice_id,
                policy_id,
                key,
                self.channel(config),
                kind,
                batch_id,
                canonical(snapshot),
                self.clock(),
            ),
        )
        self.queue.put(db, "notify", notice_id)

    @staticmethod
    def _snapshot(row):
        source, classification = json.loads(row["source"]), json.loads(row["classification"])
        return {
            "candidate_id": row["id"],
            "version": row["version"],
            "url": source["url"],
            "need": source["text"][:800],
            "priority": row["priority"],
            "reason": classification.get("priority_reason"),
            "account_id": row["owner_account_id"],
            "draft": row["draft"] or classification.get("contribution", ""),
            "media_plan": json.loads(row["media_plan"]),
            "expires_at": row["expires_at"],
            "entry": "#interactions/" + row["policy_id"],
        }

    @staticmethod
    def _expiry_noticed(db, candidate_id):
        return (
            db.execute(
                "SELECT 1 FROM hf_notices WHERE notice_type='expiry' AND state IN ('sent','sending','unknown') AND dedupe_key LIKE ? LIMIT 1",
                ("expiry:" + candidate_id + ":%",),
            ).fetchone()
            is not None
        )

    def _notify(self, job):
        notice = self.store.one("SELECT * FROM hf_notices WHERE id=?", (job["reference_id"],))
        if not notice or notice["state"] in {"sent", "cancelled", "failed"}:
            return
        config = InteractionConfig.model_validate(self.policy(notice["policy_id"])["config"])
        now = self.clock()
        if not config.enabled and notice["state"] not in {"sending", "unknown"}:
            return now + config.scan.interval_seconds
        if (
            within_hours(now, config.timezone, config.notifications.quiet_hours)
            and config.notifications.quiet_hours[0] != config.notifications.quiet_hours[1]
        ):
            return now + config.notifications.retry_delays_seconds[0]
        if notice["state"] in {"sending", "unknown"}:
            result = self.io.verify_notification(notice)
            with self.store.connect(write=True) as db:
                self.queue.guard(db, job)
                self._notice_result(db, notice, result, config)
            return
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            fresh = dict(
                db.execute("SELECT * FROM hf_notices WHERE id=?", (notice["id"],)).fetchone()
            )
            snapshot = json.loads(fresh["snapshot"])
            if fresh["batch_id"]:
                # First attempt freezes the complete current membership, then new P2 opens a new batch.
                batch = db.execute(
                    "SELECT state FROM hf_batches WHERE id=?", (fresh["batch_id"],)
                ).fetchone()
                if batch[0] == "open":
                    snapshot = [
                        self._snapshot(r)
                        for r in db.execute(
                            "SELECT c.* FROM hf_candidates c JOIN hf_batch_members m ON m.candidate_id=c.id WHERE m.batch_id=? ORDER BY c.priority,c.first_seen_at",
                            (fresh["batch_id"],),
                        )
                        if r["state"] == "REVIEW_PENDING"
                        and r["expires_at"] > now
                        and not self._expiry_noticed(db, r["id"])
                    ]
                    db.execute(
                        "UPDATE hf_batches SET state='sealed',sealed_at=? WHERE id=? AND state='open'",
                        (now, fresh["batch_id"]),
                    )
            filtered = []
            for item in snapshot:
                row = db.execute(
                    "SELECT * FROM hf_candidates WHERE id=?", (item["candidate_id"],)
                ).fetchone()
                if row and row["state"] == "REVIEW_PENDING" and row["expires_at"] > now:
                    filtered.append(self._snapshot(row))
            if not filtered:
                db.execute(
                    "UPDATE hf_notices SET state='cancelled',error='no_valid_candidates' WHERE id=?",
                    (fresh["id"],),
                )
                if fresh["batch_id"]:
                    db.execute(
                        "UPDATE hf_batches SET state='closed' WHERE id=?", (fresh["batch_id"],)
                    )
                return
            if filtered != json.loads(fresh["snapshot"]) and fresh["attempts"]:
                db.execute(
                    "UPDATE hf_notice_attempts SET outcome='not_sent_superseded' WHERE notice_id=? AND revision=? AND outcome='not_sent'",
                    (fresh["id"], fresh["revision"]),
                )
                fresh["revision"] += 1
            fresh["snapshot"] = canonical(filtered)
            fresh["body"] = canonical(
                {
                    "type": fresh["notice_type"],
                    "candidates": filtered[:3],
                    "total_candidates": len(filtered),
                    "entry": "#interactions/" + fresh["policy_id"],
                }
            )
            fresh["attempts"] += 1
            db.execute(
                "UPDATE hf_notices SET state='sending',snapshot=?,body=?,revision=?,attempts=? WHERE id=?",
                (
                    fresh["snapshot"],
                    fresh["body"],
                    fresh["revision"],
                    fresh["attempts"],
                    fresh["id"],
                ),
            )
            db.execute(
                "INSERT INTO hf_notice_attempts VALUES(?,?,?,?,?,'sending',?,NULL)",
                (fresh["id"], fresh["revision"], fresh["attempts"], now, None, fresh["body"]),
            )
        fresh["request_timeout_seconds"] = config.publishing.request_timeout_seconds
        try:
            result = self.io.notify(fresh, fresh["id"] + ":" + str(fresh["revision"]))
        except Exception:
            result = {"status": "unknown", "reason": "notification_outcome_unknown"}
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            retry = self._notice_result(db, fresh, result, config)
        return retry

    def _notice_result(self, db, notice, result, config):
        state = result.get("status")
        state = (
            "sent"
            if state == "sent" and result.get("message_id")
            else "pending"
            if state == "not_sent"
            else "unknown"
        )
        retry = None
        if state == "pending":
            if notice["attempts"] > config.notifications.max_retries_after_confirmed_not_sent:
                state = "failed"
            else:
                delays = config.notifications.retry_delays_seconds
                retry = self.clock() + delays[min(max(notice["attempts"] - 1, 0), len(delays) - 1)]
        db.execute(
            "UPDATE hf_notices SET state=?,message_id=?,delivered_at=?,error=? WHERE id=?",
            (
                state,
                result.get("message_id"),
                self.clock() if state == "sent" else None,
                result.get("reason"),
                notice["id"],
            ),
        )
        db.execute(
            "UPDATE hf_notice_attempts SET outcome=?,finished_at=?,message_id=? WHERE notice_id=? AND revision=? AND attempt=?",
            (
                "not_sent" if result.get("status") == "not_sent" else state,
                self.clock(),
                result.get("message_id"),
                notice["id"],
                notice["revision"],
                notice["attempts"],
            ),
        )
        if state == "sent" and notice["batch_id"]:
            db.execute(
                "UPDATE hf_batches SET state='delivered',delivered_at=? WHERE id=?",
                (self.clock(), notice["batch_id"]),
            )
        return retry

    def action(self, value, *, actor="operator"):
        now = self.clock()
        with self.store.connect(write=True) as db:
            rows = []
            for candidate_id, version in value.versions.items():
                row = db.execute(
                    "SELECT * FROM hf_candidates WHERE id=?", (candidate_id,)
                ).fetchone()
                if not row or row["version"] != version:
                    raise Conflict("候选已改变，请刷新后选择")
                if value.action == "verify_media":
                    generation = db.execute(
                        "SELECT version FROM hf_generation WHERE candidate_id=? AND kind<>'text' AND state IN ('submitting','pending','unknown') ORDER BY started_at LIMIT 1",
                        (candidate_id,),
                    ).fetchone()
                    if not generation:
                        raise Conflict("没有待核验的媒体任务")
                    key = self.queue.put(db, "media", candidate_id + "|" + str(generation[0]))
                    db.execute(
                        "UPDATE execution_jobs SET state='pending',available_at=?,result=? WHERE id=? AND state<>'leased'",
                        (now, canonical({"manual": True}), key),
                    )
                    rows.append(row)
                    continue
                if value.action == "verify":
                    if row["state"] != "VERIFYING":
                        raise Conflict("该候选无需核验")
                    key = self.queue.put(db, "verify", candidate_id)
                    db.execute(
                        "UPDATE execution_jobs SET state='pending',available_at=?,result=? WHERE id=? AND state<>'leased'",
                        (now, canonical({"manual": True}), key),
                    )
                    rows.append(row)
                    continue
                if (
                    row["state"] in {"CLOSED", "PUBLISHING", "VERIFYING", "PUBLISHED", "SIMULATED"}
                    or row["expires_at"] is None
                    or row["expires_at"] <= now
                ):
                    raise Conflict("候选已关闭、过期或已经提交")
                policy = db.execute(
                    "SELECT * FROM hf_policies WHERE id=?", (row["policy_id"],)
                ).fetchone()
                cfg = InteractionConfig.model_validate_json(policy["config"])
                if (
                    cfg.joint
                    and value.action in {"adopt", "edit"}
                    and row["priority"] not in {"P0", "P1"}
                ):
                    raise Conflict("联合策略不派发 P2/P3")
                if value.account_id and value.account_id not in self._execution_ids(
                    cfg, row["primary_product_id"]
                ):
                    raise Conflict("账号不在当前策略范围")
                if (
                    cfg.joint
                    and value.account_id
                    and row["priority"] == "P0"
                    and value.account_id != row["conversation_owner_account_id"]
                ):
                    raise Conflict("P0 必须使用原会话账号")
                if row["item_id"]:
                    item = db.execute(
                        "SELECT status,attempt_id FROM items WHERE id=?", (row["item_id"],)
                    ).fetchone()
                    if item and (item["attempt_id"] or item["status"] != "not_attempted"):
                        raise Conflict("发布意图已尝试，不能改派或重发")
                    if item:
                        db.execute(
                            "UPDATE tasks SET state='cancelled',updated_at=? WHERE id=?",
                            (now, row["task_id"]),
                        )
                        db.execute(
                            "UPDATE items SET status='cancelled' WHERE id=?", (row["item_id"],)
                        )
                        db.execute(
                            "DELETE FROM quota_reservations WHERE item_id=? AND state='reserved'",
                            (row["item_id"],),
                        )
                        db.execute(
                            "UPDATE interaction_intents SET item_id=NULL,state='reserved' WHERE candidate_id=?",
                            (candidate_id,),
                        )
                    db.execute(
                        "UPDATE hf_candidates SET item_id=NULL,task_id=NULL WHERE id=?",
                        (candidate_id,),
                    )
                new_version = row["version"] + 1
                if value.action == "skip":
                    db.execute(
                        "UPDATE hf_candidates SET state='CLOSED',closed_reason='human_skipped',version=?,selected_version=NULL WHERE id=?",
                        (new_version, candidate_id),
                    )
                elif value.action == "defer":
                    if not value.defer_until or not now < value.defer_until < row["expires_at"]:
                        raise Conflict("暂缓时间必须在当前时间与有效期之间")
                    db.execute(
                        "UPDATE hf_candidates SET state='REVIEW_PENDING',version=?,selected_version=NULL,defer_until=? WHERE id=?",
                        (new_version, value.defer_until, candidate_id),
                    )
                else:
                    if value.action == "adopt" and row["state"] not in {
                        "REVIEW_PENDING",
                        "QUEUED",
                        "READY",
                    }:
                        raise Conflict("候选尚未达到人工选择入口")
                    text = row["draft"] if value.text is None else value.text.strip()
                    if value.text is not None and not text:
                        raise Conflict("编辑正文不能为空")
                    db.execute(
                        "UPDATE hf_candidates SET version=?,state=?,selected_version=?,selected_at=?,selected_by=?,draft=?,draft_origin=?,owner_account_id=?,defer_until=NULL,updated_at=? WHERE id=?",
                        (
                            new_version,
                            "QUEUED" if value.action == "adopt" or cfg.joint else "REVIEW_PENDING",
                            new_version if value.action == "adopt" else None,
                            now if value.action == "adopt" else None,
                            actor,
                            text,
                            "human" if value.text is not None else row["draft_origin"],
                            value.account_id or row["owner_account_id"],
                            now,
                            candidate_id,
                        ),
                    )
                    if cfg.joint:
                        execution = value.account_id or row["execution_account_id"]
                        db.execute(
                            "UPDATE hf_candidates SET execution_account_id=?,owner_account_id=? WHERE id=?",
                            (execution, execution, candidate_id),
                        )
                        if execution:
                            db.execute(
                                "UPDATE hf_processing_tasks SET execution_account_id=?,assignment_status='ASSIGNED',state='TASK_CREATED',version=version+1,assigned_at=COALESCE(assigned_at,?),updated_at=? WHERE id=?",
                                (execution, now, now, row["processing_task_id"]),
                            )
                            db.execute(
                                "INSERT OR IGNORE INTO hf_assignment_events VALUES(?,?,?,?,?,?,?)",
                                (
                                    digest([candidate_id, new_version, actor, execution]),
                                    row["processing_task_id"],
                                    new_version,
                                    execution,
                                    "ASSIGNED",
                                    "human_edit",
                                    now,
                                ),
                            )
                        self.queue.put(
                            db,
                            "generate" if execution else "assign",
                            candidate_id + "|" + str(new_version) + "|" + str(policy["version"]),
                        )
                    elif value.action == "adopt":
                        self.queue.put(
                            db,
                            "generate",
                            candidate_id + "|" + str(new_version) + "|" + str(policy["version"]),
                        )
                db.execute(
                    "INSERT INTO hf_decisions(candidate_id,version,action,actor,at,details) VALUES(?,?,?,?,?,?)",
                    (
                        candidate_id,
                        new_version,
                        value.action,
                        actor,
                        now,
                        canonical(
                            {
                                "account_id": value.account_id or row["owner_account_id"],
                                "draft_hash": digest(value.text)
                                if value.text is not None
                                else None,
                            }
                        ),
                    ),
                )
                rows.append(row)
        self.sync_processing()
        return [self.candidate(r["id"]) for r in rows]

    def _eligible(self, candidate, config, account):
        if not config.enabled:
            return {"status": "ineligible", "reasons": ["strategy_disabled"], "evidence": []}
        if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
            return {"status": "ineligible", "reasons": ["global_stop"], "evidence": []}
        from .pools import Pools

        profile = Pools(self.engine).profile(account["id"])["payload"]
        if candidate["classification"].get("language") not in profile["languages"] or (
            profile["allowed_targets"]
            and candidate["source"]["url"] not in profile["allowed_targets"]
        ):
            return {"status": "ineligible", "reasons": ["account_profile_mismatch"], "evidence": []}
        if (
            account["id"] not in self._execution_ids(config, candidate.get("primary_product_id"))
            or account["disabled"]
            or account["status"] != "healthy"
            or account["cooldown_until"] > self.clock()
        ):
            return {"status": "ineligible", "reasons": ["account_unavailable"], "evidence": []}
        try:
            classification = Classification.model_validate(
                {
                    k: v
                    for k, v in candidate["classification"].items()
                    if k in Classification.model_fields
                }
            )
            quality = self._quality(classification, candidate["source"], config)
        except ValueError:
            quality = "classification_unresolved"
        if quality:
            return {"status": "ineligible", "reasons": [quality], "evidence": []}
        if config.joint and not candidate["classification"].get("verified_fact_ids"):
            return {"status": "unknown", "reasons": ["answer_facts_missing"], "evidence": []}
        return self.io.eligibility(candidate, config, account)

    def _block(self, job, candidate, eligibility):
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            db.execute(
                "UPDATE hf_candidates SET channel_eligibility=?,block_reason=?,updated_at=? WHERE id=? AND version=?",
                (
                    eligibility["status"],
                    ",".join(eligibility.get("reasons", [])) or "eligibility_unknown",
                    self.clock(),
                    candidate["id"],
                    candidate["version"],
                ),
            )

    def _generate(self, job):
        candidate_id, version, _ = job["reference_id"].split("|", 2)
        candidate = self.candidate(candidate_id)
        policy = self.policy(candidate["policy_id"])
        config = InteractionConfig.model_validate(policy["config"])
        if config.run_mode == "discovery_only":
            return
        if candidate["version"] != int(version) or candidate["state"] not in {
            "QUEUED",
            "GENERATING",
        }:
            return
        if (
            candidate["priority"] not in {"P0", "P1"}
            and candidate["selected_version"] != candidate["version"]
        ):
            return
        if candidate["expires_at"] <= self.clock():
            return
        if config.joint and not candidate["execution_account_id"]:
            return
        account = self.engine.account(
            candidate["execution_account_id"] or candidate["owner_account_id"]
        )
        eligible = self._eligible(candidate, config, account)
        if eligible["status"] != "eligible":
            self._block(job, candidate, eligible)
            return self.clock() + 60 if config.joint else None
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            db.execute(
                "UPDATE hf_candidates SET state='GENERATING',channel_eligibility='eligible',block_reason=NULL WHERE id=? AND version=?",
                (candidate_id, candidate["version"]),
            )
        started = time.monotonic()
        if candidate["draft_origin"] == "human":
            result = {"text": candidate["draft"], "provider": "human"}
        elif candidate["draft"]:
            result = {"text": candidate["draft"], "provider": "saved_draft"}
        elif config.run_mode == "dry_run":
            result = {
                "text": candidate["classification"]["contribution"],
                "provider": "dry_run",
                "actual_cost": 0,
            }
        else:
            result = self.io.generate_text(candidate, config, account)
        text = result.get("text", "").strip()
        if not text:
            raise Conflict("empty_draft")
        from .backends.common import x_post_weight

        if x_post_weight(text) > 280:
            raise Conflict("draft_exceeds_connection_limit")
        verdict = (
            {"valid": True}
            if config.run_mode == "dry_run"
            else self.io.validate_text(candidate, text, config, account)
        )
        if verdict.get("valid") is not True:
            raise Conflict("draft_facts_or_provenance_invalid")
        plan = {
            "recommended": candidate["classification"]["recommended_media"],
            "source": "text",
            "assets": [],
            "real_product_output": False,
        }
        media_requested = plan["recommended"] in {"image", "video", "reuse_existing"}
        available = self.io.media_status().get("configured") is True
        if media_requested and not available:
            plan["reason"] = "factory_media_connector_unavailable_text_only"
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            current = db.execute(
                "SELECT version,state FROM hf_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if current["version"] != candidate["version"] or current["state"] != "GENERATING":
                return
            db.execute(
                "UPDATE hf_candidates SET draft=?,draft_origin=COALESCE(draft_origin,'model'),media_plan=?,state=?,updated_at=? WHERE id=?",
                (
                    text,
                    canonical(plan),
                    "GENERATING"
                    if media_requested and available and config.run_mode == "live"
                    else "READY",
                    self.clock(),
                    candidate_id,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO hf_generation(id,candidate_id,version,kind,state,provider,parameters,started_at,finished_at,actual_cost,asset) VALUES(?,?,?,'text','complete',?,?,?,?,?,?)",
                (
                    "text-" + digest([candidate_id, candidate["version"]])[:24],
                    candidate_id,
                    candidate["version"],
                    result.get("provider", "configured_model"),
                    canonical({"config_version": policy["version"]}),
                    self.clock(),
                    self.clock(),
                    result.get("actual_cost"),
                    canonical({"text": text, "validation": verdict}),
                ),
            )
            db.execute(
                "INSERT INTO hf_usage(id,project_id,kind,at,duration_ms,actual_cost,details) VALUES(?,?,'text',?,?,?,?)",
                (
                    uid("usage-"),
                    config.project_id,
                    self.clock(),
                    int((time.monotonic() - started) * 1000),
                    result.get("actual_cost"),
                    canonical({"candidate_id": candidate_id}),
                ),
            )
            self.queue.put(
                db,
                "media"
                if media_requested and available and config.run_mode == "live"
                else "publish",
                candidate_id + "|" + str(candidate["version"]),
            )

    def _media(self, job):
        candidate_id, version = job["reference_id"].split("|", 1)
        candidate = self.candidate(candidate_id)
        config = InteractionConfig.model_validate(self.policy(candidate["policy_id"])["config"])
        if config.run_mode == "discovery_only":
            return
        existing = self.store.one(
            "SELECT * FROM hf_generation WHERE candidate_id=? AND version=? AND kind<>'text'",
            (candidate_id, int(version)),
        )
        if (
            candidate["version"] != int(version)
            or candidate["state"] != "GENERATING"
            or not config.enabled
            or config.run_mode != "live"
        ) and not existing:
            return
        if (
            candidate["priority"] not in {"P0", "P1"}
            and candidate["selected_version"] != candidate["version"]
        ) and not existing:
            raise Conflict("paid_media_requires_selection")
        kind = existing["kind"] if existing else candidate["classification"]["recommended_media"]
        if kind not in {"image", "video", "reuse_existing"}:
            return
        now, resource = self.clock(), "hf-media:" + config.project_id
        generation_id = (
            existing["id"]
            if existing
            else "media-" + digest([candidate_id, int(version), kind])[:24]
        )
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            # Supplier uncertainty outlives local leases and candidate TTL. It keeps the project slot.
            external_pending = db.execute(
                "SELECT g.id FROM hf_generation g JOIN hf_candidates c ON c.id=g.candidate_id WHERE c.project_id=? AND g.kind<>'text' AND g.state IN ('submitting','pending','unknown') AND g.id<>? LIMIT 1",
                (config.project_id, generation_id),
            ).fetchone()
            if external_pending:
                return now + config.publishing.retry_after_confirmed_not_submitted_seconds
            occupied = db.execute(
                "SELECT * FROM execution_resources WHERE resource=? AND expires_at>? AND owner<>?",
                (resource, now, generation_id),
            ).fetchone()
            if occupied:
                return now + config.publishing.retry_after_confirmed_not_submitted_seconds
            old = db.execute("SELECT * FROM hf_generation WHERE id=?", (generation_id,)).fetchone()
            if old and old["state"] in {"complete", "not_submitted"}:
                return
            if not old:
                if candidate["expires_at"] - now < config.generation.min_remaining_seconds:
                    raise Conflict("insufficient_remaining_media_time")
                daily_budget = (
                    config.generation.daily_image_budget
                    if kind == "image"
                    else config.generation.daily_video_budget
                )
                if kind != "reuse_existing" and (
                    daily_budget is None or config.generation.daily_cost_budget is None
                ):
                    raise Conflict("media_budget_unconfigured")
                count = db.execute(
                    "SELECT count(*) FROM hf_usage WHERE project_id=? AND kind=? AND at>=?",
                    (config.project_id, kind, now - now % 86400),
                ).fetchone()[0]
                cost = db.execute(
                    "SELECT SUM(COALESCE(actual_cost,estimated_cost)),SUM(CASE WHEN actual_cost IS NULL AND estimated_cost IS NULL THEN 1 ELSE 0 END) FROM hf_usage WHERE project_id=? AND kind IN ('image','video','text') AND at>=?",
                    (config.project_id, now - now % 86400),
                ).fetchone()
                estimate = getattr(self.io, "media_estimate", lambda *args: None)(
                    candidate, config, kind
                )
                if kind != "reuse_existing" and (
                    count >= daily_budget
                    or estimate is None
                    or cost[1]
                    or (cost[0] or 0) + estimate > config.generation.daily_cost_budget
                ):
                    raise Conflict("media_budget_exhausted_or_cost_unknown")
                db.execute(
                    "INSERT INTO hf_generation(id,candidate_id,version,kind,state,provider,parameters,started_at,estimated_cost) VALUES(?,?,?,?,'submitting','configured_media',?,?,?)",
                    (
                        generation_id,
                        candidate_id,
                        int(version),
                        kind,
                        canonical(
                            {"config": config.model_dump(), "idempotency_key": generation_id}
                        ),
                        now,
                        estimate,
                    ),
                )
                db.execute(
                    "INSERT INTO hf_usage(id,project_id,kind,at,estimated_cost,details) VALUES(?,?,?,?,?,?)",
                    (
                        generation_id,
                        config.project_id,
                        kind,
                        now,
                        estimate,
                        canonical({"candidate_id": candidate_id}),
                    ),
                )
            db.execute(
                "INSERT INTO execution_resources VALUES(?,?,?) ON CONFLICT(resource) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                (
                    resource,
                    generation_id,
                    now
                    + max(
                        config.generation.image_timeout_seconds,
                        config.generation.video_timeout_seconds,
                    )
                    + config.scan.lease_seconds,
                ),
            )
        try:
            result = (
                self.io.query_media(old["provider_job_id"] or generation_id, config)
                if old
                else self.io.submit_media(candidate, config, kind, generation_id)
            )
        except Exception:
            result = {"state": "unknown"}
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            # Even stale candidates keep their provider identity and actual cost for reconciliation.
            state = result.get("state", "unknown")
            db.execute(
                "UPDATE hf_generation SET state=?,provider_job_id=COALESCE(?,provider_job_id),actual_cost=?,source=?,real_product_output=?,asset=? WHERE id=?",
                (
                    state,
                    result.get("job_id"),
                    result.get("actual_cost"),
                    result.get("source", "external_illustration"),
                    bool(result.get("real_product_output") and result.get("product_evidence")),
                    canonical(result.get("assets", [])),
                    generation_id,
                ),
            )
            db.execute(
                "UPDATE hf_usage SET actual_cost=? WHERE id=?",
                (result.get("actual_cost"), generation_id),
            )
            if state == "complete":
                assets = result.get("assets", [])
                if not assets:
                    raise Conflict("media_asset_missing")
                for asset in assets:
                    self.engine.verify_media(asset)
                db.execute(
                    "UPDATE hf_generation SET finished_at=? WHERE id=?",
                    (self.clock(), generation_id),
                )
                db.execute(
                    "DELETE FROM execution_resources WHERE resource=? AND owner=?",
                    (resource, generation_id),
                )
                changed = db.execute(
                    "UPDATE hf_candidates SET media_plan=?,state='READY' WHERE id=? AND version=? AND state='GENERATING' AND expires_at>?",
                    (
                        canonical(
                            {
                                "assets": assets,
                                "provider_job_id": result.get("job_id"),
                                "source": result.get("source", "external_illustration"),
                                "real_product_output": bool(
                                    result.get("real_product_output")
                                    and result.get("product_evidence")
                                ),
                            }
                        ),
                        candidate_id,
                        int(version),
                        self.clock(),
                    ),
                ).rowcount
                if changed:
                    self.queue.put(db, "publish", candidate_id + "|" + version)
                return
            if state == "not_submitted":
                # No new order is made implicitly; a failed media plan falls back to the useful text.
                db.execute(
                    "DELETE FROM execution_resources WHERE resource=? AND owner=?",
                    (resource, generation_id),
                )
                db.execute(
                    "UPDATE hf_candidates SET state='READY',block_reason='media_not_submitted_text_only' WHERE id=? AND version=? AND state='GENERATING'",
                    (candidate_id, int(version)),
                )
                self.queue.put(db, "publish", candidate_id + "|" + version)
                return
        timeout = (
            config.generation.image_timeout_seconds
            if kind == "image"
            else config.generation.video_timeout_seconds
        )
        if self.clock() - (existing["started_at"] if existing else now) >= timeout:
            with self.store.connect(write=True) as db:
                self.queue.guard(db, job)
                db.execute(
                    "UPDATE hf_candidates SET block_reason='media_outcome_unknown_attention' WHERE id=? AND version=? AND state='GENERATING'",
                    (candidate_id, int(version)),
                )
            # Keep supplier/job record and project slot; a later explicit query can reconcile it.
            return
        return self.clock() + config.verification.backoff_seconds[0]

    def _recover_handoff(self, db, candidate):
        """Adopt only an existing attempted handoff, before considering TTL/pacing.

        No platform call or new attempt is made. A conflicting orphan stays blocked.
        """
        task = db.execute(
            "SELECT * FROM tasks WHERE name=? ORDER BY created_at LIMIT 1",
            ("HF " + candidate["id"] + " v" + str(candidate["version"]),),
        ).fetchone()
        if not task:
            return False
        item = db.execute(
            "SELECT * FROM items WHERE task_id=? ORDER BY position LIMIT 1", (task["id"],)
        ).fetchone()
        if not item or (item["status"] == "not_attempted" and not item["attempt_id"]):
            return False
        payload = json.loads(item["payload"])
        source = (
            candidate["source"]
            if isinstance(candidate["source"], dict)
            else json.loads(candidate["source"])
        )
        media = (
            candidate["media_plan"]
            if isinstance(candidate["media_plan"], dict)
            else json.loads(candidate["media_plan"])
        )
        manifest = json.loads(task["manifest"]) if task["manifest"] else None
        intent = db.execute(
            "SELECT candidate_id,item_id FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind='reply'",
            (candidate["project_id"], candidate["platform"], candidate["source_event_id"]),
        ).fetchone()
        matches = (
            intent
            and intent["candidate_id"] == candidate["id"]
            and intent["item_id"] in {None, item["id"]}
            and item["account_id"] == candidate["owner_account_id"]
            and payload.get("account_id") == candidate["owner_account_id"]
            and payload.get("action") == "reply"
            and payload.get("target") == source["url"]
            and payload.get("text") == candidate["draft"]
            and payload.get("media") == media.get("assets", [])
            and digest(payload) == item["payload_hash"]
            and manifest is not None
            and digest(manifest) == task["manifest_hash"]
            and payload in manifest.get("items", [])
            and json.loads(task["policy"]).get("project_id", "default") == candidate["project_id"]
        )
        if not matches:
            db.execute(
                "UPDATE hf_candidates SET state='VERIFYING',block_reason='handoff_binding_unverified' WHERE id=?",
                (candidate["id"],),
            )
            return True
        db.execute(
            "UPDATE hf_candidates SET task_id=?,item_id=?,state=?,block_reason='existing_attempt_recovered' WHERE id=?",
            (
                task["id"],
                item["id"],
                "PUBLISHING" if item["status"] == "executing" else "VERIFYING",
                candidate["id"],
            ),
        )
        db.execute(
            "UPDATE interaction_intents SET item_id=? WHERE candidate_id=?",
            (item["id"], candidate["id"]),
        )
        return True

    def _publish(self, job):
        candidate_id, version = job["reference_id"].split("|", 1)
        candidate = self.candidate(candidate_id)
        policy = self.policy(candidate["policy_id"])
        config = InteractionConfig.model_validate(policy["config"])
        if config.run_mode == "discovery_only":
            return
        if candidate["version"] != int(version) or candidate["state"] != "READY":
            return
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            if self._recover_handoff(db, candidate):
                return
        if config.joint and not candidate["execution_account_id"]:
            return
        account = self.engine.account(
            candidate["execution_account_id"] or candidate["owner_account_id"]
        )
        if candidate["expires_at"] <= self.clock():
            return
        if (
            candidate["priority"] not in {"P0", "P1"}
            and candidate["selected_version"] != candidate["version"]
        ):
            return
        eligibility = self._eligible(candidate, config, account)
        if eligibility["status"] != "eligible":
            self._block(job, candidate, eligibility)
            return self.clock() + 60 if config.joint else None
        current = self.io.revalidate(candidate, config, account)
        if (
            current.get("status") != "valid"
            or current.get("source_hash") != candidate["source_hash"]
        ):
            self._block(
                job,
                candidate,
                {
                    "status": "ineligible",
                    "reasons": [
                        "source_deleted_or_context_changed"
                        if current.get("status") != "unknown"
                        else "source_revalidation_unknown"
                    ],
                },
            )
            return
        if config.run_mode == "dry_run":
            with self.store.connect(write=True) as db:
                self.queue.guard(db, job)
                db.execute(
                    "UPDATE hf_candidates SET state='SIMULATED',external_reference=?,updated_at=? WHERE id=? AND version=? AND state='READY'",
                    (
                        "simulation://dry-run/" + candidate_id,
                        self.clock(),
                        candidate_id,
                        candidate["version"],
                    ),
                )
            return
        if not within_hours(self.clock(), config.timezone, config.publish_hours):
            return self.clock() + config.publishing.retry_after_confirmed_not_submitted_seconds
        if account["mode"] == "live" and (
            not self.engine.live_enabled or not self.engine.adapters.commissioned(account)
        ):
            self._block(
                job,
                candidate,
                {"status": "ineligible", "reasons": ["live_writes_disabled_or_not_commissioned"]},
            )
            return
        verdict = self.io.validate_text(candidate, candidate["draft"], config, account)
        if verdict.get("valid") is not True:
            raise Conflict("draft_facts_or_provenance_invalid")
        # Reserve the same global intent used by all legacy attempts before creating a sender task.
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            row = db.execute("SELECT * FROM hf_candidates WHERE id=?", (candidate_id,)).fetchone()
            if row["state"] != "READY" or row["version"] != candidate["version"]:
                return
            timing = db.execute(
                "SELECT not_before FROM hf_enqueue_times WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if not timing:
                # The first durable handoff reserves a single draw. Never redraw on restart,
                # duplicate routing, lease recovery or a definitive no-submit retry.
                sampled_at = self.clock()
                if candidate["item_id"]:
                    # An already frozen manifest is immutable; upgrades never reschedule it.
                    not_before = db.execute(
                        "SELECT due_at FROM items WHERE id=?", (candidate["item_id"],)
                    ).fetchone()[0]
                    delay = 0
                else:
                    delay = self.draw_delay(
                        config.publishing.enqueue_delay_min_seconds,
                        config.publishing.enqueue_delay_max_seconds,
                    )
                    not_before = max(sampled_at + delay, account["cooldown_until"])
                db.execute(
                    "INSERT INTO hf_enqueue_times VALUES(?,?,?,?,?)",
                    (candidate_id, sampled_at, delay, not_before, policy["version"]),
                )
            else:
                not_before = timing[0]
            scheduled_at = not_before
            from .readiness import DELIVERY_GRACE_SECONDS

            if self.clock() > scheduled_at + DELIVERY_GRACE_SECONDS:
                db.execute(
                    "UPDATE hf_candidates SET state='CLOSED',closed_reason='enqueue_window_elapsed',updated_at=? WHERE id=?",
                    (self.clock(), candidate_id),
                )
                return
            if scheduled_at >= candidate["expires_at"]:
                db.execute(
                    "UPDATE hf_candidates SET state='CLOSED',closed_reason='expires_before_enqueue',updated_at=? WHERE id=?",
                    (self.clock(), candidate_id),
                )
                return
            intent = db.execute(
                "SELECT * FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind='reply'",
                (config.scope_id, config.platform, candidate["source_event_id"]),
            ).fetchone()
            from .interaction_intents import historical_sender

            old_sender = historical_sender(
                db, config.scope_id, config.platform, candidate["source_event_id"]
            )
            linked = (
                intent["item_id"]
                if intent and intent["item_id"]
                else old_sender["id"]
                if old_sender
                else None
            )
            if linked and linked != row["item_id"]:
                item = db.execute("SELECT * FROM items WHERE id=?", (linked,)).fetchone()
                if item:
                    db.execute(
                        "UPDATE hf_candidates SET task_id=?,item_id=?,execution_account_id=?,owner_account_id=?,block_reason='existing_sender_reused' WHERE id=?",
                        (
                            item["task_id"],
                            item["id"],
                            item["account_id"],
                            item["account_id"],
                            candidate_id,
                        ),
                    )
                    db.execute(
                        "UPDATE hf_processing_tasks SET execution_account_id=?,assignment_status='ASSIGNED',task_id=?,item_id=?,assigned_at=COALESCE(assigned_at,?) WHERE id=?",
                        (
                            item["account_id"],
                            item["task_id"],
                            item["id"],
                            self.clock(),
                            row["processing_task_id"],
                        ),
                    )
                    return
            if intent and intent["candidate_id"] != candidate_id:
                db.execute(
                    "UPDATE hf_candidates SET block_reason='interaction_already_claimed' WHERE id=?",
                    (candidate_id,),
                )
                return
            db.execute(
                "INSERT OR IGNORE INTO interaction_intents(project_id,platform,source_event_id,action_kind,candidate_id,state,created_at) VALUES(?,?,?,'reply',?,'reserved',?)",
                (
                    config.scope_id,
                    config.platform,
                    candidate["source_event_id"],
                    candidate_id,
                    self.clock(),
                ),
            )
        task_name = "HF " + candidate_id + " v" + str(candidate["version"])
        task = self.engine.task(candidate["task_id"]) if candidate["task_id"] else None
        if not task:
            # Recover an orphan draft if the process died between task creation and linking.
            found = self.store.one(
                "SELECT id FROM tasks WHERE name=? ORDER BY created_at LIMIT 1", (task_name,)
            )
            task = (
                self.engine.task(found["id"])
                if found
                else self.engine.create_task(
                    TaskInput(
                        name=task_name,
                        account_ids=[account["id"]],
                        policy={
                            "project_id": config.project_id,
                            "starts_at": iso(min(self.clock(), scheduled_at) - 1),
                            "ends_at": iso(candidate["expires_at"]),
                            "min_interval": max(
                                120 if account["mode"] == "live" else 0,
                                account["min_interval"],
                                config.publishing.min_account_interval_seconds or 0,
                            ),
                            "daily_limit": min(
                                account["daily_limit"],
                                config.publishing.max_per_account_day or account["daily_limit"],
                            ),
                            "account_hourly_limit": config.publishing.max_per_account_hour,
                            "account_daily_limit": config.publishing.max_per_account_day,
                            "target_allowlist": [candidate["source"]["url"]],
                            "languages": config.languages,
                        },
                    ),
                    interaction_claim={
                        "candidate_id": candidate_id,
                        "version": candidate["version"],
                        "job_id": job["id"],
                        "job_owner": job["owner"],
                    },
                )
            )
        if not task["items"]:
            task = self.engine.set_items(
                task["id"],
                [
                    ItemInput(
                        account_id=account["id"],
                        action="reply",
                        target=candidate["source"]["url"],
                        text=candidate["draft"],
                        media=candidate["media_plan"].get("assets", []),
                        scheduled_at=iso(scheduled_at),
                        recipient_id=candidate["source"].get("author_id", ""),
                    )
                ],
            )
        item = task["items"][0]
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            row = db.execute(
                "SELECT version,state FROM hf_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if row["version"] != candidate["version"] or row["state"] != "READY":
                return
            db.execute(
                "UPDATE hf_candidates SET task_id=?,item_id=?,channel_eligibility='eligible',block_reason=NULL WHERE id=?",
                (task["id"], item["id"], candidate_id),
            )
            db.execute(
                "UPDATE interaction_intents SET item_id=? WHERE candidate_id=?",
                (item["id"], candidate_id),
            )
        self.engine.start_task(
            task["id"], source="high_frequency_policy", actor="policy:" + policy["id"]
        )
        from .queue_store import JobQueue

        JobQueue(self.engine).sync(task["id"])
        # The existing sender/pools own submission; this worker never invokes an execute adapter.

    def sync_results(self):
        now = self.clock()
        with self.store.connect(write=True) as db:
            for row in db.execute(
                "SELECT c.*,i.status item_status,i.external_reference item_reference,i.result item_result,i.claimed_at,i.attempt_id FROM hf_candidates c JOIN items i ON i.id=c.item_id WHERE c.state NOT IN ('PUBLISHED','SIMULATED','CLOSED')"
            ).fetchall():
                state = row["item_status"]
                if state == "not_attempted":
                    reason = json.loads(row["item_result"]).get("reason")
                    if reason:
                        db.execute(
                            "UPDATE hf_candidates SET block_reason=? WHERE id=?",
                            (reason, row["id"]),
                        )
                    continue
                if state == "executing":
                    db.execute(
                        "UPDATE hf_candidates SET state='PUBLISHING',verification_started_at=COALESCE(verification_started_at,?) WHERE id=?",
                        (row["claimed_at"] or now, row["id"]),
                    )
                elif state in {"unknown", "scheduled"}:
                    db.execute(
                        "UPDATE hf_candidates SET state='VERIFYING',verification_started_at=COALESCE(verification_started_at,?),block_reason='publication_outcome_unknown' WHERE id=?",
                        (row["claimed_at"] or now, row["id"]),
                    )
                    self.queue.put(db, "verify", row["id"])
                elif state == "confirmed":
                    result = json.loads(row["item_result"])
                    if result.get("verified") is not True or not row["item_reference"]:
                        continue
                    simulated = row["item_reference"].startswith("simulation://")
                    evidence = db.execute(
                        "SELECT published_at FROM publication_evidence WHERE item_id=?",
                        (row["item_id"],),
                    ).fetchone()
                    published = evidence[0] if evidence else None
                    db.execute(
                        "UPDATE hf_candidates SET state=?,external_reference=?,published_at=?,block_reason=NULL,updated_at=? WHERE id=?",
                        (
                            "SIMULATED" if simulated else "PUBLISHED",
                            row["item_reference"],
                            published,
                            now,
                            row["id"],
                        ),
                    )
                    db.execute(
                        "UPDATE interaction_intents SET state=? WHERE candidate_id=?",
                        ("simulated" if simulated else "published", row["id"]),
                    )
                    if not simulated and published:
                        cfg = InteractionConfig.model_validate_json(
                            db.execute(
                                "SELECT config FROM hf_policies WHERE id=?", (row["policy_id"],)
                            ).fetchone()[0]
                        )
                        for offset in cfg.observation_offsets_seconds:
                            db.execute(
                                "INSERT OR IGNORE INTO hf_observations(candidate_id,offset_seconds,due_at) VALUES(?,?,?)",
                                (row["id"], offset, published + offset),
                            )
                else:
                    result = json.loads(row["item_result"])
                    cfg = InteractionConfig.model_validate_json(
                        db.execute(
                            "SELECT config FROM hf_policies WHERE id=?", (row["policy_id"],)
                        ).fetchone()[0]
                    )
                    retried = db.execute(
                        "SELECT count(*) FROM hf_publish_retries WHERE candidate_id=?", (row["id"],)
                    ).fetchone()[0]
                    if (
                        state == "failed_known"
                        and result.get("confirmed_not_submitted") is True
                        and retried < cfg.publishing.max_retries_after_confirmed_not_submitted
                        and row["expires_at"] > now
                        and cfg.enabled
                        and cfg.run_mode == "live"
                    ):
                        db.execute(
                            "INSERT OR IGNORE INTO hf_publish_retries VALUES(?,?,?,?)",
                            (row["id"], retried + 1, row["item_id"], now),
                        )
                        db.execute(
                            "UPDATE hf_candidates SET state='READY',version=version+1,selected_version=CASE WHEN selected_version=version THEN version+1 ELSE NULL END,task_id=NULL,item_id=NULL,verification_started_at=NULL,verification_polls=0,block_reason='confirmed_not_submitted_retry' WHERE id=?",
                            (row["id"],),
                        )
                        db.execute(
                            "UPDATE interaction_intents SET state='reserved',item_id=NULL WHERE candidate_id=?",
                            (row["id"],),
                        )
                        self.queue.put(
                            db,
                            "publish",
                            row["id"] + "|" + str(row["version"] + 1),
                            at=now + cfg.publishing.retry_after_confirmed_not_submitted_seconds,
                        )
                        continue
                    db.execute(
                        "UPDATE hf_candidates SET state='CLOSED',closed_reason=?,block_reason=?,updated_at=? WHERE id=?",
                        (state, json.loads(row["item_result"]).get("reason"), now, row["id"]),
                    )
        self.sync_processing()

    def _verify(self, job):
        candidate = self.candidate(job["reference_id"])
        if candidate["state"] not in {"PUBLISHING", "VERIFYING"}:
            return
        config = InteractionConfig.model_validate(self.policy(candidate["policy_id"])["config"])
        started = candidate["verification_started_at"] or self.clock()
        if self.clock() - started >= config.verification.attention_after_seconds and not json.loads(
            job["result"]
        ).get("manual"):
            with self.store.connect(write=True) as db:
                self.queue.guard(db, job)
                db.execute(
                    "UPDATE hf_candidates SET attention_at=COALESCE(attention_at,?),state='VERIFYING',block_reason='verification_attention_no_resend' WHERE id=?",
                    (self.clock(), candidate["id"]),
                )
                # One fault record, visible in the same durable workbench outbox.
                key = "verify-attention:" + candidate["id"]
                db.execute(
                    "INSERT OR IGNORE INTO hf_notices(id,policy_id,dedupe_key,channel,notice_type,state,snapshot,body,created_at,delivered_at,message_id) VALUES(?,?,?,'workbench','fault','sent',?,?,?,?,?)",
                    (
                        "notice-" + digest(key)[:24],
                        candidate["policy_id"],
                        key,
                        canonical([{"candidate_id": candidate["id"]}]),
                        "发布结果仍不确定，禁止补发或换号；请核验原记录。",
                        self.clock(),
                        self.clock(),
                        "workbench://" + key,
                    ),
                )
            return
        self.engine.reconcile(candidate["item_id"])
        self.sync_results()
        current = self.candidate(candidate["id"])
        if current["state"] == "VERIFYING":
            with self.store.connect(write=True) as db:
                self.queue.guard(db, job)
                db.execute(
                    "UPDATE hf_candidates SET verification_polls=verification_polls+1 WHERE id=?",
                    (candidate["id"],),
                )
            return (
                self.clock()
                + config.verification.backoff_seconds[
                    min(
                        candidate["verification_polls"],
                        len(config.verification.backoff_seconds) - 1,
                    )
                ]
            )

    def _observe(self, job):
        candidate_id, offset = job["reference_id"].split("|", 1)
        candidate = self.candidate(candidate_id)
        config = InteractionConfig.model_validate(self.policy(candidate["policy_id"])["config"])
        result = self.io.observe(candidate, config)
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            db.execute(
                "UPDATE hf_observations SET state='observed',observed_at=?,metrics=? WHERE candidate_id=? AND offset_seconds=? AND state='pending'",
                (self.clock(), canonical(result), candidate_id, int(offset)),
            )

    def run_stage(self, stage, *, max_jobs=10):
        if stage not in self.stages:
            raise Conflict("未知工作阶段")
        processed = 0
        for _ in range(max_jobs):
            job = self.queue.claim(stage)
            if not job:
                break
            config = self._job_config(stage, job)
            with self.store.connect(write=True) as db:
                self.queue.guard(db, job)
                db.execute(
                    "UPDATE execution_jobs SET lease_until=? WHERE id=? AND owner=?",
                    (self.clock() + config.scan.lease_seconds, job["id"], job["owner"]),
                )
            try:
                with self.queue.heartbeat(
                    job, seconds=config.scan.lease_seconds, interval=config.scan.heartbeat_seconds
                ):
                    retry = getattr(self, "_" + stage)(job)
                    with self.store.connect(write=True) as db:
                        self.queue.settle(db, job, retry_at=retry)
            except LostLease:
                continue
            except Exception as exc:
                # Do not log exception messages: providers may embed tokens or response bodies.
                safe_code = getattr(exc, "safe_code", None)
                reason = (
                    safe_code
                    if safe_code
                    in {
                        "XAPISearchResponseInvalid",
                        "XAPITransportOutcomeUnknown",
                        "XAPIOutcomeUnknown",
                        "XAPITokenMissing",
                    }
                    else (str(exc) if isinstance(exc, Conflict) else type(exc).__name__)
                )
                if len(reason) > 120:
                    reason = type(exc).__name__
                try:
                    with self.store.connect(write=True) as db:
                        self.queue.guard(db, job)
                        retry = None
                        if stage in {"scan", "readonly_scan"}:
                            run = db.execute(
                                "SELECT * FROM hf_scan_runs WHERE id=?", (job["reference_id"],)
                            ).fetchone()
                            db.execute(
                                "UPDATE hf_scan_runs SET state='failed',coverage_incomplete=1,error=?,finished_at=? WHERE id=?",
                                (reason, self.clock(), job["reference_id"]),
                            )
                            self._release_scan(db, run)
                        elif stage in {"classify", "readonly_classify"}:
                            run_id, candidate_id, _ = job["reference_id"].split("|", 2)
                            db.execute(
                                "UPDATE hf_scan_candidates SET error=? WHERE run_id=? AND candidate_id=? AND state='pending'",
                                (reason, run_id, candidate_id),
                            )
                            db.execute(
                                "UPDATE hf_candidates SET block_reason=? WHERE id=? AND state NOT IN ('CLOSED','PUBLISHED','SIMULATED','PUBLISHING','VERIFYING')",
                                (reason, candidate_id),
                            )
                            db.execute(
                                "UPDATE hf_candidates SET classification_status=? WHERE id=? AND state NOT IN ('CLOSED','PUBLISHED','SIMULATED','PUBLISHING','VERIFYING')",
                                (
                                    "pending_facts"
                                    if "fact" in reason
                                    else "pending_context"
                                    if "context" in reason
                                    else "model_failed",
                                    candidate_id,
                                ),
                            )
                            attempts = json.loads(job["result"]).get("retry_count", 0) + 1
                            if attempts <= config.generation.classification_max_retries:
                                retry = self.clock() + config.generation.retry_after_failure_seconds
                        elif stage in {"generate", "media", "publish"}:
                            candidate_id = job["reference_id"].split("|", 1)[0]
                            db.execute(
                                "UPDATE hf_candidates SET block_reason=? WHERE id=? AND state NOT IN ('CLOSED','PUBLISHED','SIMULATED','PUBLISHING','VERIFYING')",
                                (reason, candidate_id),
                            )
                        elif stage == "verify":
                            retry = (
                                self.clock()
                                + config.verification.backoff_seconds[
                                    min(1, len(config.verification.backoff_seconds) - 1)
                                ]
                            )
                        self.queue.settle(
                            db,
                            job,
                            retry_at=retry,
                            error=reason,
                            retry_count=json.loads(job["result"]).get("retry_count", 0) + 1,
                        )
                        Store.audit(
                            db,
                            "interaction.stage_error",
                            {"stage": stage, "job_id": job["id"], "reason": reason},
                            now=self.clock(),
                        )
                except LostLease:
                    pass
            processed += 1
        self.sync_processing()
        return {"stage": stage, "processed": processed}

    def _job_config(self, stage, job):
        reference = job["reference_id"]
        if stage in {"scan", "readonly_scan"}:
            row = self.store.one("SELECT config FROM hf_scan_runs WHERE id=?", (reference,))
        elif stage == "notify":
            row = self.store.one(
                "SELECT p.config FROM hf_policies p JOIN hf_notices n ON n.policy_id=p.id WHERE n.id=?",
                (reference,),
            )
        else:
            candidate_id = (
                reference.split("|", 2)[1]
                if stage in {"classify", "readonly_classify"}
                else reference.split("|", 1)[0]
            )
            row = self.store.one(
                "SELECT p.config FROM hf_policies p JOIN hf_candidates c ON c.policy_id=p.id WHERE c.id=?",
                (candidate_id,),
            )
        return InteractionConfig.model_validate_json(row["config"]) if row else InteractionConfig()

    def metrics(self, policy_id):
        rows = self.store.rows(
            "SELECT DISTINCT c.* FROM hf_candidates c LEFT JOIN hf_discoveries d ON d.candidate_id=c.id WHERE c.policy_id=? OR d.policy_id=?",
            (policy_id, policy_id),
        )
        tasks = self.store.rows(
            "SELECT DISTINCT t.* FROM hf_processing_tasks t LEFT JOIN hf_discoveries d ON d.candidate_id=t.candidate_id WHERE t.policy_id=? OR d.policy_id=?",
            (policy_id, policy_id),
        )
        runs = self.store.rows(
            "SELECT actual_started_at FROM hf_scan_runs WHERE policy_id=? AND actual_started_at IS NOT NULL ORDER BY seq",
            (policy_id,),
        )
        reviewed = [r for r in rows if r["priority"] in {"P2", "P3"}]
        selected = sum(r["selected_at"] is not None for r in reviewed)
        return {
            "candidate_count": len(rows),
            "qualified_p0_p1": sum(r["priority"] in {"P0", "P1"} for r in rows),
            "duplicates": self.store.one(
                "SELECT COALESCE(sum(duplicates),0) n FROM hf_scan_runs WHERE policy_id=?",
                (policy_id,),
            )["n"],
            "processing_count": len(tasks),
            "assigned": sum(t["assignment_status"] == "ASSIGNED" for t in tasks),
            "waiting_assignment": sum(t["assignment_status"] == "WAITING_ACCOUNT" for t in tasks),
            "qualified_to_task_seconds": [t["created_at"] - t["qualified_at"] for t in tasks],
            "qualified_to_assignment_seconds": [
                t["assigned_at"] - t["qualified_at"] for t in tasks if t["assigned_at"]
            ],
            "actual_start_intervals_seconds": [
                b["actual_started_at"] - a["actual_started_at"] for a, b in zip(runs, runs[1:])
            ],
            "published": sum(r["state"] == "PUBLISHED" for r in rows),
            "verifying": sum(r["state"] == "VERIFYING" for r in rows),
            "manual_adoption_rate": selected / len(reviewed) if reviewed else None,
            "expired_waiting_review": sum(
                r["closed_reason"] == "expired_waiting_review" for r in reviewed
            ),
            "discovery_latency_seconds": [r["first_seen_at"] - r["source_event_at"] for r in rows],
            "discovery_to_publication_seconds": [
                r["published_at"] - r["first_seen_at"] for r in rows if r["published_at"]
            ],
            "manual_wait_seconds": [
                r["selected_at"] - r["first_seen_at"] for r in rows if r["selected_at"]
            ],
            "views": None,
            "related_dialogues": None,
            "visits": None,
            "installs": None,
            "first_generations": None,
            "attribution_status": "unconnected",
            "observations": [
                decode(r, "metrics")
                for r in self.store.rows(
                    "SELECT o.* FROM hf_observations o JOIN hf_candidates c ON c.id=o.candidate_id WHERE c.policy_id=?",
                    (policy_id,),
                )
            ],
        }
