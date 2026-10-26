import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from "miniflare";
import { sha, canonical } from "../kernel.mjs";
import { controlProof } from "../offline-permit.mjs";
import { authorizeNative } from "../../telegram-executor/native-authorization.mjs";

test("production Worker enforces the fence on online and standby requests", async () => {
  const mf = new Miniflare(
    convertV4MiniflareOptions({
      name: "dual-channel-test",
      modules: await Promise.all(
        [
          "worker.mjs",
          "kernel.mjs",
          "schema.mjs",
          "maintenance.mjs",
          "standby.mjs",
          "offline-permit.mjs",
          "workflow-queue.mjs",
          "public-research.mjs",
          "telegram-executor-bridge.mjs",
          "telegram-native-gate.mjs",
          "telegram-workflow-reader.mjs",
          "telegram-reply-policy.mjs",
          "telegram-task-builder.mjs",
          "device-control.mjs",
          "telegram-target.mjs",
          "telegram-prepared-replies.mjs",
        ].map(async (name) => ({
          type: "ESModule",
          path: fileURLToPath(new URL("../" + name, import.meta.url)),
          contents: (
            await readFile(new URL("../" + name, import.meta.url), "utf8")
          ).replace(
            "const checked = standbyCheck",
            "if (body.synthetic_quota) throw new Error('Exceeded allowed rows written in Durable Objects free tier'); const checked = standbyCheck",
          ),
        })),
      ),
      modulesRoot: fileURLToPath(new URL("..", import.meta.url)),
      compatibilityDate: "2025-09-01",
      bindings: {
        OPERATOR_TOKEN: "synthetic-operator",
        FALLBACK_NODE_ID: "only-node",
        FALLBACK_STORE_ID: "only-db",
        WORKSPACE_ID: "synthetic-only",
        LOCAL_CHANNEL_UNTIL: String(Date.now() / 1000 + 3600),
        TG_QUEUE_BRIDGE: "synthetic-executor-bridge",
        TELEGRAM_EXECUTOR_ACCOUNT: "synthetic-account",
      },
      serviceBindings: {
        TELEGRAM_EXECUTOR: async () =>
          Response.json({
            status: "authorized",
            reader_started: false,
            last_verified_at: "2026-10-20T00:00:00Z",
          }),
      },
      durableObjects: {
        CONTROL: { className: "WorkspaceCoordinator", useSQLite: true },
      },
      cf: false,
      log: new Log(LogLevel.ERROR),
    }),
  );
  const request = async (
    path,
    body,
    {
      operator = true,
      node = "only-node",
      token = "synthetic-node",
      method = "POST",
      proof = true,
      requestId,
    } = {},
  ) => {
    body = operator ? { ...body, local_store_id: "only-db" } : body;
    return mf.dispatchFetch("https://local" + path, {
      method,
      headers: {
        Authorization: "Bearer " + (operator ? "synthetic-operator" : token),
        "X-Node-ID": node,
        "Content-Type": "application/json",
        ...(requestId ? { "X-AP-Request-ID": requestId } : {}),
        "X-Local-Control-Proof": proof
          ? await controlProof(canonical(body), await sha("synthetic-node"))
          : "forged",
      },
      body: JSON.stringify(body),
    });
  };
  try {
    let gateResult;
    const executor = {
      TG_QUEUE_BRIDGE: "synthetic-executor-bridge",
      CONTROL_INTERNAL: {
        async fetch(url, options) {
          const response = await mf.dispatchFetch(url, options);
          gateResult = await response.clone().json();
          return response;
        },
      },
    };
    // Exercise the executor's production transport against the real control
    // router: a signed, unlinked job must reach the native lease guard.
    await assert.rejects(
      authorizeNative(executor, { job_id: "synthetic-unlinked" }),
      /NativeGateRejected/,
    );
    assert.equal(gateResult.error, "NativeWorkflowLinkRequired");
    await assert.rejects(
      authorizeNative(
        { ...executor, TG_QUEUE_BRIDGE: "wrong-key" },
        {
          job_id: "synthetic-unlinked",
        },
      ),
      /NativeGateRejected/,
    );
    assert.equal(gateResult.error, "Unauthorized");
    let r = await request("/v1/nodes", {
      id: "only-node",
      token_hash: await sha("synthetic-node"),
      pools: ["x.edge"],
      account_ids: ["synthetic-account"],
    });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request(
      "/v1/settings",
      { global_stop: false, live_enabled: true },
      { method: "PUT" },
    );
    assert.equal(r.status, 200, await r.clone().text());
    r = await request(
      "/v1/settings",
      { global_stop: false },
      { method: "PUT", proof: false },
    );
    assert.equal(r.status, 403);
    // Python JSON preserves integral float notation; authenticate the exact
    // signed request bytes without rewriting frozen policies or their hashes.
    const raw =
      '{"global_stop":false,"live_enabled":true,"local_store_id":"only-db","max_workers":6.0}';
    const rawHeaders = {
      Authorization: "Bearer synthetic-operator",
      "Content-Type": "application/json",
      "X-Local-Control-Proof": await controlProof(
        raw,
        await sha("synthetic-node"),
      ),
    };
    r = await mf.dispatchFetch("https://local/v1/settings", {
      method: "PUT",
      headers: rawHeaders,
      body: raw,
    });
    assert.equal(r.status, 200, await r.clone().text());
    r = await mf.dispatchFetch("https://local/v1/settings", {
      method: "PUT",
      headers: rawHeaders,
      body: raw.replace("false", "true"),
    });
    assert.equal(r.status, 403);
    r = await request(
      "/v1/standby-check",
      { store_id: "only-db" },
      { operator: false },
    );
    assert.equal(r.status, 200, await r.clone().text());
    const checkpoint = await r.json();
    assert.equal(checkpoint.allowed, true);
    assert.ok(checkpoint.offline_permit.signature);
    r = await request(
      "/v1/standby-check",
      {
        store_id: "only-db",
        synthetic_quota: true,
        permit: checkpoint.offline_permit,
      },
      { operator: false },
    );
    assert.equal(r.status, 200, await r.clone().text());
    assert.equal((await r.json()).checkpoint_used, true);
    r = await request(
      "/v1/standby-check",
      { store_id: "only-db", synthetic_quota: true },
      { operator: false },
    );
    assert.equal(r.status, 403);
    r = await request(
      "/v1/standby-check",
      {
        store_id: "only-db",
        synthetic_quota: true,
        permit: { ...checkpoint.offline_permit, signature: "forged" },
      },
      { operator: false },
    );
    assert.equal(r.status, 403);
    r = await request(
      "/v1/standby-check",
      {
        store_id: "only-db",
        synthetic_quota: true,
        permit: checkpoint.offline_permit,
      },
      { operator: false, token: "wrong" },
    );
    assert.equal(r.status, 403);
    r = await request(
      "/v1/standby-check",
      { store_id: "other-db" },
      { operator: false },
    );
    assert.equal(r.status, 403);
    r = await request(
      "/v1/claim",
      { pool: "x.edge" },
      { operator: false, node: "other-node" },
    );
    assert.equal(r.status, 403);
    r = await request("/v1/nodes", { id: "other-node" });
    assert.equal(r.status, 403);
    r = await request(
      "/v1/standby-check",
      { store_id: "only-db" },
      { operator: false, token: "wrong" },
    );
    assert.equal(r.status, 401);
    await request("/v1/settings", { global_stop: true }, { method: "PUT" });
    r = await request(
      "/v1/standby-check",
      { store_id: "only-db" },
      { operator: false },
    );
    assert.equal(r.status, 409);
    assert.equal((await r.json()).error, "ExecutionStopped");
    await request("/v1/settings", { global_stop: false }, { method: "PUT" });
    await request("/v1/nodes", {
      id: "only-node",
      token_hash: await sha("synthetic-node"),
      pools: ["x.edge"],
      account_ids: ["synthetic-account", "second"],
    });
    const now = Date.now() / 1000;
    const accounts = ["synthetic-account", "second"].map((id) => ({
      id,
      identity: "x:" + id,
      platform: "x",
      mode: "live",
      limits: { daily: 10, hourly: 10 },
      min_interval: 0,
      allowed_actions: ["reply"],
    }));
    const tasks = accounts.map((a, i) => ({
      id: "t-" + i,
      state: "active",
      phase: "ready",
      version: "policy-" + i,
      account_ids: [a.id],
      policy: {
        execution_channel: i === 0 ? "cloud" : "inherit",
        starts_at: new Date((now - 60) * 1000).toISOString(),
        ends_at: new Date((now + 1200) * 1000).toISOString(),
        min_interval: 0,
      },
    }));
    const jobs = accounts.map((a, i) => ({
      id: "send:" + i,
      kind: "send",
      reference_id: "item-" + i,
      task_id: tasks[i].id,
      account_id: a.id,
      pool: "x.edge",
      version: "payload-" + i,
      payload: { text: "synthetic" },
      action: "reply",
      state: "not_attempted",
      available_at: now - 1,
      expires_at: now + 300,
    }));
    r = await request("/v1/sync", {
      revision: 1,
      accounts,
      tasks,
      jobs,
      quota_events: [],
    });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request(
      "/v1/claim",
      { pool: "x.edge", expected_id: "send:0" },
      { operator: false },
    );
    assert.equal((await r.json()).job, null);
    const definition = {
      id: "WF-INTEGRATION-100",
      name: "synthetic complete queue",
      goal: 100,
      execution_channel: "cloud",
      local_fallback_allowed: false,
      account_ids: ["synthetic-account"],
      native_task_ids: [],
      native_job_ids: [],
      requirements: { language: ["en", "ru"] },
      steps: [
        {
          id: "continue-search",
          stage: "discover",
          depends_on: [],
          input: { query: "synthetic" },
        },
      ],
    };
    r = await request(
      "/v1/workflow/start-public",
      { id: definition.id, source: "explicit" },
      { proof: false },
    );
    assert.equal(r.status, 403);
    r = await request(
      "/v1/workflow/start-public",
      { id: definition.id, source: "explicit" },
      { operator: false },
    );
    assert.equal(r.status, 403);
    r = await request("/internal/public-research", {});
    assert.equal(r.status, 403);
    r = await request(
      "/v1/workflow/create",
      { definition, source: "explicit_human_start" },
      { proof: false },
    );
    assert.equal(r.status, 403);
    r = await request("/v1/workflow/create", {
      definition,
      source: "explicit_human_start",
    });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request("/v1/workflow/status", { id: definition.id });
    assert.equal((await r.json()).counts.waiting_cloud_executor, 1);
    const setupRequestId = "00000000-0000-4000-8000-000000000001";
    r = await request(
      "/v1/executor/telegram/login-poll",
      {
        plan_id: definition.id,
        source: "explicit_login_setup",
      },
      { requestId: setupRequestId },
    );
    assert.equal(r.status, 200, await r.clone().text());
    const login = await r.json();
    assert.equal(login.status, "authorized");
    assert.equal(login.request_id, setupRequestId);
    assert.equal(login.reader_started, false);
    assert.equal(Object.hasOwn(login, "reader"), false);
    r = await request("/v1/workflow/status", { id: definition.id });
    assert.equal((await r.json()).counts.waiting_cloud_executor, 1);
    r = await request(
      "/v1/workflow/claim",
      { id: definition.id },
      { operator: false },
    );
    assert.equal(r.status, 403);
    r = await request("/v1/workflow/node", {
      id: "cloud-reader",
      runtime: "cloud",
      token_hash: await sha("synthetic-workflow"),
      stages: ["discover"],
    });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request(
      "/v1/workflow/claim",
      { id: definition.id },
      { operator: false, node: "cloud-reader", token: "synthetic-workflow" },
    );
    assert.equal(r.status, 200, await r.clone().text());
    const work = (await r.json()).step;
    assert.equal(work.id, "continue-search");
    r = await request(
      "/v1/claim",
      { pool: "x.edge", expected_id: "send:0" },
      { operator: false, node: "cloud-reader", token: "synthetic-workflow" },
    );
    assert.equal(r.status, 403); // Workflow credentials never weaken native execution fencing.
    r = await request("/v1/account-channel", {
      account_id: accounts[0].id,
      task_id: tasks[0].id,
      node_id: "only-node",
      channel: "cloud",
      generation: "1".repeat(32),
      expected_generation: null,
      until: now + 600,
    });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request(
      "/v1/claim",
      { pool: "x.edge", expected_id: "send:0" },
      { operator: false },
    );
    const lease = (await r.json()).job;
    assert.equal(lease.id, "send:0");
    r = await request(
      "/v1/claim",
      { pool: "x.edge", expected_id: "send:1" },
      { operator: false },
    );
    assert.equal((await r.json()).job, null);
    accounts[0].outbound_block_state = {
      state: "active", reason: "x_challenge", since: now,
      until: null, generation: 1,
    };
    r = await request("/v1/sync", { revision: 2, accounts: [accounts[0]], tasks: [], jobs: [] });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request("/v1/authorize", {
      id: lease.id, lease_token: lease.lease_token, attempt_id: "synthetic-blocked-attempt",
    }, { operator: false });
    assert.equal(r.status, 409);
    assert.equal((await r.json()).error, "AccountOutboundBlocked");
    r = await request("/v1/heartbeat", {}, { operator: false });
    const blockedHeartbeat = await r.json();
    assert.equal(blockedHeartbeat.account_blocks[0].account_id, accounts[0].id);
    assert.equal(blockedHeartbeat.account_blocks[0].outbound_block_state.reason, "x_challenge");
    assert.ok(blockedHeartbeat.account_blocks[0].affected_tasks.includes(tasks[0].id));
    r = await request("/v1/complete", {
      id: lease.id, lease_token: lease.lease_token, state: "pending", result: {},
    }, { operator: false });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request("/v1/claim", { pool: "x.edge", expected_id: lease.id }, { operator: false });
    assert.equal((await r.json()).job, null);
  } finally {
    await mf.dispose();
  }
});
