import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from "miniflare";
import { sha, canonical } from "../kernel.mjs";
import { controlProof } from "../offline-permit.mjs";

test("production Worker enforces the fence on online and standby requests", async () => {
  const mf = new Miniflare(convertV4MiniflareOptions({
    name: "dual-channel-test",
    modules: await Promise.all(["worker.mjs", "kernel.mjs", "schema.mjs", "maintenance.mjs", "standby.mjs", "offline-permit.mjs"].map(async name => ({
      type: "ESModule", path: fileURLToPath(new URL("../" + name, import.meta.url)),
      contents: (await readFile(new URL("../" + name, import.meta.url), "utf8"))
        .replace("const checked = standbyCheck", "if (body.synthetic_quota) throw new Error('Exceeded allowed rows written in Durable Objects free tier'); const checked = standbyCheck"),
    }))),
    modulesRoot: fileURLToPath(new URL("..", import.meta.url)),
    compatibilityDate: "2025-09-01",
    bindings: { OPERATOR_TOKEN: "synthetic-operator", FALLBACK_NODE_ID: "only-node",
      FALLBACK_STORE_ID: "only-db", WORKSPACE_ID: "synthetic-only" },
    durableObjects: { CONTROL: { className: "WorkspaceCoordinator", useSQLite: true } },
    cf: false, log: new Log(LogLevel.ERROR),
  }));
  const request = async (path, body, { operator = true, node = "only-node", token = "synthetic-node", method = "POST", proof = true } = {}) => {
    body = operator ? { ...body, local_store_id: "only-db" } : body;
    return mf.dispatchFetch("https://local" + path, { method,
      headers: { Authorization: "Bearer " + (operator ? "synthetic-operator" : token),
        "X-Node-ID": node, "Content-Type": "application/json",
        "X-Local-Control-Proof": proof ? await controlProof(canonical(body), await sha("synthetic-node")) : "forged" },
      body: JSON.stringify(body) });
  };
  try {
    let r = await request("/v1/nodes", { id: "only-node", token_hash: await sha("synthetic-node"),
      pools: ["x.edge"], account_ids: ["synthetic-account"] });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request("/v1/settings", { global_stop: false, live_enabled: true }, { method: "PUT" });
    assert.equal(r.status, 200, await r.clone().text());
    r = await request("/v1/settings", { global_stop: false }, { method: "PUT", proof: false });
    assert.equal(r.status, 403);
    r = await request("/v1/standby-check", { store_id: "only-db" }, { operator: false });
    assert.equal(r.status, 200, await r.clone().text());
    const checkpoint = await r.json();
    assert.equal(checkpoint.allowed, true);
    assert.ok(checkpoint.offline_permit.signature);
    r = await request("/v1/standby-check", { store_id: "only-db", synthetic_quota: true,
      permit: checkpoint.offline_permit }, { operator: false });
    assert.equal(r.status, 200, await r.clone().text());
    assert.equal((await r.json()).checkpoint_used, true);
    r = await request("/v1/standby-check", { store_id: "only-db", synthetic_quota: true }, { operator: false });
    assert.equal(r.status, 403);
    r = await request("/v1/standby-check", { store_id: "only-db", synthetic_quota: true,
      permit: { ...checkpoint.offline_permit, signature: "forged" } }, { operator: false });
    assert.equal(r.status, 403);
    r = await request("/v1/standby-check", { store_id: "only-db", synthetic_quota: true,
      permit: checkpoint.offline_permit }, { operator: false, token: "wrong" });
    assert.equal(r.status, 403);
    r = await request("/v1/standby-check", { store_id: "other-db" }, { operator: false });
    assert.equal(r.status, 403);
    r = await request("/v1/claim", { pool: "x.edge" }, { operator: false, node: "other-node" });
    assert.equal(r.status, 403);
    r = await request("/v1/nodes", { id: "other-node" });
    assert.equal(r.status, 403);
    r = await request("/v1/standby-check", { store_id: "only-db" }, { operator: false, token: "wrong" });
    assert.equal(r.status, 401);
    await request("/v1/settings", { global_stop: true }, { method: "PUT" });
    r = await request("/v1/standby-check", { store_id: "only-db" }, { operator: false });
    assert.equal(r.status, 409);
    assert.equal((await r.json()).error, "ExecutionStopped");
  } finally {
    await mf.dispose();
  }
});
