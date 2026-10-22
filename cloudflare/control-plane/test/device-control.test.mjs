import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { ControlKernel, sha, canonical } from "../kernel.mjs";
import { WorkflowQueue } from "../workflow-queue.mjs";
import { DeviceControl } from "../device-control.mjs";
import { controlProof } from "../offline-permit.mjs";
function fixture() {
  const db = new DatabaseSync(":memory:");
  const storage = {
    sql: {
      exec(sql, ...args) {
        if (sql.includes("CREATE TABLE")) {
          db.exec(sql);
          return { toArray: () => [] };
        }
        const s = db.prepare(sql);
        const rows = s.columns().length ? s.all(...args) : (s.run(...args), []);
        return { toArray: () => rows };
      },
    },
    transactionSync(fn) {
      db.exec("BEGIN");
      try {
        const r = fn();
        db.exec("COMMIT");
        return r;
      } catch (e) {
        db.exec("ROLLBACK");
        throw e;
      }
    },
  };
  const k = new ControlKernel(storage, { clock: () => 1800000000 });
  new WorkflowQueue(k);
  k.sync({
    accounts: [
      {
        id: "a",
        identity: "telegram:a",
        platform: "telegram",
        mode: "live",
        limits: { daily: 5 },
        allowed_actions: ["send_text"],
      },
    ],
    tasks: [],
    jobs: [],
  });
  const d = new DeviceControl(k);
  const reg = {
    node_id: "device_test_00001",
    store_id: "store_test_000001",
    token_hash: "a".repeat(64),
    expires_at: 1800003600,
    scope: {
      account_ids: ["a"],
      targets: ["-1002123962275"],
      action: "send_text",
      max_messages: 1,
    },
  };
  return { k, d, reg };
}
test("register is idempotent, cannot rotate or overwrite legacy identity", () => {
  const { k, d, reg } = fixture();
  assert.equal(d.register(reg).existing, false);
  assert.equal(d.register(reg).existing, true);
  for (const patch of [
    { token_hash: "b".repeat(64) },
    { store_id: "store_other_00001" },
    { scope: { ...reg.scope, targets: ["-123"] } },
  ])
    assert.throws(
      () => d.register({ ...reg, ...patch }),
      /DeviceRegistrationConflict/,
    );
  k.registerNode({
    id: "legacy_node_00001",
    token_hash: "c".repeat(64),
    pools: ["telegram.mtproto"],
    account_ids: ["a"],
  });
  assert.throws(
    () => d.register({ ...reg, node_id: "legacy_node_00001" }),
    /ExistingNodeProtected/,
  );
});
test("signature binds method path node and store; status cannot inspect foreign plans", async () => {
  const { d, reg } = fixture();
  d.register(reg);
  const path = "/v2/device/prepare",
    body = { store_id: reg.store_id };
  const proof = await controlProof(
    canonical({
      protocol: 2,
      method: "POST",
      path,
      node_id: reg.node_id,
      body,
    }),
    reg.token_hash,
  );
  const req = new Request("https://synthetic.invalid", {
    headers: {
      "X-Control-Node": reg.node_id,
      "X-Control-Node-Hash": reg.token_hash,
      "X-Local-Control-Proof": proof,
    },
  });
  const device = await d.authenticate(req, path, body);
  assert.equal(device.id, reg.node_id);
  await assert.rejects(
    d.authenticate(req, "/v2/device/start", body),
    /DeviceProofRequired/,
  );
  await assert.rejects(
    d.authenticate(req, path, { store_id: "another" }),
    /DeviceUnauthorized/,
  );
  assert.throws(() => d.status(device, "foreign"), /DeviceTaskNotOwned/);
});
test("one-message draft retries survive reconstruction, cancellation never frees the budget or changes shared cursor", async () => {
  const { k, d, reg } = fixture();
  d.register(reg);
  const device = {
    id: reg.node_id,
    scope: reg.scope,
    expires_at: reg.expires_at,
  };
  const body = {
    request_id: "request_test_0001",
    account_id: "a",
    target: "-1002123962275",
    text: "synthetic acceptance",
    expires_at: 1800001800,
  };
  const before = k.one("SELECT revision FROM snapshot_cursor WHERE id=1");
  const p = await d.prepare(device, body);
  assert.equal(d.status(device, p.plan_id).attempted, false);
  assert.equal(
    (await new DeviceControl(k).prepare(device, body)).existing,
    true,
  );
  await assert.rejects(
    d.prepare(device, { ...body, text: "changed" }),
    /DeviceRequestChanged/,
  );
  await assert.rejects(
    d.prepare(device, { ...body, request_id: "request_test_0002" }),
    /DeviceMessageBudgetUsed/,
  );
  assert.equal(d.cancel(device, p.plan_id).state, "cancelled");
  await assert.rejects(
    d.prepare(device, { ...body, request_id: "request_test_0003" }),
    /DeviceMessageBudgetUsed/,
  );
  assert.deepEqual(
    k.one("SELECT revision FROM snapshot_cursor WHERE id=1"),
    before,
  );
});
import { TelegramNativeGate } from "../telegram-native-gate.mjs";
test("legacy sync/node routes cannot overwrite device records; revoke preserves uncertain attempts", async () => {
  const { k, d, reg } = fixture();
  d.register(reg);
  const device = {
    id: reg.node_id,
    scope: reg.scope,
    expires_at: reg.expires_at,
  };
  const p = await d.prepare(device, {
    request_id: "request_protected_01",
    account_id: "a",
    target: reg.scope.targets[0],
    text: "synthetic",
    expires_at: 1800001800,
  });
  const owner = d.owned(device, p.plan_id);
  assert.throws(
    () =>
      k.registerNode({
        id: reg.node_id,
        token_hash: "b".repeat(64),
        pools: ["telegram.mtproto"],
        account_ids: ["a"],
      }),
    /DeviceNodeProtected/,
  );
  assert.throws(
    () => k.sync({ accounts: [], tasks: [{ id: owner.task_id }], jobs: [] }),
    /DeviceTaskProtected/,
  );
  assert.throws(
    () =>
      k.sync({
        accounts: [],
        tasks: [],
        jobs: [{ id: owner.job_id, task_id: owner.task_id }],
      }),
    /DeviceTaskProtected/,
  );
  assert.equal(
    k.deviceJobAllowed({ id: "legacy" }, { id: owner.job_id }),
    false,
  );
  k.sql.exec(
    "UPDATE jobs SET state='unknown',attempt_id='synthetic-attempt' WHERE id=?",
    owner.job_id,
  );
  d.revoke(reg.node_id);
  assert.equal(
    k.one("SELECT state FROM tasks WHERE id=?", owner.task_id).state,
    "cancelled",
  );
  assert.equal(
    k.one("SELECT state FROM jobs WHERE id=?", owner.job_id).state,
    "unknown",
  );
  assert.equal(
    k.deviceJobAllowed({ id: "cloudflare-telegram" }, { id: owner.job_id }),
    false,
  );
});
test("new device cannot take over legacy local authority or an explicitly local account", async () => {
  for (const explicit of [false, true]) {
    const { k, d, reg } = fixture();
    d.register(reg);
    k.configure({ global_stop: false, live_enabled: true });
    const device = {
      id: reg.node_id,
      scope: reg.scope,
      expires_at: reg.expires_at,
    };
    const p = await d.prepare(device, {
      request_id: "request_handoff_01",
      account_id: "a",
      target: reg.scope.targets[0],
      text: "synthetic",
      expires_at: 1800001800,
    });
    k.sql.exec("UPDATE workplans SET state='active' WHERE id=?", p.plan_id);
    if (explicit)
      k.accountChannel = () => ({ channel: "local", generation: "original" });
    else k.legacyLocalUntil = 1800003600;
    const gate = new TelegramNativeGate(new WorkflowQueue(k), {
      TELEGRAM_EXECUTOR_ACCOUNT: "a",
      TG_QUEUE_BRIDGE: "synthetic",
    });
    await assert.rejects(
      gate.activate({ plan_id: p.plan_id, source: "explicit_device_start" }),
      /DeviceRequiresExplicitChannelHandoff/,
    );
  }
});
test("revocation during prepare hash await cannot create a post-revoke job", async () => {
  const { k, d, reg } = fixture();
  d.register(reg);
  const device = {
    id: reg.node_id,
    scope: reg.scope,
    expires_at: reg.expires_at,
  };
  const pending = d.prepare(device, {
    request_id: "request_race_00001",
    account_id: "a",
    target: reg.scope.targets[0],
    text: "synthetic",
    expires_at: 1800001800,
  });
  d.revoke(reg.node_id);
  await assert.rejects(pending, /DeviceUnauthorized/);
  assert.equal(k.one("SELECT COUNT(*) n FROM device_tasks").n, 0);
  assert.equal(k.one("SELECT COUNT(*) n FROM jobs").n, 0);
});
