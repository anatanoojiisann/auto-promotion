import { DeviceControl } from "./device-control.mjs";
import { DurableObject } from "cloudflare:workers";
import {
  ControlKernel,
  ControlError,
  cloudFailure,
  sha,
  canonical,
} from "./kernel.mjs";
import { scheduleMaintenance } from "./maintenance.mjs";
import { standbyCheck } from "./standby.mjs";
import { issuePermit, offlineCheck, controlProof } from "./offline-permit.mjs";
import { WorkflowQueue } from "./workflow-queue.mjs";
import { PublicResearch } from "./public-research.mjs";
import {
  telegramExecutorBridge,
  executorRequestId,
} from "./telegram-executor-bridge.mjs";
import { TelegramWorkflowReader } from "./telegram-workflow-reader.mjs";
import { TelegramPreparedReplies } from "./telegram-prepared-replies.mjs";
import { TelegramTaskBuilder } from "./telegram-task-builder.mjs";
import {
  TelegramNativeGate,
  verifyNativeGate,
} from "./telegram-native-gate.mjs";

export class WorkspaceCoordinator extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.env = env;
    this.checkAlarmOnActivation = true;
  }
  get kernel() {
    return (this._kernel ||= new ControlKernel(this.ctx.storage, {
      legacyLocalUntil: Number(this.env.LOCAL_CHANNEL_UNTIL || 0),
    }));
  }
  get workflows() {
    return (this._workflows ||= new WorkflowQueue(this.kernel));
  }
  get publicResearch() {
    return (this._publicResearch ||= new PublicResearch(this.workflows));
  }
  get telegramNative() {
    return (this._telegramNative ||= new TelegramNativeGate(
      this.workflows,
      this.env,
    ));
  }
  get telegramReader() {
    return (this._telegramReader ||= new TelegramWorkflowReader(
      this.workflows,
      this.env,
    ));
  }
  get replyPipeline() {
    return (this._replyPipeline ||= new TelegramPreparedReplies(
      this.workflows,
      this.telegramReader,
      this.env,
    ));
  }
  get telegramTaskBuilder() {
    this.replyPipeline;
    return (this._telegramTaskBuilder ||= new TelegramTaskBuilder(
      this.workflows,
      this.telegramNative,
      this.env,
    ));
  }
  async fetch(request) {
    try {
      const path = new URL(request.url).pathname;
      const rawBody = request.method === "GET" ? "{}" : await request.text();
      const body = JSON.parse(rawBody);
      const nodeId = request.headers.get("X-Control-Node");
      const fenced = this.env.FALLBACK_NODE_ID;
      if (path.startsWith("/v2/device/")) {
        if (request.method !== "POST")
          throw new ControlError("MethodNotAllowed", 405);
        this.workflows; // Additive schema; no snapshot sync or legacy migration bypass.
        const devices = new DeviceControl(this.kernel);
        if (["/v2/device/register", "/v2/device/revoke"].includes(path)) {
          if (request.headers.get("X-Control-Role") !== "operator")
            throw new ControlError("Unauthorized", 403);
          return Response.json(
            path.endsWith("/revoke")
              ? devices.revoke(body.node_id)
              : devices.register(body),
          );
        }
        if (request.headers.get("X-Control-Role") !== "node")
          throw new ControlError("Unauthorized", 403);
        const device = await devices.authenticate(request, path, body);
        let value;
        if (path === "/v2/device/prepare") {
          let resolved = null;
          if (typeof body.target === "string" && body.target.startsWith("@")) {
            if (
              !device.scope.targets.includes(body.target) ||
              body.account_id !== device.scope.account_ids[0] ||
              body.account_id !== this.env.TELEGRAM_EXECUTOR_ACCOUNT
            )
              throw new ControlError("DeviceTargetNotGranted", 403);
            const r = await this.env.TELEGRAM_EXECUTOR.fetch(
              "https://telegram-executor/internal",
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  account_id: body.account_id,
                  operation: "resolve_target",
                  target: body.target,
                }),
              },
            );
            const result = await r.json();
            if (!r.ok || !result.target)
              throw new ControlError(
                "TelegramTargetResolutionUnavailable",
                409,
              );
            resolved = result.target;
          }
          value = await devices.prepare(device, body, resolved);
        } else if (path === "/v2/device/status")
          value = devices.status(device, body.plan_id);
        else if (path === "/v2/device/cancel")
          value = devices.cancel(device, body.plan_id);
        else if (path === "/v2/device/start") {
          const owned = devices.owned(device, body.plan_id);
          const job = this.kernel.one(
            "SELECT state,attempt_id FROM jobs WHERE id=?",
            owned.job_id,
          );
          if (job.attempt_id || !["pending"].includes(job.state))
            return Response.json(devices.status(device, body.plan_id));
          const task = JSON.parse(
            this.kernel.one(
              "SELECT payload FROM tasks WHERE id=?",
              owned.task_id,
            ).payload,
          );
          if (
            this.kernel.one("SELECT state FROM tasks WHERE id=?", owned.task_id)
              .state === "cancelled"
          )
            throw new ControlError("DeviceTaskCancelled", 409);
          this.deviceStarts ||= new Set();
          if (this.deviceStarts.has(device.scope.account_ids[0]))
            throw new ControlError("DeviceAccountStartBusy", 409);
          this.deviceStarts.add(device.scope.account_ids[0]);
          try {
            task.state = "active";
            this.kernel.tx(() => {
              this.kernel.sql.exec(
                "UPDATE tasks SET state='active',payload=? WHERE id=?",
                canonical(task),
                owned.task_id,
              );
              this.kernel.sql.exec(
                "UPDATE workplans SET state='active' WHERE id=?",
                body.plan_id,
              );
            });
            const activated = await this.telegramNative.activate({
              plan_id: body.plan_id,
              source: "explicit_device_start",
            });
            if (!activated.activated)
              return Response.json({
                plan_id: body.plan_id,
                state: "waiting_cooldown",
                submitted: false,
              });
            this.telegramNative.link({
              plan_id: body.plan_id,
              step_id: "explicit:send",
              job_id: owned.job_id,
              source: "explicit_device_start",
            });
            value = await this.telegramNative.dispatch({
              plan_id: body.plan_id,
              step_id: "explicit:send",
            });
          } catch (error) {
            const current = this.kernel.one(
              "SELECT attempt_id,state FROM jobs WHERE id=?",
              owned.job_id,
            );
            if (!current.attempt_id && current.state === "pending") {
              this.kernel.sql.exec(
                "UPDATE tasks SET state='paused' WHERE id=? AND state='active'",
                owned.task_id,
              );
              this.kernel.sql.exec(
                "UPDATE workplans SET state='paused' WHERE id=? AND state='active'",
                body.plan_id,
              );
            }
            throw error;
          } finally {
            this.deviceStarts.delete(device.scope.account_ids[0]);
          }
        } else throw new ControlError("NotFound", 404);
        return Response.json(value);
      }

      if (path === "/v1/standby-check") {
        if (
          request.headers.get("X-Control-Role") !== "node" ||
          request.method !== "POST"
        )
          throw new ControlError("Unauthorized", 401);
        const checked = standbyCheck(
          this.ctx.storage,
          nodeId,
          request.headers.get("X-Control-Node-Hash"),
          body,
          { node: fenced, store: this.env.FALLBACK_STORE_ID },
        );
        if (checked.checkpoint) {
          checked.offline_permit = await issuePermit(
            checked.checkpoint,
            this.env.OPERATOR_TOKEN,
          );
          delete checked.checkpoint;
        }
        return Response.json(checked, {
          headers: { "Cache-Control": "no-store" },
        });
      }
      if (
        !path.startsWith("/v2/") &&
        this.kernel.one(
          "SELECT name FROM sqlite_master WHERE type='table' AND name='device_tasks'",
        ) &&
        this.kernel.one(
          "SELECT plan_id FROM device_tasks WHERE plan_id=? OR task_id=? OR job_id=? OR task_id=? OR job_id=?",
          body.plan_id || body.id || "",
          body.task_id || "",
          body.job_id || "",
          body.id || "",
          body.id || "",
        ) &&
        path !== "/v1/executor/telegram/gate"
      )
        throw new ControlError("DeviceTaskRequiresOwnedRoute", 403);
      // The static deployment fence cannot disappear when storage is out of
      // writes. It also blocks old clients and other nodes on the online path.
      if (
        fenced &&
        !path.startsWith("/v1/workflow/") &&
        request.headers.get("X-Control-Role") === "node" &&
        nodeId !== fenced
      )
        throw new ControlError("LocalChannelRequiresSingleNode", 403);
      if (fenced && path === "/v1/nodes" && body.id !== fenced)
        throw new ControlError("LocalChannelRequiresSingleNode", 403);
      // In fenced mode all control mutations must come from the bound local
      // database, where stop/pause/limits are persisted before being mirrored.
      if (
        fenced &&
        [
          "/v1/settings",
          "/v1/sync",
          "/v1/account-channel",
          "/v1/workflow/create",
          "/v1/workflow/node",
          "/v1/workflow/state",
          "/v1/workflow/start-public",
          "/v1/workflow/stop-public",
          "/v1/workflow/replies/start",
          "/v1/workflow/replies/stop",
          "/v1/workflow/replies/prepare",
          "/v1/workflow/replies/reconfirm",
          "/v1/executor/telegram/login-begin",
          "/v1/executor/telegram/login-poll",
          "/v1/executor/telegram/read",
          "/v1/executor/telegram/activate",
          "/v1/executor/telegram/link-native",
          "/v1/executor/telegram/dispatch-native",
          "/v1/executor/telegram/start-reader",
          "/v1/executor/telegram/stop-reader",
        ].includes(path)
      ) {
        if (body.local_store_id !== this.env.FALLBACK_STORE_ID)
          throw new ControlError("LocalControlSourceRequired", 403);
        const owner = this.kernel.one(
          "SELECT token_hash,enabled FROM nodes WHERE id=?",
          fenced,
        );
        const proof = request.headers.get("X-Local-Control-Proof");
        if (
          !owner?.enabled ||
          (proof !== (await controlProof(rawBody, owner.token_hash)) &&
            proof !== (await controlProof(canonical(body), owner.token_hash)))
        )
          throw new ControlError("LocalControlSourceRequired", 403);
      }
      delete body.local_store_id;
      let result;
      if (path === "/internal/public-research") {
        if (request.headers.get("X-Control-Role") !== "scheduler")
          throw new ControlError("Unauthorized", 403);
        result = {};
        const stages = [["public", () => this.publicResearch.tick()]];
        if (this.env.TELEGRAM_EXECUTOR)
          stages.push(
            ["telegram", () => this.telegramReader.tick()],
            ["prepared_replies", () => this.replyPipeline.tick()],
            ["task_builder", () => this.telegramTaskBuilder.tick()],
          );
        if (this.env.TELEGRAM_EXECUTOR && this.env.TG_QUEUE_BRIDGE)
          stages.push(["native", () => this.telegramNative.tick()]);
        for (const [name, run] of stages) {
          try {
            result[name] = await run();
          } catch (error) {
            result[name] = {
              deferred: true,
              reason:
                error instanceof ControlError
                  ? error.message
                  : "WorkflowStageUnavailable",
            };
          }
        }
      } else if (path.startsWith("/v1/executor/telegram/")) {
        if (path === "/v1/executor/telegram/gate") {
          if (request.headers.get("X-Control-Role") !== "telegram-native")
            throw new ControlError("Unauthorized", 403);
          result = await this.telegramNative.authorize(body);
        } else {
          if (
            request.headers.get("X-Control-Role") !== "operator" ||
            request.method !== "POST"
          )
            throw new ControlError("Unauthorized", 403);
          if (path === "/v1/executor/telegram/activate")
            result = await this.telegramNative.activate(body);
          else if (path === "/v1/executor/telegram/link-native")
            result = this.telegramNative.link(body);
          else if (path === "/v1/executor/telegram/dispatch-native")
            result = await this.telegramNative.dispatch(body);
          else if (path === "/v1/executor/telegram/start-reader") {
            result = await this.telegramReader.start(body.plan_id, body.source);
            this.publicResearch.stop(
              body.plan_id,
              "authenticated_reader_enabled",
            );
            this.ctx.waitUntil(this.telegramReader.tick());
          } else if (path === "/v1/executor/telegram/stop-reader")
            result = this.telegramReader.stop(body.plan_id, body.source);
          else
            result = await telegramExecutorBridge(
              this.env,
              this.workflows,
              path,
              body,
              request.headers.get("X-AP-Request-ID"),
            );
          // Login confirms identity only. Reader execution remains behind the
          // separate, explicit start-reader route above, including its guards.
        }
      } else if (path.startsWith("/v1/workflow/")) {
        const operator = request.headers.get("X-Control-Role") === "operator";
        if (path.startsWith("/v1/workflow/replies/")) {
          if (!operator || request.method !== "POST")
            throw new ControlError("Unauthorized", 403);
          if (path.endsWith("/start"))
            result = await this.replyPipeline.start(body);
          else if (path.endsWith("/stop"))
            result = this.replyPipeline.stop(body);
          else if (path.endsWith("/prepare"))
            result = await this.replyPipeline.prepare(body);
          else if (path.endsWith("/reconfirm"))
            result = await this.replyPipeline.reconfirm(body);
          else if (path.endsWith("/status"))
            result = this.replyPipeline.snapshot(body.plan_id);
          else throw new ControlError("NotFound", 404);
        } else if (
          path === "/v1/workflow/create" &&
          operator &&
          request.method === "POST"
        )
          result = await this.workflows.create(body);
        else if (
          path === "/v1/workflow/state" &&
          operator &&
          request.method === "POST"
        )
          result = this.workflows.changeState(body.id, body.state, body.source);
        else if (
          path === "/v1/workflow/start-public" &&
          operator &&
          request.method === "POST"
        ) {
          result = await this.publicResearch.start(
            body.id,
            body.source,
            this.env.OPERATOR_TOKEN,
          );
          this.ctx.waitUntil(
            this.publicResearch
              .tick()
              .catch(() => console.error("PublicResearchDeferred")),
          );
        } else if (
          path === "/v1/workflow/stop-public" &&
          operator &&
          request.method === "POST"
        ) {
          result = this.publicResearch.stop(body.id, body.source);
        } else if (
          path === "/v1/workflow/status" &&
          operator &&
          request.method === "POST"
        )
          result = {
            ...this.workflows.status(body.id, {
              offset: body.offset || 0,
              limit: body.limit || 100,
            }),
            public_research: this.publicResearch.snapshot(body.id),
            authenticated_telegram_reader: this.env.TELEGRAM_EXECUTOR
              ? this.telegramReader.snapshot(body.id)
              : null,
            prepared_reply_pipeline: this.replyPipeline.snapshot(body.id),
            telegram_executor_activation:
              this.telegramNative.activationSnapshot(body.id),
          };
        else if (
          path === "/v1/workflow/node" &&
          operator &&
          request.method === "POST"
        )
          result = this.workflows.registerNode(body);
        else if (!operator && request.method === "POST") {
          const node = this.workflows.node(
            nodeId,
            request.headers.get("X-Control-Node-Hash"),
          );
          if (path === "/v1/workflow/claim")
            result = this.workflows.claim(node, body.id);
          else if (path === "/v1/workflow/result")
            result = this.workflows.finish(node, body);
          else throw new ControlError("NotFound", 404);
        } else throw new ControlError("NotFound", 404);
      } else if (request.headers.get("X-Control-Role") === "operator") {
        if (path === "/v1/status") result = this.kernel.status();
        else if (path === "/v1/settings" && request.method === "PUT")
          result = this.kernel.configure(body);
        else if (path === "/v1/nodes" && request.method === "POST")
          result = this.kernel.registerNode(body);
        else if (path === "/v1/sync" && request.method === "POST")
          result = this.kernel.sync(body);
        else if (path === "/v1/account-channel" && request.method === "POST") {
          if (fenced && body.node_id !== fenced)
            throw new ControlError("LocalChannelRequiresSingleNode", 403);
          result = this.kernel.setAccountChannel(body);
        } else throw new ControlError("NotFound", 404);
      } else {
        const node = this.kernel.node(
          request.headers.get("X-Control-Node"),
          request.headers.get("X-Control-Node-Hash"),
        );
        if (
          path === "/v1/account-channel/status" &&
          request.method === "POST"
        ) {
          if (!node.accounts.includes(body.account_id))
            throw new ControlError("JobNotAssigned", 403);
          result = { channel: this.kernel.accountChannel(body.account_id) };
        } else if (path === "/v1/heartbeat" && request.method === "POST")
          result = { ready: true, node_id: node.id };
        else if (path === "/v1/claim" && request.method === "POST")
          result = {
            job: this.kernel.claim(node, body.pool, body.expected_id || null),
          };
        else if (path === "/v1/job" && request.method === "POST") {
          this.kernel.sweep();
          const job = this.kernel.one(
            "SELECT id,state,account_id,pool FROM jobs WHERE id=?",
            body.id,
          );
          if (
            job &&
            (!node.accounts.includes(job.account_id) ||
              !node.pools.includes(job.pool))
          )
            throw new ControlError("JobNotAssigned", 403);
          result = { job: job || null };
        } else if (path === "/v1/renew" && request.method === "POST")
          result = this.kernel.renew(node, body.id, body.lease_token);
        else if (path === "/v1/authorize" && request.method === "POST")
          result = this.kernel.authorize(
            node,
            body.id,
            body.lease_token,
            body.attempt_id,
          );
        else if (path === "/v1/complete" && request.method === "POST")
          result = this.kernel.complete(node, body.id, body.lease_token, body);
        else throw new ControlError("NotFound", 404);
      }
      if (
        this.checkAlarmOnActivation ||
        path === "/v1/sync" ||
        path === "/v1/claim"
      ) {
        await scheduleMaintenance(this.kernel, this.ctx.storage);
        this.checkAlarmOnActivation = false;
      }
      return Response.json(result, {
        headers: { "Cache-Control": "no-store" },
      });
    } catch (e) {
      const failure = cloudFailure(e);
      if (failure.error === "CloudWriteQuotaExceeded")
        return Response.json(failure, {
          status: 503,
          headers: {
            "Cache-Control": "no-store",
            "Retry-After": String(failure.retry_after_seconds),
          },
        });
      return Response.json(
        { error: e instanceof ControlError ? e.message : "ControlPlaneError" },
        { status: e.status || 500, headers: { "Cache-Control": "no-store" } },
      );
    }
  }
  async alarm() {
    try {
      this.kernel.sweep();
      await scheduleMaintenance(this.kernel, this.ctx.storage);
    } catch (error) {
      if (cloudFailure(error).error !== "CloudWriteQuotaExceeded") throw error;
      // An exhausted write budget cannot run migration or expiry maintenance.
      // Do not poison read-only standby requests with a retrying failed alarm.
      console.error("CloudWriteQuotaExceeded");
    }
  }
}
export default {
  async scheduled(controller, env, ctx) {
    if (!env.OPERATOR_TOKEN) return;
    const stub = env.CONTROL.get(
      env.CONTROL.idFromName(env.WORKSPACE_ID || "auto-promotion"),
    );
    ctx.waitUntil(
      stub
        .fetch(
          new Request("https://internal/internal/public-research", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Control-Role": "scheduler",
            },
            body: "{}",
          }),
        )
        .then((r) => {
          if (!r.ok) throw new Error("PublicResearchDeferred");
        }),
    );
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET")
      return Response.json({
        status: "ok",
        service: "auto-promotion-control",
        schema: 2,
        fallback: {
          node_id: env.FALLBACK_NODE_ID || null,
          store_id: env.FALLBACK_STORE_ID || null,
        },
      });
    if (!env.OPERATOR_TOKEN)
      return Response.json({ error: "ServiceNotConfigured" }, { status: 503 });
    if (url.pathname === "/v1/executor/telegram/gate") {
      const raw = await request.text();
      if (
        request.method !== "POST" ||
        raw.length > 16000 ||
        !(await verifyNativeGate(
          env.TG_QUEUE_BRIDGE,
          raw,
          request.headers.get("X-Telegram-Executor-Proof"),
        ))
      )
        return Response.json({ error: "Unauthorized" }, { status: 403 });
      const stub = env.CONTROL.get(
        env.CONTROL.idFromName(env.WORKSPACE_ID || "auto-promotion"),
      );
      return stub.fetch(
        new Request("https://internal" + url.pathname, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Control-Role": "telegram-native",
          },
          body: raw,
        }),
      );
    }
    const token =
      request.headers.get("Authorization")?.replace(/^Bearer /, "") || "";
    const headers = new Headers({ "Content-Type": "application/json" });
    if (request.headers.has("X-Local-Control-Proof"))
      headers.set(
        "X-Local-Control-Proof",
        request.headers.get("X-Local-Control-Proof"),
      );
    headers.set(
      "X-AP-Request-ID",
      executorRequestId(request.headers.get("X-AP-Request-ID")),
    );
    if (token && (await sha(token)) === (await sha(env.OPERATOR_TOKEN)))
      headers.set("X-Control-Role", "operator");
    else {
      const node = request.headers.get("X-Node-ID");
      if (!node || !token)
        return Response.json({ error: "Unauthorized" }, { status: 401 });
      headers.set("X-Control-Role", "node");
      headers.set("X-Control-Node", node);
      headers.set("X-Control-Node-Hash", await sha(token));
    }
    const length = Number(request.headers.get("Content-Length") || 0);
    if (length > 4_000_000)
      return Response.json({ error: "SnapshotTooLarge" }, { status: 413 });
    const body = request.method === "GET" ? undefined : await request.text();
    if (body && body.length > 4_000_000)
      return Response.json({ error: "SnapshotTooLarge" }, { status: 413 });
    const stub = env.CONTROL.get(
      env.CONTROL.idFromName(env.WORKSPACE_ID || "auto-promotion"),
    );
    try {
      const response = await stub.fetch(
        new Request("https://internal" + url.pathname, {
          method: request.method,
          headers,
          body,
        }),
      );
      if (
        url.pathname === "/v1/standby-check" &&
        response.status === 503 &&
        (await response.clone().json()).error === "CloudWriteQuotaExceeded"
      ) {
        const input = JSON.parse(body);
        try {
          if (
            headers.get("X-Control-Role") !== "node" ||
            headers.get("X-Control-Node") !== env.FALLBACK_NODE_ID
          )
            throw new ControlError("Unauthorized", 403);
          return Response.json(
            await offlineCheck(input.permit, token, input, env),
            { headers: { "Cache-Control": "no-store" } },
          );
        } catch (error) {
          if (error instanceof ControlError)
            return Response.json(
              { error: error.message },
              { status: error.status },
            );
          throw error;
        }
      }
      return response;
    } catch (error) {
      const failure = cloudFailure(error);
      console.error(failure.error);
      return Response.json(failure, {
        status: 503,
        headers: {
          "Cache-Control": "no-store",
          "Retry-After": String(failure.retry_after_seconds),
        },
      });
    }
  },
};
