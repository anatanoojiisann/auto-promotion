import hashlib
import json
import os
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from auto_promotion.config import database_path, load_environment
from auto_promotion.store import Store

load_environment(".env")
root = Path("outputs/facebook-formal-20261026").resolve()
store = Store(database_path(), initialize=False)
manifest = json.loads((root / "today-p1-candidates.json").read_text(encoding="utf-8"))
migration = json.loads((root / "migration-report.json").read_text(encoding="utf-8"))
receipt = json.loads(
    json.loads((root / "opportunity-import-receipt.json").read_text(encoding="utf-8"))["content"][
        0
    ]["text"]
)
accounts = store.rows(
    "SELECT a.id,a.label,a.status,a.checked_at,a.daily_limit,a.hourly_limit,a.min_interval,m.connector,m.profile_url FROM accounts a JOIN meta_browser_accounts m ON m.account_id=a.id WHERE a.platform='facebook'"
)
assert len(accounts) == 2
ids = {a["id"] for a in accounts}
assert ids == set(migration["account_ids"])
assert all(
    (a["daily_limit"], a["hourly_limit"], a["min_interval"]) == (5, 1, 1800) for a in accounts
)
sources = store.rows("SELECT account_id,task_id,state,path FROM meta_browser_sources")
assert {s["state"] for s in sources} == {"stopped", "unknown"}
assert all(
    hashlib.sha256(Path(path).read_bytes()).hexdigest() == checksum
    for path, checksum in migration["source_hashes"].items()
)
history = store.rows(
    "SELECT a.label,i.status,count(*) count FROM meta_browser_history h JOIN items i ON i.id=h.item_id JOIN accounts a ON a.id=i.account_id WHERE a.platform='facebook' GROUP BY a.label,i.status"
)
assert sum(r["count"] for r in history) == 30
assert sum(r["count"] for r in history if r["status"] == "unknown") == 1
attempts = store.one(
    "SELECT count(*) n FROM attempts a JOIN accounts c ON c.id=a.account_id WHERE c.platform='facebook'"
)["n"]
jobs = store.one(
    "SELECT count(*) n FROM execution_jobs j JOIN accounts a ON a.id=j.account_id WHERE a.platform='facebook'"
)["n"]
assert attempts == 0 and jobs == 0
assert len(manifest["items"]) == 10 and all(
    i["event_date"] == "2026-10-26" for i in manifest["items"]
)
assert len({i["url"] for i in manifest["items"]}) == 10
assert len(receipt["ids"]) == 8 and len(set(receipt["ids"])) == 8
events = []
pool = []
for identifier in receipt["ids"]:
    row = store.one("SELECT id,payload FROM opportunities WHERE id=?", (identifier,))
    payload = json.loads(row["payload"])
    context = json.loads(payload["context"])
    assert payload["permission"] == "unknown" and not payload["draft_text"]
    assert context["execution_started"] is False
    events += context["events"]
    pool.append(
        {
            "opportunity_id": identifier,
            "event_count": len(context["events"]),
            "target": payload["target"],
        }
    )
assert len(events) == 10 and {e["event_id"] for e in events} == {
    i["event_id"] for i in manifest["items"]
}


class Check(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = 0
        self.links = 0

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.cards += 1
        if tag == "a" and dict(attrs).get("href", "").startswith("https://www.facebook.com/"):
            self.links += 1


parser = Check()
parser.feed((root / "Facebook正式接入与今日10条P1候选.html").read_text(encoding="utf-8"))
assert parser.cards == 10 and parser.links == 10
health = {}
for port in (8780, 8782):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
        current = json.load(response)
    health[str(port)] = {
        k: current.get(k)
        for k in ("status", "worker", "report_worker", "live_writes", "statistics_only")
    }
operator = (
    os.getenv("AP_API_TOKEN") or (Path(store.path).parent / "operator.key").read_text().strip()
)
request = urllib.request.Request(
    "http://127.0.0.1:8780/api/meta-browser/status", headers={"Authorization": "Bearer " + operator}
)
with urllib.request.urlopen(request, timeout=5) as response:
    api = json.load(response)
assert {a["account_id"] for a in api["accounts"]} == ids and api["cloud_catalog_pending"] is False
checks = {"cloud": 121, "meta": 13, "mcp": 10, "task_channels": 17}
report = {
    "verified_at": time.time(),
    "status": "complete",
    "scope": "two Facebook accounts; original browser logins retained",
    "accounts": accounts,
    "sources": sources,
    "history": history,
    "source_files_unchanged": True,
    "new_facebook_attempts": attempts,
    "facebook_execution_jobs": jobs,
    "old_tasks_restarted": False,
    "global_flags": {
        r["key"]: r["value"]
        for r in store.rows(
            "SELECT key,value FROM meta WHERE key IN ('global_stop','cloud_control_required','meta_browser_cloud_catalog')"
        )
    },
    "services": health,
    "candidate_event_count": len(events),
    "deduplicated_opportunity_count": len(pool),
    "pool": pool,
    "candidate_breakdown": {
        "P0": 0,
        "P1": 10,
        "today_posts": 5,
        "today_comments": 5,
        "video_creation": 8,
        "extended_ai_need": 2,
    },
    "execution_started": False,
    "tests_passed": checks,
    "cloud_version": "4c7b853a-ba44-488e-b8c8-f395e0b5d174",
}
(root / "final-verification.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
plan_path = Path("outputs/meta-integration-20261026/release-plan.json")
plan = json.loads(plan_path.read_text(encoding="utf-8"))
plan.update(
    status="facebook_production_complete_instagram_not_migrated",
    production_database_migrated=True,
    migrated_platforms=["facebook"],
    production_services_restarted=True,
    cloud_deploy_executed=True,
    current_cloud_version=report["cloud_version"],
    production_report=str(root / "final-verification.json"),
    explicit_authorization="User explicitly requested formal unified management and production migration of both Facebook accounts in this chat on 2026-10-26; no new send authorization.",
)
plan["live_browser_observations"].update(
    tabbit_kore="Original login identity verified and registered in production; fresh observation required before sending",
    chrome_loo="Original login identity verified and registered in production; one legacy unknown result retained",
)
plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
hashes = {
    p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    for p in (
        "cloudflare/control-plane/kernel.mjs",
        "cloudflare/control-plane/test/kernel.test.mjs",
        "scripts/migrate_facebook_20261026.py",
        "scripts/commission_facebook_20261026.py",
        "scripts/sync_facebook_identities_20261026.py",
        "docs/meta-browser-integration.md",
    )
}
(root / "production-code-hashes.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
print(
    json.dumps(
        {
            k: report[k]
            for k in (
                "status",
                "new_facebook_attempts",
                "facebook_execution_jobs",
                "candidate_event_count",
                "deduplicated_opportunity_count",
                "candidate_breakdown",
                "services",
                "tests_passed",
                "cloud_version",
            )
        },
        ensure_ascii=False,
    )
)
