"""Add Facebook to the existing node without replacing its scopes or settings."""

import hashlib
import json
import os
import re
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
if Path("outputs/facebook-formal-20261026/commission-report.json").exists():
    raise SystemExit("CommissionAlreadyRecorded: inspect report; do not repeat provisioning")
cloud = CloudControl.configured()
status = cloud.request("/v1/status", operator=True, method="GET")
node = next(n for n in status["nodes"] if n["id"] == cloud.node_id)
assert node["enabled"] == 1
before_settings = status["settings"]
before_accounts = set(json.loads(node["accounts"]))
before_pools = set(json.loads(node["pools"]))
engine = Engine(
    Store(database_path()),
    AdapterRegistry(workspace_path()),
    live_enabled=os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1",
)
facebook = engine.store.rows(
    "SELECT a.id,a.label FROM accounts a JOIN meta_browser_accounts m ON m.account_id=a.id WHERE a.platform='facebook'"
)
assert len(facebook) == 2
selected = {a["id"] for a in facebook}
new_accounts = before_accounts | selected
new_pools = before_pools | {"facebook.desktop"}
enrollment = cloud.request(
    "/v1/nodes",
    {
        "id": cloud.node_id,
        "token_hash": hashlib.sha256(cloud.node_token.encode()).hexdigest(),
        "pools": sorted(new_pools),
        "account_ids": sorted(new_accounts),
    },
    operator=True,
)
version = "41b09933-cb6b-4696-8ab7-7633345154dc"
with engine.store.connect(write=True) as db:
    db.execute(
        "INSERT INTO meta VALUES('meta_browser_cloud_catalog',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (version,),
    )
    Store.audit(
        db,
        "meta_browser.formal_release",
        {
            "scope": sorted(selected),
            "cloud_version": version,
            "source": "Explicit human request in this chat to formally migrate the two Facebook accounts; no send authorization",
        },
    )
snapshot = export_snapshot(engine)
scoped = {
    "schema": snapshot["schema"],
    "accounts": [a for a in snapshot["accounts"] if a["id"] in selected],
    "tasks": [],
    "jobs": [],
    "quota_events": [],
}
assert len(scoped["accounts"]) == 2
with engine.store.connect(write=True) as db:
    previous = db.execute("SELECT value FROM meta WHERE key='cloud_snapshot_revision'").fetchone()
    revision = int(previous[0]) + 1 if previous else 1
    db.execute(
        "INSERT INTO meta VALUES('cloud_snapshot_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(revision),),
    )
scoped.update(local_store_id=binding(engine), revision=revision)
sync = cloud.request("/v1/sync", scoped, operator=True)
assert not sync.get("stale")
destination = hashlib.sha256(cloud.url.encode()).hexdigest()
with engine.store.connect(write=True) as db:
    for account in scoped["accounts"]:
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
envfile = Path(".env")
envtext = envfile.read_text(encoding="utf-8-sig")
owner_before = next(
    (
        line.partition("=")[2]
        for line in envtext.splitlines()
        if line.startswith("AP_FACEBOOK_OWNER=")
    ),
    None,
)
owner_line = "AP_FACEBOOK_OWNER=auto-promotion"
updated = (
    re.sub(r"(?m)^AP_FACEBOOK_OWNER=.*$", owner_line, envtext)
    if owner_before is not None
    else envtext.rstrip("\r\n") + "\n" + owner_line + "\n"
)
if updated != envtext:
    envfile.write_text(updated, encoding="utf-8")
nodefile = Path("var/cloud-node.env")
node_text = nodefile.read_text(encoding="utf-8-sig")
pool_line = "AP_EXECUTOR_POOLS=" + json.dumps(",".join(sorted(new_pools)))
node_updated = re.sub(r"(?m)^AP_EXECUTOR_POOLS=.*$", lambda _: pool_line, node_text)
assert node_updated != node_text or "facebook.desktop" in node_text
if node_updated != node_text:
    nodefile.write_text(node_updated, encoding="utf-8")
after = cloud.request("/v1/status", operator=True, method="GET")
assert after["settings"] == before_settings, "Cloud settings unexpectedly changed"
current = next(n for n in after["nodes"] if n["id"] == cloud.node_id)
assert before_accounts <= set(json.loads(current["accounts"]))
assert before_pools <= set(json.loads(current["pools"]))
result = {
    "at": time.time(),
    "version": version,
    "node_id": cloud.node_id,
    "facebook_accounts": facebook,
    "prior_node_accounts_preserved": True,
    "prior_node_pools_preserved": True,
    "cloud_settings_preserved": True,
    "scoped_account_sync": sync,
    "existing_tasks_uploaded": 0,
    "new_jobs_uploaded": 0,
    "facebook_owner_configured": True,
    "requires_service_refresh": True,
    "dispatcher_remains_paused": True,
    "previous_facebook_owner": owner_before,
}
Path("outputs/facebook-formal-20261026/commission-report.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=False))
