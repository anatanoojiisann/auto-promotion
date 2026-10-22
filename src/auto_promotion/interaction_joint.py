"""Joint policy routing. All authority comes from database/configuration, never model text."""

import json

from .core import Conflict, NotFound
from .interaction_models import InteractionConfig
from .models import canonical, digest
from .store import decode


def account_identity(account):
    # Live identities must be bound by health verification. Synthetic IDs are isolated.
    return account.get("identity_hash") or (
        account["id"] if account["mode"] == "simulation" else None
    )


class JointInteractions:
    def _resolve_accounts(self, config):
        from .pools import Pools

        if config.joint:
            if config.search_accounts.account_pool_id:
                config.search_accounts.account_ids = Pools(self.engine).get(
                    config.search_accounts.account_pool_id
                )["account_ids"]
            for p in config.products:
                if p.execution_pool_id:
                    p.execution_account_ids = Pools(self.engine).get(p.execution_pool_id)[
                        "account_ids"
                    ]
            config.account_ids = list(config.search_accounts.account_ids)
        elif config.account_pool_id:
            config.account_ids = Pools(self.engine).get(config.account_pool_id)["account_ids"]
        ids = list(
            dict.fromkeys(
                config.account_ids + [a for p in config.products for a in p.execution_account_ids]
            )
        )
        accounts = [self.engine.account(a) for a in ids]
        if (
            any(a["platform"] != config.platform for a in accounts)
            or len({a["mode"] for a in accounts}) > 1
        ):
            raise Conflict("策略必须使用同平台、同模式账号")
        if (
            config.joint
            and config.enabled
            and config.search_accounts.require_different_from_previous
        ):
            identities = {account_identity(self.engine.account(a)) for a in config.account_ids} - {
                None
            }
            if len(identities) < 2:
                raise Conflict("严格轮换需要至少两个已核验的不同平台身份")

    def _register_scope(self, db, config):
        for project in dict.fromkeys([config.project_id, *config.historical_project_ids]):
            existing = db.execute(
                "SELECT scope_id FROM hf_dedupe_projects WHERE project_id=?", (project,)
            ).fetchone()
            previous_scope = existing[0] if existing else project
            if previous_scope != config.scope_id:
                # Mapping may only absorb an independent project; never split a shared scope.
                shared = db.execute(
                    "SELECT count(*) FROM hf_dedupe_projects WHERE scope_id=?", (previous_scope,)
                ).fetchone()[0]
                if shared > 1:
                    raise Conflict("历史项目已属于另一共享去重范围")
                for intent in db.execute(
                    "SELECT * FROM interaction_intents WHERE project_id=?", (previous_scope,)
                ).fetchall():
                    conflict = db.execute(
                        "SELECT 1 FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=?",
                        (
                            config.scope_id,
                            intent["platform"],
                            intent["source_event_id"],
                            intent["action_kind"],
                        ),
                    ).fetchone()
                    if conflict:
                        raise Conflict("共享范围存在重叠发送意图，需先核对未决记录")
                db.execute(
                    "UPDATE interaction_intents SET project_id=? WHERE project_id=?",
                    (config.scope_id, previous_scope),
                )
                db.execute(
                    "UPDATE hf_processing_tasks SET dedupe_scope_id=? WHERE dedupe_scope_id=?",
                    (config.scope_id, previous_scope),
                )
            db.execute(
                "INSERT INTO hf_dedupe_projects VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET scope_id=excluded.scope_id",
                (project, config.scope_id),
            )

    def _search_choice(self, db, row, config, now):
        cooldown = db.execute(
            "SELECT max(until_at) FROM hf_read_cooldowns WHERE scope_id IN (?,?)",
            (config.platform, config.platform + ":search"),
        ).fetchone()[0]
        if cooldown and cooldown > now:
            return None, "shared_read_cooldown", cooldown
        ids = config.account_ids
        if not config.joint:
            return (
                (self.engine.account(ids[0]), None, 0)
                if ids
                else (None, "WAITING_ACCOUNT", now + 60)
            )
        if config.search_accounts.account_pool_id:
            ids = [
                a
                for a in ids
                if db.execute(
                    "SELECT 1 FROM pool_members WHERE pool_id=? AND account_id=?",
                    (config.search_accounts.account_pool_id, a),
                ).fetchone()
            ]
        if not ids:
            return None, "WAITING_ACCOUNT:empty_search_pool", now + 60
        # Stable last identity handles removals, reordered rings and duplicate connections.
        start = (
            (ids.index(row["last_search_account_id"]) + 1) % len(ids)
            if row["last_search_account_id"] in ids
            else row["rotation_cursor"] % len(ids)
        )
        for offset in range(len(ids)):
            account = self.engine.account(ids[(start + offset) % len(ids)])
            identity = account_identity(account)
            if (
                not identity
                or account["disabled"]
                or account["status"] != "healthy"
                or account["cooldown_until"] > now
            ):
                continue
            if (
                config.search_accounts.require_different_from_previous
                and identity == row["last_search_identity"]
            ):
                continue
            return account, None, 0
        return None, "WAITING_ACCOUNT:no_eligible_alternative", now + 60

    def _query_workset(self, config):
        if not config.joint:
            return [
                {
                    "key": q,
                    "expression": q,
                    "products": [],
                    "source_type": "public_need",
                    "visibility": "public",
                    "version": "1",
                    "budget": config.scan.max_pages_per_query,
                }
                for q in dict.fromkeys(config.queries)
            ]
        # Interleave product groups so one product cannot consume the full read budget first.
        work = []
        groups = [
            (p, [q for q in p.query_groups if q.enabled]) for p in config.products if p.enabled
        ]
        for index in range(max((len(g) for _, g in groups), default=0)):
            for product, queries in groups:
                if index >= len(queries):
                    continue
                q = queries[index]
                products = list(dict.fromkeys([product.product_id, *q.product_ids]))
                semantics = [
                    product.product_id,
                    q.id,
                    q.version,
                    q.expression,
                    q.source_type,
                    q.visibility_scope,
                    sorted(products),
                    q.required_any_phrases,
                    q.required_intent_phrases,
                ]
                work.append(
                    {
                        "key": q.id + ":" + digest(semantics)[:24],
                        "expression": q.expression,
                        "products": products,
                        "source_type": q.source_type,
                        "visibility": q.visibility_scope,
                        "version": q.version,
                        "budget": q.max_reads_per_round,
                        "required_any_phrases": q.required_any_phrases,
                        "required_intent_phrases": q.required_intent_phrases,
                    }
                )
        if config.run_mode == "discovery_only":
            # Account observers read the high-priority query groups first.
            # Within a priority, the configured product order is the preference.
            # A human still decides whether a visible post is actually suitable.
            rank = {"p0": 0, "p1": 1, "p2": 2}
            product_rank = {p.product_id: i for i, p in enumerate(config.products)}
            work.sort(
                key=lambda part: (
                    rank.get(part["key"].split("-", 1)[0], 1),
                    product_rank.get(part["products"][0], len(product_rank)),
                )
            )
        return work

    def _conversation_owner(self, source):
        # These fields are extracted by the trusted X adapter from native entities/references.
        identities = set(map(str, source.get("mentioned_user_ids", [])))
        if source.get("in_reply_to_user_id"):
            identities.add(str(source["in_reply_to_user_id"]))
        for account in self.engine.accounts():
            if account["platform"] != "x":
                continue
            native = self.io.native_user_id(account)
            if native and native in identities:
                return account["id"]
        reply_id = source.get("in_reply_to_event_id")
        if reply_id:
            row = self.store.one(
                "SELECT execution_account_id,owner_account_id FROM hf_candidates WHERE external_reference LIKE ? AND state='PUBLISHED'",
                ("%/status/" + str(reply_id),),
            )
            if row:
                return row["execution_account_id"] or row["owner_account_id"]
        return None

    def _joint_quality(self, classification, source, config):
        if classification.priority == "EXCLUDED":
            return None if classification.exclusion_reason else "exclusion_reason_missing"
        if classification.classification_status != "classified" and not (
            classification.priority == "P0"
            and classification.classification_status == "pending_facts"
        ):
            return classification.classification_status
        if (
            classification.context_status != "complete"
            or source.get("context_status") != "complete"
        ):
            return "context_incomplete"
        if classification.priority in {"P2", "P3"}:
            return None
        if not classification.unresolved or not classification.intent_evidence:
            return "unresolved_need_not_proven"
        for evidence in classification.intent_evidence:
            if isinstance(evidence, str):
                return "structured_intent_evidence_required"
            if evidence.source_event_id != source["id"] or evidence.quote not in source.get(
                evidence.field, ""
            ):
                return "intent_evidence_invalid"
        products = {p.product_id: p for p in config.products if p.enabled}
        matches = {}
        for match in classification.product_matches:
            if match.product_id not in products or match.product_id in matches:
                return "product_match_invalid"
            facts = {f.id for f in config.active_facts(self.clock(), match.product_id)}
            if not set(match.verified_fact_ids) <= facts:
                if classification.priority == "P0" and self._conversation_owner(source):
                    continue
                return "product_constraints_or_facts_invalid"
            if match.unsatisfied_constraints:
                continue
            if match.verified_fact_ids:
                matches[match.product_id] = match
        if classification.priority == "P0" and not self._conversation_owner(source):
            # Paying intent is P1 only when the model supplies real matching evidence.
            classification.priority = "P1"
        if classification.priority in {"P0", "P1"}:
            if classification.priority == "P1" and not matches:
                return "facts_unverified"
            if classification.priority == "P1" and not classification.contribution:
                return "contribution_missing"
            if set(classification.matched_product_ids) - products.keys():
                return "product_match_invalid"
            viable = list(matches) or [
                p for p in classification.matched_product_ids if p in products
            ]
            explicit = classification.explicit_product_id
            text = (source["text"] + source.get("context", "")).casefold()
            if explicit not in products or explicit.casefold() not in text:
                literal_products = [p for p in products if p.casefold() in text]
                explicit = literal_products[0] if len(literal_products) == 1 else None
            if classification.priority == "P0" and explicit in products and explicit not in viable:
                viable.append(explicit)
            if not viable:
                return "product_match_missing"
            # A model may suggest an order; only validated matches enter the deterministic tie break.
            ranked = sorted(
                viable,
                key=lambda p: (
                    -len(matches[p].satisfied_constraints) if p in matches else 0,
                    config.product_tie_break_order.index(p)
                    if p in config.product_tie_break_order
                    else len(config.product_tie_break_order),
                    p,
                ),
            )
            # Explicit product must also appear literally in the supplied source/context.
            if explicit in viable and explicit.casefold() in text:
                primary, reason = explicit, "explicit_product_with_verified_match"
            else:
                primary, reason = ranked[0], "constraint_coverage_then_configured_order"
            classification.primary_product_id = primary
            classification.primary_product_reason = reason
            classification.matched_product_ids = viable
            classification.verified_fact_ids = (
                matches[primary].verified_fact_ids if primary in matches else []
            )
            if (
                classification.language not in products[primary].languages
                or classification.language not in config.languages
            ):
                return "language_not_authorized"
        return None

    def _create_processing(self, db, candidate_id, config, classification):
        now = self.clock()
        row = db.execute("SELECT * FROM hf_candidates WHERE id=?", (candidate_id,)).fetchone()
        existing = db.execute(
            "SELECT * FROM hf_processing_tasks WHERE dedupe_scope_id=? AND platform=? AND source_event_id=? AND action_kind='reply'",
            (config.scope_id, config.platform, row["source_event_id"]),
        ).fetchone()
        if existing and existing["state"] in {
            "CLOSED",
            "PUBLISHED",
            "PUBLISHING",
            "VERIFYING",
            "SIMULATED",
        }:
            return existing["id"]
        task_id = (
            existing["id"]
            if existing
            else "HFP-"
            + digest([config.scope_id, config.platform, row["source_event_id"], "reply"])[:24]
        )
        priority = (
            "P0"
            if classification.priority == "P0" or existing and existing["priority"] == "P0"
            else "P1"
        )
        matched = (
            list(
                dict.fromkeys(
                    json.loads(existing["matched_product_ids"]) + classification.matched_product_ids
                )
            )
            if existing
            else classification.matched_product_ids
        )
        owner = self._conversation_owner(json.loads(row["source"])) if priority == "P0" else None
        db.execute(
            "INSERT INTO hf_processing_tasks(id,candidate_id,policy_id,dedupe_scope_id,platform,source_event_id,priority,primary_product_id,matched_product_ids,conversation_owner_account_id,qualified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_scope_id,platform,source_event_id,action_kind) DO UPDATE SET priority=excluded.priority,primary_product_id=excluded.primary_product_id,matched_product_ids=excluded.matched_product_ids,conversation_owner_account_id=COALESCE(excluded.conversation_owner_account_id,conversation_owner_account_id),version=version+1,updated_at=excluded.updated_at",
            (
                task_id,
                candidate_id,
                row["policy_id"],
                config.scope_id,
                config.platform,
                row["source_event_id"],
                priority,
                classification.primary_product_id,
                canonical(matched),
                owner,
                now,
                now,
                now,
            ),
        )
        db.execute(
            "UPDATE hf_candidates SET processing_task_id=?,conversation_owner_account_id=?,matched_product_ids=?,primary_product_id=? WHERE id=?",
            (task_id, owner, canonical(matched), classification.primary_product_id, candidate_id),
        )
        self.queue.put(
            db,
            "assign",
            candidate_id + "|" + str(row["version"]) + "|" + str(row["config_version"]),
        )
        db.execute(
            "UPDATE hf_processing_tasks SET config_version=?,source_hash=?,classification=? WHERE id=?",
            (
                row["config_version"],
                row["source_hash"],
                canonical(classification.model_dump()),
                task_id,
            ),
        )
        return task_id

    def _execution_ids(self, config, product_id):
        if not config.joint:
            return config.account_ids
        product = next(
            (p for p in config.products if p.enabled and p.product_id == product_id), None
        )
        if not product:
            return []
        ids = product.execution_account_ids
        if product.execution_pool_id:
            ids = [
                a
                for a in ids
                if self.store.one(
                    "SELECT 1 FROM pool_members WHERE pool_id=? AND account_id=?",
                    (product.execution_pool_id, a),
                )
            ]
        return ids

    def _assign(self, job):
        candidate_id = job["reference_id"].split("|")[0]
        candidate = self.candidate(candidate_id)
        if (
            candidate["state"] not in {"QUEUED", "GENERATING", "READY"}
            or not candidate["processing_task_id"]
        ):
            return
        config = InteractionConfig.model_validate(self.policy(candidate["policy_id"])["config"])
        if not config.enabled:
            return self.clock() + 60
        ids = self._execution_ids(config, candidate["primary_product_id"])
        owner = candidate["conversation_owner_account_id"]
        preferred = []
        if candidate["priority"] == "P0":
            ids = [owner] if owner in ids else []
        elif config.joint:
            product = next(
                p for p in config.products if p.product_id == candidate["primary_product_id"]
            )
            preferred = product.preferred_execution_account_ids_by_language.get(
                candidate["classification"].get("language", ""), []
            )
            ids = [a for a in preferred if a in ids] + [a for a in ids if a not in preferred]
        from .pools import Pools

        account = None
        for account_id in ids:
            a = self.engine.account(account_id)
            profile = Pools(self.engine).profile(account_id)["payload"]
            if a["disabled"] or a["status"] != "healthy" or a["cooldown_until"] > self.clock():
                continue
            if (
                candidate["classification"].get("language") not in profile["languages"]
                or profile["allowed_targets"]
                and candidate["source"]["url"] not in profile["allowed_targets"]
            ):
                continue
            account = a
            break
        with self.store.connect(write=True) as db:
            self.queue.guard(db, job)
            fresh = db.execute("SELECT * FROM hf_candidates WHERE id=?", (candidate_id,)).fetchone()
            if fresh["version"] != candidate["version"] or fresh["state"] not in {
                "QUEUED",
                "GENERATING",
                "READY",
            }:
                return
            # Never reassign an intent whose external submission has begun.
            if fresh["item_id"]:
                item = db.execute(
                    "SELECT attempt_id FROM items WHERE id=?", (fresh["item_id"],)
                ).fetchone()
                if item and item[0]:
                    return
            processing = db.execute(
                "SELECT * FROM hf_processing_tasks WHERE id=?", (candidate["processing_task_id"],)
            ).fetchone()
            selected = account["id"] if account else None
            status = "ASSIGNED" if selected else "WAITING_ACCOUNT"
            reason = "conversation_owner" if owner else "product_language_persona_target"
            if selected and not owner and config.joint and selected in preferred:
                reason = "language_preference:" + candidate["classification"].get("language", "")
            if not selected:
                reason = (
                    "conversation_owner_unavailable" if owner else "no_eligible_execution_account"
                )
            db.execute(
                "INSERT OR IGNORE INTO hf_assignment_events VALUES(?,?,?,?,?,?,?)",
                (
                    digest([processing["id"], processing["version"], selected, status]),
                    processing["id"],
                    processing["version"],
                    selected,
                    status,
                    reason,
                    self.clock(),
                ),
            )
            db.execute(
                "UPDATE hf_processing_tasks SET execution_account_id=?,assignment_status=?,state=?,block_reason=?,assigned_at=CASE WHEN ? IS NOT NULL THEN COALESCE(assigned_at,?) ELSE assigned_at END,updated_at=? WHERE id=?",
                (
                    selected,
                    status,
                    "TASK_CREATED" if selected else "WAITING_ACCOUNT",
                    None if selected else reason,
                    selected,
                    self.clock(),
                    self.clock(),
                    processing["id"],
                ),
            )
            db.execute(
                "UPDATE hf_candidates SET execution_account_id=?,owner_account_id=?,block_reason=? WHERE id=?",
                (selected, selected, None if selected else reason, candidate_id),
            )
            if selected:
                self.queue.put(
                    db,
                    "generate",
                    candidate_id
                    + "|"
                    + str(candidate["version"])
                    + "|"
                    + str(candidate["config_version"]),
                )
        return None if account else self.clock() + 60

    def processing_tasks(self, policy_id, *, offset=0, limit=100):
        self.policy(policy_id)
        if offset < 0 or not 1 <= limit <= 500:
            raise Conflict("分页范围无效")
        return [
            self.processing_task(r["id"])
            for r in self.store.rows(
                "SELECT DISTINCT t.id,t.created_at FROM hf_processing_tasks t LEFT JOIN hf_discoveries d ON d.candidate_id=t.candidate_id WHERE t.policy_id=? OR d.policy_id=? ORDER BY t.created_at DESC LIMIT ? OFFSET ?",
                (policy_id, policy_id, limit, offset),
            )
        ]

    def processing_task(self, task_id):
        row = self.store.one("SELECT * FROM hf_processing_tasks WHERE id=?", (task_id,))
        if not row:
            raise NotFound("处理任务不存在")
        task = decode(row, "matched_product_ids", "classification")
        candidate = self.candidate(row["candidate_id"])
        task.update(
            {
                "candidate": candidate,
                "assignment_events": self.store.rows(
                    "SELECT * FROM hf_assignment_events WHERE processing_task_id=? ORDER BY at",
                    (task_id,),
                ),
            }
        )
        return task

    def sync_processing(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE hf_processing_tasks SET state=CASE WHEN c.state IN ('PUBLISHING','VERIFYING','PUBLISHED','SIMULATED','CLOSED') THEN c.state WHEN c.block_reason IS NOT NULL AND c.execution_account_id IS NOT NULL THEN CASE WHEN c.block_reason LIKE '%facts%' THEN 'WAITING_FACTS' ELSE 'WAITING_CAPABILITY' END WHEN c.state IN ('GENERATING','READY') THEN c.state ELSE hf_processing_tasks.state END,channel_eligibility=c.channel_eligibility,block_reason=c.block_reason,task_id=c.task_id,item_id=c.item_id,updated_at=c.updated_at FROM hf_candidates c WHERE c.processing_task_id=hf_processing_tasks.id"
            )
