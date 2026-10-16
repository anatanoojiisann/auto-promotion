"""One-time provisioning of a scoped executor; secrets never appear in output."""

import hashlib
import os
import secrets
from pathlib import Path

from .cloud_control import CloudControl, CloudUnavailable, export_snapshot
from .models import canonical


def bootstrap(engine, *, url, node_id, operator_file, account_ids=None, adopt_local=False):
    operator_file = Path(operator_file).resolve()
    cloud = CloudControl(url, operator_token=operator_file.read_text().strip())
    snapshot = export_snapshot(engine)
    selected = set(account_ids or [a["id"] for a in snapshot["accounts"]])
    if not selected or selected - {a["id"] for a in snapshot["accounts"]}:
        raise ValueError("UnknownNodeAccount")
    from .routing import route_for

    pools = {route_for(a).pool for a in engine.accounts() if a["id"] in selected}
    pools.update(j["pool"] for j in snapshot["jobs"] if j["account_id"] in selected)
    folder = Path(engine.store.path).parent
    node_file = folder / "cloud-node.env"
    if node_file.exists():
        # Reuse the token on retries; replacing a token would revoke active receipts.
        from .config import load_environment

        load_environment(node_file)
        if os.getenv("AP_NODE_ID") != node_id or os.getenv("AP_CLOUD_CONTROL_URL") != url.rstrip(
            "/"
        ):
            raise ValueError("ExistingNodeBindingDiffers")
        node_token = os.getenv("AP_CLOUD_NODE_TOKEN")
        if not node_token:
            raise ValueError("ExistingNodeTokenMissing")
    else:
        node_token = secrets.token_urlsafe(48)
    cloud.request(
        "/v1/nodes",
        {
            "id": node_id,
            "token_hash": hashlib.sha256(node_token.encode()).hexdigest(),
            "pools": sorted(pools),
            "account_ids": sorted(selected),
        },
        operator=True,
    )
    cloud.sync(engine, force=True)
    if adopt_local:
        settings = engine.execution_settings()
        cloud.request(
            "/v1/settings",
            {
                "global_stop": engine.store.one("SELECT value FROM meta WHERE key='global_stop'")[
                    "value"
                ]
                != "0",
                "live_enabled": engine.live_enabled,
                "max_workers": settings["max_workers"],
                "global_limit": settings["global_limit"],
                "platform_workers": settings["platform_workers"],
                "platform_limits": settings["platform_limits"],
                "pool_workers": settings.get("pool_workers", {}),
            },
            operator=True,
            method="PUT",
        )
    values = {
        "AP_EXECUTION_MODE": "pooled",
        "AP_CLOUD_CONTROL_URL": url.rstrip("/"),
        "AP_NODE_ID": node_id,
        "AP_CLOUD_NODE_TOKEN": node_token,
        "AP_CLOUD_OPERATOR_TOKEN_FILE": operator_file.as_posix(),
        "AP_EXECUTOR_POOLS": ",".join(sorted(pools)),
    }
    import tempfile

    fd, temporary = tempfile.mkstemp(prefix="cloud-node-", suffix=".env", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            for key, value in values.items():
                target.write(key + "=" + canonical(value) + "\n")
        os.replace(temporary, node_file)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        "provisioned": True,
        "node_id": node_id,
        "pools": sorted(pools),
        "account_count": len(selected),
        "configuration_file": str(node_file.resolve()),
        "local_policy_adopted": adopt_local,
        "requires_service_restart": True,
    }


def configured():
    cloud = CloudControl.configured()
    if not cloud:
        raise CloudUnavailable("CloudControlNotConfigured")
    return cloud
