import assert from "node:assert/strict";
import test from "node:test";
import { issuePermit, offlineCheck } from "../offline-permit.mjs";
import { sha } from "../kernel.mjs";

test("quota-only offline checkpoint is signed, scoped and expires before UTC reset", async () => {
  const now = 1800000000;
  const env = { OPERATOR_TOKEN: "test-key", FALLBACK_NODE_ID: "node", FALLBACK_STORE_ID: "db" };
  const data = { node_id: "node", store_id: "db", node_token_hash: await sha("node-token"),
    settings: { global_stop: false, live_enabled: true },
    accounts: [{ id: "a", mode: "live", allowed_actions: ["reply"], identity_aliases: 1,
      disabled: false, cooldown_until: 0, next_at: 0 }],
    pools: ["x.edge"], closed_jobs: ["closed"], attempts: [], historical_attempts: [],
    until: (Math.floor(now / 86400) + 1) * 86400 - 5 };
  const permit = await issuePermit(data, env.OPERATOR_TOKEN);
  const check = (p = permit, input = { store_id: "db" }, token = "node-token", at = now) =>
    offlineCheck(p, token, input, env, at);
  assert.equal((await check()).checkpoint_used, true);
  await assert.rejects(check({ ...permit, signature: "bad" }), /CheckpointRequired/);
  await assert.rejects(check(permit, { store_id: "clone" }), /CheckpointExpired/);
  await assert.rejects(check(permit, { store_id: "db" }, "wrong"), /CheckpointExpired/);
  await assert.rejects(check(permit, { store_id: "db" }, "node-token", data.until), /CheckpointExpired/);
  const job = { id: "new", account_id: "a", pool: "x.edge", action: "reply" };
  assert.equal((await check(permit, { store_id: "db", job })).allowed, true);
  await assert.rejects(check(permit, { store_id: "db", job: { ...job, id: "closed" } }), /JobNotEligible/);
  await assert.rejects(check(permit, { store_id: "db", job: { ...job, action: "like" } }), /ActionNotEnabled/);
  const stopped = await issuePermit({ ...data, settings: { ...data.settings, global_stop: true } }, env.OPERATOR_TOKEN);
  await assert.rejects(check(stopped), /ExecutionStopped/);
});
