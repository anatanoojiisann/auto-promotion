import hashlib
import json
import time
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.cloud_control import CloudControl, export_snapshot
from auto_promotion.config import database_path, load_environment, workspace_path
from auto_promotion.core import Engine
from auto_promotion.execution_channels import binding
from auto_promotion.models import canonical
from auto_promotion.store import Store

load_environment(".env")
load_environment("var/cloud-node.env")
if Path("outputs/facebook-formal-20261026/identity-sync-report.json").exists():
    raise SystemExit("IdentitySyncAlreadyRecorded: inspect the saved outcome")
engine = Engine(Store(database_path()), AdapterRegistry(workspace_path()), live_enabled=True)
cloud = CloudControl.configured()
before = cloud.request("/v1/status", operator=True, method="GET")
snapshot = export_snapshot(engine)
accounts = [a for a in snapshot["accounts"] if a["platform"] == "facebook" and a["mode"] == "live"]
assert len(accounts) == 2 and all(len(a["identity"].split(":", 1)[1]) == 64 for a in accounts)
with engine.store.connect(write=True) as db:
    previous = db.execute("SELECT value FROM meta WHERE key='cloud_snapshot_revision'").fetchone()
    revision = int(previous[0]) + 1 if previous else 1
    db.execute(
        "INSERT INTO meta VALUES('cloud_snapshot_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(revision),),
    )
payload = {
    "local_store_id": binding(engine),
    "schema": 1,
    "revision": revision,
    "accounts": accounts,
    "tasks": [],
    "jobs": [],
    "quota_events": [],
}
result = cloud.request("/v1/sync", payload, operator=True)
assert not result.get("stale")
destination = hashlib.sha256(cloud.url.encode()).hexdigest()
version = "4c7b853a-ba44-488e-b8c8-f395e0b5d174"
with engine.store.connect(write=True) as db:
    db.execute("UPDATE meta SET value=? WHERE key='meta_browser_cloud_catalog'", (version,))
    for account in accounts:
        db.execute(
            "INSERT INTO cloud_sync_records VALUES(?,?,?,?,?) ON CONFLICT(destination,section,record_id) DO UPDATE SET digest=excluded.digest,revision=excluded.revision WHERE excluded.revision>=cloud_sync_records.revision",
            (
                destination,
                "accounts",
                account["id"],
                hashlib.sha256(canonical(account).encode()).hexdigest(),
                revision,
            ),
        )
after = cloud.request("/v1/status", operator=True, method="GET")
assert after["settings"] == before["settings"]
report = {
    "at": time.time(),
    "version": version,
    "result": result,
    "identities": [{"account_id": a["id"], "identity": a["identity"]} for a in accounts],
    "settings_preserved": True,
    "new_jobs": 0,
    "new_attempts": 0,
}
Path("outputs/facebook-formal-20261026/identity-sync-report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(report, ensure_ascii=False))
