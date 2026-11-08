"""Durable coordination only: external writes stay inside AP's fenced original queue."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError


@workflow.defn
class PromotionTask:
    def __init__(self):
        self.state = {"stage": "queued", "events": []}

    @workflow.query
    def progress(self) -> dict:
        return self.state

    def record(self, stage, **fields):
        if stage != self.state["stage"]:
            self.state["events"].append({"stage": stage, "at": workflow.now().timestamp()})
        self.state.update(stage=stage, **fields)

    async def call(self, name, value):
        remaining = max(0.001, self.deadline - workflow.now().timestamp())
        return await workflow.execute_activity(name, value,
            start_to_close_timeout=timedelta(seconds=min(5, remaining)),
            schedule_to_close_timeout=timedelta(seconds=min(15, remaining)),
            retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1)))

    @workflow.run
    async def run(self, value: dict) -> dict:
        if set(value) != {"task_id", "manifest_hash", "workflow_id", "deadline"}:
            raise ApplicationError("Frozen task correlation required", non_retryable=True)
        if workflow.info().workflow_id != value["workflow_id"]:
            raise ApplicationError("Workflow identity does not match original registration", non_retryable=True)
        self.deadline = value["deadline"]
        self.state.update(task_id=value["task_id"], manifest_hash=value["manifest_hash"],
                          started_at=workflow.now().timestamp())
        admitted = False
        while workflow.now().timestamp() < self.deadline:
            try:
                if not admitted:
                    reply = await self.call("promotion_task_admit", value)
                    if not reply.get("admitted"):
                        self.record("waiting_control", reason=reply.get("waiting_for"), device_held=False)
                        await workflow.sleep(1)
                        continue
                    admitted = True
                current = await self.call("promotion_task_status", value)
            except ActivityError as error:
                if isinstance(error.cause, ApplicationError) and error.cause.non_retryable:
                    self.record("blocked", reason=error.cause.type, device_held=False)
                    return self.state
                self.record("waiting_connection", device_held=False)
                await workflow.sleep(2)
                continue
            if current.get("manifest_hash") != value["manifest_hash"]:
                self.record("unknown", reason="immutable_manifest_changed")
                return self.state
            self.state["items"] = current["items"]
            rows = current["items"]
            if rows and all(i["status"] == "confirmed" and i.get("external_reference")
                            and i.get("verified") is True and i.get("native_id") for i in rows):
                self.record("confirmed", device_held=False)
                self.state["elapsed_seconds"] = workflow.now().timestamp() - self.state["started_at"]
                return self.state
            terminal = {"failed_known", "cancelled", "missed"}
            if rows and all(i["status"] in terminal or (
                    i["status"] == "confirmed" and i.get("external_reference")
                    and i.get("verified") is True and i.get("native_id")) for i in rows):
                self.record("partial" if any(i["status"] == "confirmed" for i in rows) else "failed_known", device_held=False)
                return self.state
            if current["state"] in {"paused", "cancelled"}:
                self.record(current["state"], reason="business_task_stopped")
                return self.state
            pending = any(i["status"] == "unknown" for i in rows)
            self.record("waiting_receipt" if pending else "executing",
                        device_held=any(i.get("resource_state") in {"fenced", "busy"} for i in rows))
            await workflow.sleep(1)
        self.record("unknown" if any(i.get("submission_started") or i.get("status") == "unknown" for i in self.state.get("items", [])) else "missed",
                    reason="original_deadline_elapsed")
        return self.state
