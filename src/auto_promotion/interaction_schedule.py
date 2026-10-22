"""Atomic, persistent scan starts and explicit schedule changes."""

import time
import uuid

from .core import Conflict
from .interaction_joint import account_identity
from .interaction_models import InteractionConfig
from .models import canonical, digest
from .store import Store


class InteractionSchedule:
    def trigger(self, policy_id, *, manual=False, request_key=None):
        from .interactions import within_hours

        now = self.clock()
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM hf_policies WHERE id=?", (policy_id,)).fetchone()
            if not row:
                raise Conflict("策略不存在")
            config = InteractionConfig.model_validate_json(row["config"])
            if config.scan.source == "imported_results":
                raise Conflict("manual_import_required:no_live_browser_scan")
            if not config.enabled:
                raise Conflict("策略已停用；请先启用或使用隔离模拟")
            if manual and request_key:
                if len(request_key) > 200:
                    raise Conflict("手动请求标识过长")
                request = db.execute(
                    "SELECT * FROM hf_manual_triggers WHERE policy_id=? AND request_key=?",
                    (policy_id, request_key),
                ).fetchone()
                if request and request["run_id"]:
                    return {"run_id": request["run_id"], "coalesced": True, "idempotent": True}
                db.execute(
                    "INSERT OR IGNORE INTO hf_manual_triggers VALUES(?,?,NULL,?)",
                    (policy_id, request_key, now),
                )
            manual = manual or bool(row["pending_manual"])
            if row["active_run_id"]:
                db.execute(
                    "UPDATE hf_policies SET pending_trigger=1,pending_manual=MAX(pending_manual,?) WHERE id=?",
                    (manual, policy_id),
                )
                return {"run_id": row["active_run_id"], "coalesced": True}
            if not manual and row["next_scan_at"] > now:
                return {"due": False}
            if row["deferred_until"] > now:
                if manual:
                    db.execute("UPDATE hf_policies SET pending_manual=1 WHERE id=?", (policy_id,))
                return {
                    "due": False,
                    "reason": row["deferred_reason"],
                    "deferred_until": row["deferred_until"],
                }
            account, reason, until = self._search_choice(db, row, config, now)
            if not within_hours(now, config.timezone, config.scan_hours):
                reason, until = "outside_scan_hours", now + 60
            used = db.execute(
                "SELECT count(*) FROM hf_usage WHERE project_id=? AND kind='read' AND at>=?",
                (config.project_id, now - now % 86400),
            ).fetchone()[0]
            if used >= config.scan.max_reads_per_day:
                reason, until = "read_budget_exhausted", now - now % 86400 + 86400
            if reason:
                db.execute(
                    "UPDATE hf_policies SET deferred_until=?,deferred_reason=?,pending_manual=MAX(pending_manual,?) WHERE id=?",
                    (until, reason, manual, policy_id),
                )
                # Deduplicated local fault notice. No account/cursor/sample is consumed.
                key = "scan-wait:" + policy_id + ":" + reason
                db.execute(
                    "INSERT OR IGNORE INTO hf_notices(id,policy_id,dedupe_key,channel,notice_type,state,snapshot,body,created_at,delivered_at,message_id) VALUES(?,?,?,'workbench','fault','sent','[]',?,?,?,?)",
                    (
                        "wait-" + digest(key)[:24],
                        policy_id,
                        key,
                        reason,
                        now,
                        now,
                        "workbench://interactions/" + policy_id,
                    ),
                )
                return {"due": False, "reason": reason, "deferred_until": until}
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM hf_scan_runs WHERE policy_id=?", (policy_id,)
            ).fetchone()[0]
            run_id = "scan-" + uuid.uuid4().hex[:24]
            interval = (
                self.randint(config.scan.min_interval_seconds, config.scan.max_interval_seconds)
                if config.scan.schedule_mode == "random_interval"
                else config.scan.interval_seconds
            )
            next_at = now + interval
            if (
                row["last_complete_at"]
                and now - row["last_complete_at"] > config.scan.long_gap_reset_seconds
            ):
                db.execute("UPDATE hf_policies SET empty_streak=0 WHERE id=?", (policy_id,))
            pool = [
                dict(id=a, identity=account_identity(self.engine.account(a)))
                for a in config.account_ids
            ]
            db.execute(
                "INSERT INTO hf_scan_runs(id,policy_id,seq,config_version,count_epoch,config,upper_at,started_at,deadline_at,scheduled_at,actual_started_at,sampled_interval_seconds,next_scan_at,schedule_config_version,search_account_id,account_pool_snapshot,trigger_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    policy_id,
                    seq,
                    row["version"],
                    row["count_epoch"],
                    row["config"],
                    now,
                    now,
                    now + config.scan.round_deadline_seconds,
                    row["next_scan_at"] or now,
                    now,
                    interval,
                    next_at,
                    row["version"],
                    account["id"],
                    canonical(pool),
                    "manual" if manual else "scheduled",
                ),
            )
            for part in self._query_workset(config):
                watermark = db.execute(
                    "SELECT watermark FROM hf_watermarks WHERE policy_id=? AND query=?",
                    (policy_id, part["key"]),
                ).fetchone()
                lower = (
                    watermark[0] - config.scan.overlap_seconds
                    if watermark
                    else now - config.scan.initial_lookback_seconds
                )
                db.execute(
                    "INSERT INTO hf_query_progress(run_id,query,lower_at,upper_at,expression,product_ids,source_type,visibility_scope,semantic_version,read_budget) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        part["key"],
                        lower,
                        now,
                        part["expression"],
                        canonical(part["products"]),
                        part["source_type"],
                        part["visibility"],
                        part["version"],
                        part["budget"],
                    ),
                )
            cursor = (config.account_ids.index(account["id"]) + 1) % len(config.account_ids)
            db.execute(
                "UPDATE hf_policies SET active_run_id=?,pending_trigger=0,pending_manual=0,next_scan_at=?,rotation_cursor=?,last_search_account_id=?,last_search_identity=?,account_pool_version=?,deferred_until=0,deferred_reason=NULL WHERE id=?",
                (
                    run_id,
                    next_at,
                    cursor,
                    account["id"],
                    account_identity(account),
                    digest(pool),
                    policy_id,
                ),
            )
            db.execute(
                "UPDATE hf_manual_triggers SET run_id=? WHERE policy_id=? AND run_id IS NULL",
                (run_id, policy_id),
            )
            self.queue.put(
                db, "readonly_scan" if config.run_mode == "discovery_only" else "scan", run_id
            )
            Store.audit(
                db,
                "interaction.scan_started",
                {
                    "run_id": run_id,
                    "trigger_type": "manual" if manual else "scheduled",
                    "sampled_interval_seconds": interval,
                    "search_account_id": account["id"],
                    "next_scan_at": next_at,
                },
                now=now,
            )
        return {"run_id": run_id, "coalesced": False}

    def reschedule(self, policy_id, expected_version, *, actor="operator"):
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM hf_policies WHERE id=?", (policy_id,)).fetchone()
            if not row or row["version"] != expected_version:
                raise Conflict("配置已改变，请刷新")
            config = InteractionConfig.model_validate_json(row["config"])
            interval = (
                self.randint(config.scan.min_interval_seconds, config.scan.max_interval_seconds)
                if config.scan.schedule_mode == "random_interval"
                else config.scan.interval_seconds
            )
            old, new = row["next_scan_at"], self.clock() + interval
            db.execute("UPDATE hf_policies SET next_scan_at=? WHERE id=?", (new, policy_id))
            Store.audit(
                db,
                "interaction.schedule_changed",
                {
                    "policy_id": policy_id,
                    "old_next_scan_at": old,
                    "new_next_scan_at": new,
                    "sampled_interval_seconds": interval,
                    "config_version": row["version"],
                    "actor": actor,
                },
                now=self.clock(),
            )
        return self.status(policy_id)

    def _scan_joint(self, job, run, config, account):
        """Fair per-page consumption. Only local errors are isolated; shared gates stop reads."""
        query_rules = {part["key"]: part for part in self._query_workset(config)}
        while self.clock() < run["deadline_at"]:
            progress = self.store.rows(
                "SELECT * FROM hf_query_progress WHERE run_id=? AND complete=0 AND error IS NULL ORDER BY rowid",
                (run["id"],),
            )
            if not progress:
                break
            made_progress, retry_at = False, None
            for part in progress:
                if config.run_mode == "discovery_only" and config.policy_key.endswith("_observer"):
                    stage = part["query"].split("-", 1)[0]
                    rank = {"p0": 0, "p1": 1, "p2": 2}.get(stage, 1)
                    prior = [f"p{i}-%" for i in range(rank)]
                    if prior:
                        clauses = " OR ".join("query LIKE ?" for _ in prior)
                        waiting = self.store.one(
                            "SELECT 1 FROM hf_query_progress WHERE run_id=? AND complete=0 "
                            "AND error IS NULL AND (" + clauses + ") LIMIT 1",
                            (run["id"], *prior),
                        )
                        if waiting:
                            break
                        clauses = " OR ".join("partition_key LIKE ?" for _ in prior)
                        found = self.store.one(
                            "SELECT 1 FROM hf_discoveries WHERE run_id=? AND ("
                            + clauses
                            + ") LIMIT 1",
                            (run["id"], *prior),
                        )
                        if found:
                            with self.store.connect(write=True) as db:
                                self.queue.guard(db, job)
                                db.execute(
                                    "UPDATE hf_query_progress SET error='deferred_lower_priority' "
                                    "WHERE run_id=? AND complete=0 AND error IS NULL "
                                    "AND (query LIKE 'p1-%' OR query LIKE 'p2-%')"
                                    if rank == 1
                                    else "UPDATE hf_query_progress SET error='deferred_lower_priority' "
                                    "WHERE run_id=? AND complete=0 AND error IS NULL "
                                    "AND query LIKE 'p2-%'",
                                    (run["id"],),
                                )
                            break
                if part["retry_at"] > self.clock():
                    retry_at = min(retry_at or part["retry_at"], part["retry_at"])
                    continue
                try:
                    current_account = self.engine.account(account["id"])
                    current_config = InteractionConfig.model_validate(
                        self.policy(run["policy_id"])["config"]
                    )
                    if (
                        not current_config.enabled
                        or account["id"] not in current_config.account_ids
                        or current_account["disabled"]
                        or current_account["status"] != "healthy"
                        or current_config.search_accounts.account_pool_id
                        and not self.store.one(
                            "SELECT 1 FROM pool_members WHERE pool_id=? AND account_id=?",
                            (current_config.search_accounts.account_pool_id, account["id"]),
                        )
                    ):
                        raise Conflict("search_account_removed_or_unavailable")
                    if part["source_type"] == "private" or part["visibility_scope"] != "public":
                        raise Conflict("coverage_incomplete:private_source_unavailable")
                    if part["pages"] >= min(part["read_budget"], config.scan.max_pages_per_query):
                        raise Conflict("coverage_incomplete:page_or_group_budget")
                    if self.clock() >= run["deadline_at"]:
                        break
                    with self.store.connect(write=True) as db:
                        self.queue.guard(db, job)
                        cooldown = db.execute(
                            "SELECT max(until_at) FROM hf_read_cooldowns WHERE scope_id IN (?,?)",
                            (config.platform, config.platform + ":search"),
                        ).fetchone()[0]
                        if (
                            cooldown
                            and cooldown > self.clock()
                            or current_account["cooldown_until"] > self.clock()
                        ):
                            return max(cooldown or 0, current_account["cooldown_until"])
                        usage = self._read_budget(db, config)
                    started = time.monotonic()
                    result = self.io.discover(
                        account,
                        part["expression"],
                        part["lower_at"],
                        part["upper_at"],
                        part["cursor"],
                        config,
                    )
                    if (
                        result.get("rate_limited")
                        or result.get("retry_after_seconds")
                        or result.get("rate_limit_remaining") == 0
                    ):
                        until = max(
                            self.clock() + max(1, result.get("retry_after_seconds", 0)),
                            result.get("rate_limit_reset_at", 0),
                        )
                        if until <= self.clock() + 1:
                            until = self.clock() + 900
                        with self.store.connect(write=True) as db:
                            self.queue.guard(db, job)
                            # Conservative platform scope when provider cannot prove a narrower scope.
                            db.execute(
                                "INSERT INTO hf_read_cooldowns VALUES(?,?,'rate_limited') ON CONFLICT(scope_id) DO UPDATE SET until_at=MAX(until_at,excluded.until_at)",
                                (config.platform, until),
                            )
                        if result.get("rate_limited"):
                            return until
                    incomplete = result.get("coverage_complete") is not True
                    if incomplete and config.run_mode != "discovery_only":
                        raise Conflict("coverage_incomplete:adapter_coverage_unknown")
                    cursor = result.get("next_cursor")
                    if cursor and cursor == part["cursor"]:
                        raise Conflict("coverage_incomplete:cursor_not_advancing")
                    with self.store.connect(write=True) as db:
                        self.queue.guard(db, job)
                        fresh = db.execute(
                            "SELECT state FROM hf_scan_runs WHERE id=?", (run["id"],)
                        ).fetchone()
                        if fresh[0] != "collecting":
                            return
                        for source in result.get("candidates", []):
                            if self._source_matches_query_group(source, query_rules[part["query"]]):
                                self._ingest(db, run, source, config, account, partition=part)
                        db.execute(
                            "UPDATE hf_usage SET actual_cost=?,duration_ms=? WHERE id=?",
                            (
                                result.get("actual_cost"),
                                int((time.monotonic() - started) * 1000),
                                usage,
                            ),
                        )
                        if incomplete:
                            db.execute(
                                "UPDATE hf_query_progress SET pages=pages+1,error=?,retry_at=0 WHERE run_id=? AND query=?",
                                (
                                    result.get("reason") or "browser_window_not_exhaustive",
                                    run["id"],
                                    part["query"],
                                ),
                            )
                        else:
                            db.execute(
                                "UPDATE hf_query_progress SET cursor=?,pages=pages+1,complete=?,retry_at=0 WHERE run_id=? AND query=?",
                                (cursor, not cursor, run["id"], part["query"]),
                            )
                        if not incomplete and not cursor:
                            db.execute(
                                "INSERT INTO hf_watermarks VALUES(?,?,?) ON CONFLICT(policy_id,query) DO UPDATE SET watermark=MAX(watermark,excluded.watermark)",
                                (run["policy_id"], part["query"], part["upper_at"]),
                            )
                    made_progress = True
                except Exception as exc:
                    from .interaction_queue import LostLease

                    if isinstance(exc, LostLease):
                        raise
                    reason = (
                        str(exc)
                        if isinstance(exc, Conflict) and len(str(exc)) < 120
                        else type(exc).__name__
                    )
                    if reason == "read_budget_exhausted":
                        with self.store.connect(write=True) as db:
                            self.queue.guard(db, job)
                            db.execute(
                                "UPDATE hf_query_progress SET error=? WHERE run_id=? AND complete=0",
                                (reason, run["id"]),
                            )
                        break
                    # Transient faults retry the same partition/account/window with bounded backoff.
                    transient = not isinstance(exc, Conflict) or getattr(exc, "safe_code", "") in {
                        "XAPITransportOutcomeUnknown",
                        "XAPIOutcomeUnknown",
                    }
                    delay = min(60, 5 * 2 ** min(part["failures"], 4))
                    again = (
                        transient
                        and part["failures"] < 2
                        and self.clock() + delay < run["deadline_at"]
                    )
                    with self.store.connect(write=True) as db:
                        self.queue.guard(db, job)
                        db.execute(
                            "UPDATE hf_query_progress SET error=?,failures=failures+1,retry_at=? WHERE run_id=? AND query=?",
                            (
                                None if again else reason,
                                self.clock() + delay if again else 0,
                                run["id"],
                                part["query"],
                            ),
                        )
                    if again:
                        retry_at = min(retry_at or self.clock() + delay, self.clock() + delay)
            if not made_progress:
                if retry_at:
                    return retry_at
                break
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            incomplete = db.execute(
                "SELECT count(*) FROM hf_query_progress WHERE run_id=? AND complete=0", (run["id"],)
            ).fetchone()[0]
            db.execute(
                "UPDATE hf_scan_runs SET state='classifying',coverage_incomplete=?,collection_finished_at=? WHERE id=? AND state='collecting'",
                (bool(incomplete), self.clock(), run["id"]),
            )
            self._release_scan(db, run)

    @staticmethod
    def _source_matches_query_group(source, rule):
        text = str(
            source.get("query_visible_text")
            if rule["source_type"] == "public_mentions"
            else source.get("text", "")
        ).casefold()
        return (
            not rule["required_any_phrases"]
            or any(p.casefold() in text for p in rule["required_any_phrases"])
        ) and (
            not rule["required_intent_phrases"]
            or any(p.casefold() in text for p in rule["required_intent_phrases"])
        )
