import assert from "node:assert/strict";
import test from "node:test";
import { sha } from "../kernel.mjs";
import { issuePermit, offlineCheck } from "../offline-permit.mjs";

test("signed offline checkpoints retain manual blocks and bounded anomaly expiry", async () => {
  const now = 1800000000;
  const env = { OPERATOR_TOKEN: "synthetic-operator", FALLBACK_NODE_ID: "n", FALLBACK_STORE_ID: "s" };
  const input = { store_id: "s", job: { id: "j", account_id: "a", pool: "x.edge", action: "reply" } };
  const account = {
    id: "a", mode: "live", allowed_actions: ["reply"], identity_aliases: 1,
    outbound_block_state: { state: "active", reason: "x_challenge", since: now, until: null, generation: 1 },
  };
  const p = { node_id: "n", store_id: "s", until: now + 900,
    node_token_hash: await sha("synthetic-node"), settings: { global_stop: false, live_enabled: true },
    accounts: [account], pools: ["x.edge"], closed_jobs: [], attempts: [], historical_attempts: [],
  };
  const permit = await issuePermit(p, env.OPERATOR_TOKEN);
  await assert.rejects(offlineCheck(permit, "synthetic-node", input, env, now), /AccountOutboundBlocked/);
  account.outbound_block_state = { state: "active", reason: "browser_page_anomaly", since: now, until: now + 60, generation: 2 };
  const bounded = await issuePermit(p, env.OPERATOR_TOKEN);
  await assert.rejects(offlineCheck(bounded, "synthetic-node", input, env, now), /AccountOutboundBlocked/);
  assert.equal((await offlineCheck(bounded, "synthetic-node", input, env, now + 61)).allowed, true);
});
