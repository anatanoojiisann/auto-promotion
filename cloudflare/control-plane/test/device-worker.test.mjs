import assert from "node:assert/strict";
import test from "node:test";
import { readFile, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from "miniflare";
import { sha, canonical } from "../kernel.mjs";
import { controlProof } from "../offline-permit.mjs";
import { signNativeGate } from "../telegram-native-gate.mjs";
test("actual Worker: new device draft/start/duplicate/cancel, legacy fence and cross-device isolation", async () => {
  const root = new URL("../", import.meta.url);
  let mf,
    sends = 0;
  const names = (await readdir(root))
    .filter((n) => n.endsWith(".mjs"))
    .sort((a, b) =>
      a === "worker.mjs" ? -1 : b === "worker.mjs" ? 1 : a.localeCompare(b),
    );
  const options = {
    name: "device-e2e",
    modules: await Promise.all(
      names.map(async (n) => ({
        type: "ESModule",
        path: fileURLToPath(new URL(n, root)),
        contents: await readFile(new URL(n, root), "utf8"),
      })),
    ),
    modulesRoot: fileURLToPath(root),
    compatibilityDate: "2025-09-01",
    bindings: {
      OPERATOR_TOKEN: "synthetic-operator",
      WORKSPACE_ID: "synthetic",
      FALLBACK_NODE_ID: "legacy",
      FALLBACK_STORE_ID: "legacy-store",
      TELEGRAM_EXECUTOR_ACCOUNT: "a",
      TG_QUEUE_BRIDGE: "synthetic-bridge",
    },
    durableObjects: {
      CONTROL: { className: "WorkspaceCoordinator", useSQLite: true },
    },
    cf: false,
    log: new Log(LogLevel.ERROR),
    serviceBindings: {
      TELEGRAM_EXECUTOR: async (req) => {
        const b = await req.json();
        if (b.operation === "health")
          return Response.json({ identity_verified: true });
        if (b.operation === "status")
          return Response.json({
            login_state: "authorized",
            last_verified_at: new Date().toISOString(),
          });
        const raw = JSON.stringify(b);
        const response = await mf.dispatchFetch(
          "https://local/v1/executor/telegram/gate",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Telegram-Executor-Proof": await signNativeGate(
                "synthetic-bridge",
                raw,
              ),
            },
            body: raw,
          },
        );
        assert.equal(response.status, 200);
        const gate = await response.json();
        assert.equal(gate.job.payload.action, "send_text");
        assert.equal(gate.job.payload.text, "synthetic text");
        sends++;
        return Response.json({
          status: "confirmed",
          verified: true,
          message_ids: [9],
          external_reference: "telegram:synthetic:9",
        });
      },
    },
  };
  mf = new Miniflare(convertV4MiniflareOptions(options));
  const op = async (path, body) =>
    mf.dispatchFetch("https://local" + path, {
      method: "POST",
      headers: {
        Authorization: "Bearer synthetic-operator",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
  const device = {
    node_id: "device_e2e_000001",
    store_id: "store_e2e_000001",
    token_hash: await sha("synthetic-device"),
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    scope: {
      account_ids: ["a"],
      targets: ["-1002123962275"],
      action: "send_text",
      max_messages: 1,
    },
  };
  const call = async (
    action,
    body,
    node = device.node_id,
    token = "synthetic-device",
    store = device.store_id,
  ) => {
    const path = "/v2/device/" + action;
    body = { ...body, store_id: store };
    const proof = await controlProof(
      canonical({ protocol: 2, method: "POST", path, node_id: node, body }),
      await sha(token),
    );
    return mf.dispatchFetch("https://local" + path, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + token,
        "X-Node-ID": node,
        "X-Local-Control-Proof": proof,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
  };
  try {
    const ns = await mf.getDurableObjectNamespace("CONTROL");
    const stub = ns.get(ns.idFromName("synthetic"));
    // Synthetic setup through internal role only; no production credential or platform.
    const seed = async (path, b) =>
      stub.fetch("https://internal" + path, {
        method: "POST",
        headers: {
          "X-Control-Role": "operator",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(b),
      });
    assert.equal(
      (
        await op("/v1/nodes", {
          id: "legacy",
          token_hash: await sha("synthetic-legacy"),
          pools: ["telegram.mtproto"],
          account_ids: ["a"],
        })
      ).status,
      200,
    );
    const signed = async (path, b) => {
      b = { ...b, local_store_id: "legacy-store" };
      return stub.fetch("https://internal" + path, {
        method: path === "/v1/settings" ? "PUT" : "POST",
        headers: {
          "X-Control-Role": "operator",
          "X-Local-Control-Proof": await controlProof(
            canonical(b),
            await sha("synthetic-legacy"),
          ),
        },
        body: canonical(b),
      });
    };
    assert.equal(
      (
        await signed("/v1/sync", {
          accounts: [
            {
              id: "a",
              identity: "telegram:a",
              platform: "telegram",
              mode: "live",
              limits: { daily: 5, hourly: 5 },
              allowed_actions: ["send_text"],
            },
          ],
          tasks: [],
          jobs: [],
        })
      ).status,
      200,
    );
    assert.equal(
      (await signed("/v1/settings", { global_stop: false, live_enabled: true }))
        .status,
      200,
    );
    assert.equal((await op("/v2/device/register", device)).status, 200);
    const r = await call("prepare", {
      request_id: "request_e2e_00001",
      account_id: "a",
      target: "-1002123962275",
      text: "synthetic text",
      expires_at: Math.floor(Date.now() / 1000) + 1200,
    });
    assert.equal(r.status, 200);
    const p = await r.json();
    assert.equal(sends, 0);
    const started = await call("start", { plan_id: p.plan_id });
    const result = await started.json();
    assert.equal(started.status, 200, JSON.stringify(result));
    assert.equal(result.submitted, true);
    assert.equal(sends, 1);
    await call("start", { plan_id: p.plan_id });
    assert.equal(sends, 1);
    assert.equal(
      (
        await call(
          "status",
          { plan_id: p.plan_id },
          "foreign_device_01",
          "foreign",
          "foreign_store_01",
        )
      ).status,
      403,
    );
    assert.equal(
      (
        await op("/v1/workflow/state", {
          id: p.plan_id,
          state: "paused",
          source: "synthetic",
        })
      ).status,
      403,
    );
    assert.equal(
      (await op("/v2/device/revoke", { node_id: device.node_id })).status,
      200,
    );
    assert.equal((await call("status", { plan_id: p.plan_id })).status, 403);
  } finally {
    await mf.dispose();
  }
});
