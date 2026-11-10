"""Local-only official Temporal SDK worker; never invokes a platform adapter."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from .models import epoch
from .temporal_task_workflow import PromotionTask


class Activities:
    def __init__(self, http):
        self.http = http

    async def request(self, method, path, value=None, *, timeout=3):
        response = await self.http.request(method, path, json=value, timeout=timeout)
        if response.status_code in {400, 401, 403, 404, 409, 410, 422}:
            raise ApplicationError(
                "Authenticated task endpoint rejected",
                type="task_endpoint_rejected",
                non_retryable=True,
            )
        response.raise_for_status()
        return response.json()

    @activity.defn(name="promotion_task_admit")
    async def admit(self, value: dict) -> dict:
        return await self.request(
            "POST",
            "/api/temporal/tasks/" + quote(value["task_id"], safe="") + "/admit",
            {k: value[k] for k in ("manifest_hash", "workflow_id")},
        )

    @activity.defn(name="promotion_task_prepare")
    async def prepare(self, value: dict) -> dict:
        return await self.request(
            "POST",
            "/api/temporal/tasks/" + quote(value["task_id"], safe="") + "/prepare",
            {k: value[k] for k in ("manifest_hash", "workflow_id")},
            timeout=10,
        )

    @activity.defn(name="promotion_task_status")
    async def status(self, value: dict) -> dict:
        current = await self.request(
            "GET", "/api/temporal/tasks/" + quote(value["task_id"], safe="")
        )
        if (
            not isinstance(current, dict)
            or current.get("task_id") != value["task_id"]
            or not isinstance(current.get("items"), list)
            or not current.get("state")
        ):
            raise ApplicationError(
                "Original task response invalid", type="task_response_invalid", non_retryable=True
            )
        return current


def validate_config(value):
    for key, prefix in (("temporal_address", "//"), ("promotion_url", "")):
        endpoint = urlsplit(prefix + value[key])
        if (
            endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not endpoint.port
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
        ):
            raise ValueError("Explicit local-only service origins required")
    if value.get("enabled") is not True:
        raise ValueError("Explicit Temporal admission enablement required")


async def run(config):
    validate_config(config)
    temporal = await Client.connect(config["temporal_address"], namespace=config["namespace"])
    async with httpx.AsyncClient(
        base_url=config["promotion_url"],
        timeout=3,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + os.environ[config["promotion_token_env"]]},
    ) as http:
        acts = Activities(http)
        async with Worker(
            temporal,
            task_queue=config["task_queue"],
            workflows=[PromotionTask],
            activities=[acts.admit, acts.status, acts.prepare],
        ):
            while True:
                try:
                    response = await http.get("/api/temporal/tasks")
                    response.raise_for_status()
                    for row in response.json()["data"]:
                        value = {k: row[k] for k in ("task_id", "manifest_hash", "workflow_id")}
                        value["deadline"] = epoch(json.loads(row["policy"])["ends_at"])
                        try:
                            await temporal.start_workflow(
                                PromotionTask.run,
                                value,
                                id=row["workflow_id"],
                                task_queue=config["task_queue"],
                                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                            )
                        except WorkflowAlreadyStartedError:
                            pass
                except httpx.HTTPError:
                    pass  # Only registration discovery waits; existing workflows remain durable.
                await asyncio.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    asyncio.run(run(json.loads(Path(args.config).read_text(encoding="utf-8-sig"))))


if __name__ == "__main__":
    main()
