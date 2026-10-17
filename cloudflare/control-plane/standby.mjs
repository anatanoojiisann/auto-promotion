// Read-only even when SQLite's write budget is exhausted. The deployment fence
// makes this a single-node, single-database workspace in BOTH execution channels.
import { ControlError } from "./kernel.mjs";

export function standbyCheck(storage, nodeId, tokenHash, input, fence, now = Date.now() / 1000) {
  const rows = (q, ...args) => {
    try { return storage.sql.exec(q, ...args).toArray(); }
    catch (error) {
      // Only a fixed query stage is logged, never credentials or row contents.
      console.error("StandbyReadFailed", q.split("FROM ")[1]?.split(" ")[0] || "query");
      throw error;
    }
  };
  const one = (q, ...args) => rows(q, ...args)[0];
  if (!fence.node || !fence.store || nodeId !== fence.node || input.store_id !== fence.store)
    throw new ControlError("LocalChannelBindingMismatch", 403);
  const node = one("SELECT * FROM nodes WHERE id=?", nodeId);
  if (!node || !node.enabled || node.token_hash !== tokenHash)
    throw new ControlError("Unauthorized", 401);
  if (one("SELECT id FROM nodes WHERE enabled=1 AND id<>? LIMIT 1", nodeId))
    throw new ControlError("LocalChannelRequiresSingleNode", 409);
  const settings = JSON.parse(one("SELECT payload FROM config WHERE id=1").payload);
  if (settings.global_stop || !settings.live_enabled)
    throw new ControlError("ExecutionStopped", 409);
  if (one("SELECT id FROM jobs WHERE state='leased' AND lease_until>? LIMIT 1", now))
    throw new ControlError("LocalChannelCloudLeaseActive", 409);
  const attempts = rows("SELECT kind,reference_id,account_id,attempt_id FROM jobs WHERE attempt_id IS NOT NULL LIMIT 10001");
  if (attempts.length > 10000) throw new ControlError("LocalChannelLedgerTooLarge", 409);
  const history = rows("SELECT DISTINCT attempt_id FROM quota_events WHERE at>? LIMIT 10001", now - 86400);
  if (history.length > 10000) throw new ControlError("LocalChannelLedgerTooLarge", 409);
  let account = null;
  if (input.job) {
    const j = input.job;
    if (!JSON.parse(node.accounts).includes(j.account_id) || !JSON.parse(node.pools).includes(j.pool))
      throw new ControlError("JobNotAssigned", 403);
    const a = one("SELECT * FROM accounts WHERE id=?", j.account_id);
    if (!a) throw new ControlError("LocalChannelAccountNotMirrored", 409);
    account = JSON.parse(a.payload);
    if (!j.manual && account.mode === "live" && !(account.allowed_actions || []).includes(j.action))
      throw new ControlError("ActionNotEnabled", 403);
    // The local ledger uses account IDs; do not drop a cloud identity-alias cap.
    if (rows("SELECT id FROM accounts WHERE identity=?", a.identity).length !== 1)
      throw new ControlError("LocalChannelIdentityAliases", 409);
    if (a.disabled || a.cooldown_until > now || a.next_at > now)
      throw new ControlError("ExecutionStopped", 409);
    const remote = one("SELECT * FROM jobs WHERE id=?", j.id);
    if (remote && (remote.version !== j.version || remote.account_id !== j.account_id ||
      remote.task_id !== j.task_id || remote.pool !== j.pool || remote.attempt_id ||
      !["pending", "leased"].includes(remote.state)))
      throw new ControlError("LocalChannelJobNotEligible", 409);
    const task = one("SELECT state FROM tasks WHERE id=?", j.task_id);
    if (task && task.state !== "active") throw new ControlError("ExecutionStopped", 409);
  }
  const result = { allowed: true, node_id: nodeId, store_id: fence.store, settings, account, attempts,
    historical_attempts: history.map(a => a.attempt_id) };
  if (!input.job) {
    const accounts = rows("SELECT a.*, (SELECT count(*) FROM accounts b WHERE b.identity=a.identity) identity_aliases FROM accounts a")
      .filter(a => JSON.parse(node.accounts).includes(a.id))
      .map(a => ({ ...JSON.parse(a.payload), disabled: !!a.disabled, next_at: a.next_at,
        cooldown_until: a.cooldown_until, identity_aliases: a.identity_aliases }));
    result.checkpoint = { ...result, accounts, pools: JSON.parse(node.pools),
      node_token_hash: node.token_hash, issued_at: now,
      until: (Math.floor(now / 86400) + 1) * 86400 - 5,
      closed_jobs: rows("SELECT id FROM jobs WHERE state NOT IN ('pending','leased') OR attempt_id IS NOT NULL").map(j => j.id) };
  }
  return result;
}
