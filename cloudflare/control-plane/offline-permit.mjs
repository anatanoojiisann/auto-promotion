// A pre-issued, signed checkpoint lets the fenced local owner continue when the
// provider rejects even SELECT. It never extends beyond the next UTC reset.
import { accountBlockActive, ControlError, sha } from "./kernel.mjs";

export async function controlProof(value, secret) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return [...new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value)))]
    .map(n => n.toString(16).padStart(2, "0")).join("");
}

export async function issuePermit(checkpoint, secret) {
  const body = JSON.stringify(checkpoint);
  return { body, signature: await controlProof(body, secret) };
}

export async function offlineCheck(permit, token, input, env, now = Date.now() / 1000) {
  if (!permit || typeof permit.body !== "string" || permit.body.length > 2000000 ||
      permit.signature !== await controlProof(permit.body, env.OPERATOR_TOKEN))
    throw new ControlError("LocalChannelCheckpointRequired", 403);
  let p;
  try { p = JSON.parse(permit.body); }
  catch { throw new ControlError("LocalChannelCheckpointRequired", 403); }
  if (p.until <= now || p.until > (Math.floor(now / 86400) + 1) * 86400 ||
      p.node_id !== env.FALLBACK_NODE_ID || p.store_id !== env.FALLBACK_STORE_ID ||
      input.store_id !== p.store_id || await sha(token) !== p.node_token_hash)
    throw new ControlError("LocalChannelCheckpointExpired", 403);
  let account = null;
  if (input.job) {
    const j = input.job;
    account = p.accounts.find(a => a.id === j.account_id);
    if (!account || !p.pools.includes(j.pool) || p.closed_jobs.includes(j.id))
      throw new ControlError("LocalChannelJobNotEligible", 409);
    if (accountBlockActive(account.outbound_block_state, now))
      throw new ControlError("AccountOutboundBlocked", 409);
    if (account.disabled || account.cooldown_until > now || account.next_at > now)
      throw new ControlError("ExecutionStopped", 409);
    if (!j.manual && account.mode === "live" && !account.allowed_actions.includes(j.action))
      throw new ControlError("ActionNotEnabled", 403);
    if (account.identity_aliases !== 1) throw new ControlError("LocalChannelIdentityAliases", 409);
  }
  if (p.settings.global_stop || !p.settings.live_enabled)
    throw new ControlError("ExecutionStopped", 409);
  return { allowed: true, node_id: p.node_id, store_id: p.store_id, settings: p.settings,
    attempts: p.attempts, historical_attempts: p.historical_attempts, account,
    checkpoint_used: true };
}
