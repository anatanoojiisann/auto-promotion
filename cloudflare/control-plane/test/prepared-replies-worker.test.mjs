import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from "miniflare";
import { canonical, sha } from "../kernel.mjs";
import { controlProof } from "../offline-permit.mjs";
import { fixture } from "./prepared-replies-fixture.mjs";

test("actual Worker accepts exact reply packets without a model and fences all pipeline mutations", async () => {
  const f = await fixture();
  let platformCalls = 0;
  const files = [
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
    "telegram-prepared-replies.mjs",
  ];
  const mf = new Miniflare(
    convertV4MiniflareOptions({
      name: "prepared-reply-worker-test",
      modules: await Promise.all(
        files.map(async (name) => ({
          type: "ESModule",
          path: fileURLToPath(new URL("../" + name, import.meta.url)),
          contents: await readFile(
            new URL("../" + name, import.meta.url),
            "utf8",
          ),
        })),
      ),
      modulesRoot: fileURLToPath(new URL("..", import.meta.url)),
      compatibilityDate: "2025-09-01",
      bindings: {
        OPERATOR_TOKEN: "synthetic-operator",
        FALLBACK_NODE_ID: "only-node",
        FALLBACK_STORE_ID: "only-db",
        WORKSPACE_ID: "synthetic",
        TELEGRAM_EXECUTOR_ACCOUNT: "a",
        TG_QUEUE_BRIDGE: "synthetic-bridge",
      },
      serviceBindings: {
        TELEGRAM_EXECUTOR: async () => {
          platformCalls++;
          return Response.json({ login_state: "not_logged_in" });
        },
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
    value,
    { operator = true, proof = true } = {},
  ) => {
    const body = { ...value, local_store_id: "only-db" };
    return mf.dispatchFetch("https://local" + path, {
      method: "POST",
      headers: {
        Authorization:
          "Bearer " + (operator ? "synthetic-operator" : "synthetic-node"),
        "X-Node-ID": "only-node",
        "Content-Type": "application/json",
        "X-Local-Control-Proof": proof
          ? await controlProof(canonical(body), await sha("synthetic-node"))
          : "wrong",
      },
      body: canonical(body),
    });
  };
  try {
    let r = await request("/v1/nodes", {
      id: "only-node",
      token_hash: await sha("synthetic-node"),
      pools: ["telegram.mtproto"],
      account_ids: ["a"],
    });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request("/v1/sync", {
      accounts: [
        JSON.parse(
          f.k.one("SELECT payload FROM accounts WHERE id='a'").payload,
        ),
      ],
      tasks: [f.parent],
      jobs: [],
    });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request("/v1/workflow/create", {
      definition: f.q.plan(f.id).definition,
      source: "synthetic_user_start",
    });
    assert.equal(r.status, 200, await r.clone().text());
    for (const action of ["start", "stop", "prepare", "reconfirm"]) {
      r = await request(
        "/v1/workflow/replies/" + action,
        { plan_id: f.id },
        { proof: false },
      );
      assert.equal(r.status, 403, action);
      r = await request(
        "/v1/workflow/replies/" + action,
        { plan_id: f.id },
        { operator: false },
      );
      assert.equal(r.status, 403, action);
    }
    r = await request("/v1/workflow/replies/start", {
      plan_id: f.id,
      reference_task_id: f.parent.id,
      source: "synthetic_explicit_start",
    });
    assert.equal(r.status, 200, await r.clone().text());
    assert.equal((await r.json()).model_service_required, false);
    r = await request("/v1/workflow/replies/prepare", f.packet);
    assert.equal(r.status, 200, await r.clone().text());
    assert.equal((await r.json()).text, f.packet.text);
    r = await request("/v1/workflow/replies/prepare", {
      ...f.packet,
      text: f.packet.text.replace("short note", "brief note"),
    });
    assert.equal(r.status, 409);
    assert.equal((await r.json()).error, "FrozenPreparedReplyChanged");
    r = await request("/v1/workflow/status", { id: f.id, limit: 1 });
    assert.equal(r.status, 200, await r.clone().text());
    const status = await r.json();
    assert.equal(status.prepared_reply_pipeline.prepared_replies, 1);
    assert.equal(status.prepared_reply_pipeline.generated_tasks, 0);
    assert.equal(status.prepared_reply_pipeline.model_service_required, false);
    assert.equal(status.native_jobs.length, 0);
    assert.equal(
      platformCalls,
      0,
      "start and prepare only save work, not platform operations",
    );
  } finally {
    await mf.dispose();
    f.db.close();
  }
});
