"""Explicit local development composition with fresh databases and no real senders."""

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

import uvicorn
from phone_fleet.api import create_app as fleet_app
from phone_fleet.auth import DevelopmentAuth, Principal
from phone_fleet.protocol import VERSION
from phone_fleet.service import Fleet
from phone_fleet.storage import Store as FleetStore

from .adapters import AdapterRegistry
from .api import create_app


def initialize(root, ap_port, fleet_port):
    root = root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = root / "lab.private.json"
    if secret.exists():
        config = json.loads(secret.read_text(encoding="utf-8"))
        if (
            config.get("schema") != "phone-lab/1"
            or config.get("ports") != [ap_port, fleet_port]
            or config.get("protocol") != VERSION
        ):
            raise ValueError("PhoneLabConfigurationMismatch")
    else:
        if any(root.iterdir()):
            raise ValueError("PhoneLabRequiresEmptyDirectory")
        config = {
            "schema": "phone-lab/1",
            "protocol": VERSION,
            "ports": [ap_port, fleet_port],
            "operator_token": secrets.token_urlsafe(32),
            "admin_token": secrets.token_urlsafe(32),
            "business_token": secrets.token_urlsafe(32),
        }
        fd = os.open(secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream)
    phone = root / "phone.private.json"
    value = {
        "mode": "development",
        "enabled": True,
        "fleet_url": f"http://127.0.0.1:{fleet_port}",
        "service_token": config["business_token"],
        "authorized_task_ids": [],
        "channel": "local",
        "bindings": {},
    }
    if not phone.exists():
        fd = os.open(phone, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
    elif json.loads(phone.read_text(encoding="utf-8")) != value:
        raise ValueError("PhoneLabBindingConfigurationChanged")
    return config


def apps(root, ap_port, fleet_port):
    config = initialize(root, ap_port, fleet_port)
    store = FleetStore("sqlite:///" + (root / "fleet.db").resolve().as_posix(), development=True)
    store.migrate()
    auth = DevelopmentAuth(
        {
            config["admin_token"]: Principal("phone-lab", "operator", "admin"),
            config["business_token"]: Principal("phone-lab", "promotion", "business"),
        },
        development=True,
    )
    fleet = fleet_app(Fleet(store, development=True, internal_enabled=True), auth)
    ap = create_app(
        db_path=root / "ap.db",
        registry=AdapterRegistry(root / "workspace"),
        token=config["operator_token"],
        worker=False,
        report_worker=False,
        pooled_worker=False,
        phone_rounds_enabled=True,
        disable_live_writes=True,
        phone_fleet_config=str(root / "phone.private.json"),
    )
    if ap.state.engine.live_enabled:
        raise RuntimeError("PhoneLabLiveWritesForbidden")
    return ap, fleet


async def serve(root, ap_port, fleet_port):
    ap, fleet = apps(root, ap_port, fleet_port)
    servers = [
        uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
        )
        for app, port in ((ap, ap_port), (fleet, fleet_port))
    ]
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        for server in servers:
            server.should_exit = True


def main():
    parser = argparse.ArgumentParser(
        description="Start isolated local services, with real sends disabled and no device fixture"
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--ap-port", type=int, default=18452)
    parser.add_argument("--fleet-port", type=int, default=18450)
    args = parser.parse_args()
    if args.ap_port == args.fleet_port or not all(
        1024 <= p <= 65535 for p in (args.ap_port, args.fleet_port)
    ):
        parser.error("Two distinct non-privileged ports required")
    asyncio.run(serve(args.directory.resolve(), args.ap_port, args.fleet_port))


if __name__ == "__main__":
    main()
