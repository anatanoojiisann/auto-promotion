"""Advance only the joint observation policy; never invoke the publication stages."""

import argparse
import json
import time
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.config import database_path, load_environment, workspace_path
from auto_promotion.core import Conflict, Engine
from auto_promotion.interaction_models import InteractionConfig
from auto_promotion.interaction_observer import account_review_status, fallback_status
from auto_promotion.interactions import Interactions
from auto_promotion.store import Store


def main():
    load_environment(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(description="Run the joint read-only observer once")
    parser.add_argument("--db", default=str(database_path()))
    parser.add_argument("--policy-id", required=True)
    args = parser.parse_args()
    engine = Engine(
        Store(args.db), AdapterRegistry(workspace_path()), clock=time.time, live_enabled=False
    )
    service = Interactions(engine)
    policy = service.policy(args.policy_id)
    config = InteractionConfig.model_validate(policy["config"])
    if not config.joint or config.run_mode != "discovery_only":
        raise Conflict("observer_policy_is_not_read_only")
    if not config.enabled:
        print(json.dumps({"status": "paused", "policy_id": args.policy_id}))
        return
    # This entry point is deliberately scoped to a single strategy. Do not consume
    # another strategy's stage jobs if a second read-only policy is introduced.
    other = engine.store.one(
        "SELECT 1 FROM execution_jobs j WHERE j.kind IN ('hf_readonly_scan','hf_readonly_classify') "
        "AND j.state IN ('pending','leased') AND NOT EXISTS("
        "SELECT 1 FROM hf_scan_runs r WHERE r.policy_id=? AND "
        "(j.reference_id=r.id OR j.reference_id LIKE r.id||'|%')) LIMIT 1",
        (args.policy_id,),
    )
    if other:
        raise Conflict("another_read_only_policy_has_pending_jobs")
    now = time.time()
    if not policy["active_run_id"] and policy["next_scan_at"] <= now:
        service.trigger(args.policy_id)
    scans = service.run_stage("readonly_scan", max_jobs=1)
    classifications = service.run_stage("readonly_classify", max_jobs=500)
    service.settle_rounds()
    status = service.status(args.policy_id)
    recent = status["runs"][0] if status["runs"] else None
    print(
        json.dumps(
            {
                "policy_id": args.policy_id,
                "mode": status["config"]["run_mode"],
                "scan_jobs": scans["processed"],
                "review_jobs": classifications["processed"],
                "run_id": recent["id"] if recent else None,
                "run_state": recent["state"] if recent else None,
                "observed": status["metrics"]["candidate_count"],
                "new_observed": recent["fetched"] - recent["duplicates"]
                if recent and scans["processed"]
                else 0,
                "coverage_incomplete": bool(recent["coverage_incomplete"]) if recent else None,
                "next_scan_at": status["next_scan_at"],
                "deferred_reason": status["deferred_reason"],
                "publication_attempts": 0,
                "fallback": (
                    fallback_status(engine.store, args.policy_id)
                    if config.policy_key == "aigram_aurax_joint"
                    else None
                ),
                "account_review": (
                    account_review_status(engine.store, args.policy_id)
                    if config.policy_key.endswith("_observer")
                    else None
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
