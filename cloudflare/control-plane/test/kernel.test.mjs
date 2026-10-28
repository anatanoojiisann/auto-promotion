import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { ControlKernel, POOLS, LEGACY_SCHEMA, cloudFailure, sha } from "../kernel.mjs";
import { scheduleMaintenance } from "../maintenance.mjs";

function fixture({ beforeOpen } = {}) {
  const db = new DatabaseSync(":memory:");
  let now = 1800000000;
  const storage = {
    sql: {
      exec(sql, ...args) {
        if (sql.includes("CREATE TABLE")) {
          db.exec(sql);
          return { toArray: () => [] };
        }
        const stmt = db.prepare(sql);
        let rows = [];
        if (stmt.columns().length) rows = stmt.all(...args);
        else stmt.run(...args);
        return { toArray: () => rows };
      },
    },
    transactionSync(fn) {
      db.exec("BEGIN IMMEDIATE");
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
  beforeOpen?.(db, storage);
  const k = new ControlKernel(storage, { clock: () => now });
  function seed({
    id = "one",
    pool = "x.edge",
    identity = "x:one",
    account = "a",
    resource = null,
    state = "not_attempted",
    payload = {},
    action = "reply",
    policy = {},
    ownedTarget = "",
  } = {}) {
    const platform = pool.startsWith("simulation.")
      ? pool.split(".")[1]
      : pool.split(".")[0];
    const a = {
      id: account,
      identity,
      platform,
      mode: "live",
      limits: { daily: 50, hourly: 20 },
      min_interval: 0,
      allowed_actions: ["reply", "join"],
      owned_target_url: ownedTarget,
    };
    const t = {
      id: "t-" + id,
      state: "active",
      phase: "ready",
      version: "policy-v1",
      account_ids: [account],
      policy: {
        starts_at: new Date((now - 60) * 1000).toISOString(),
        ends_at: new Date((now + 3600) * 1000).toISOString(),
        min_interval: 0,
        daily_limit: 50,
        ...policy,
      },
    };
    const j = {
      id: "send:" + id,
      kind: "send",
      reference_id: id,
      task_id: t.id,
      account_id: account,
      pool,
      resource,
      payload,
      version: "payload-v1",
      action,
      available_at: now,
      expires_at: now + 900,
      state,
    };
    const snapshot = { accounts: [a], tasks: [t], jobs: [j] };
    k.sync(snapshot);
    return { a, t, j, snapshot };
  }
  k.configure({
    global_stop: false,
    live_enabled: true,
    max_workers: 10,
    global_limit: { daily: 100, hourly: 100 },
    pool_workers: { "x.edge": 10 },
    platform_workers: { x: 10 },
  });
  const hash = "a".repeat(64);
  function node(id = "node", accounts = ["a", "b"], pools = [...POOLS]) {
    k.registerNode({ id, token_hash: hash, pools, account_ids: accounts });
    return k.node(id, hash);
  }
  return {
    k,
    db,
    storage,
    seed,
    node,
    advance: (n) => {
      now += n;
    },
  };
}

test("unchanged snapshots do not rewrite account or task rows", () => {
  const f = fixture();
  const s = f.seed();
  f.k.sync({ ...s.snapshot, revision: 1 });
  const before = f.db.prepare("SELECT total_changes() n").get().n;
  f.k.sync({ ...s.snapshot, revision: 1 });
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n, before);
  new ControlKernel(f.storage);
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n, before);
});

test("TikTok Tabbit jobs use the same persistent claim and once-only fence", () => {
  const f = fixture();
  const s = f.seed({pool:"tiktok.tabbit", identity:"tiktok:bound", resource:"desktop:tabbit:bound"});
  const node=f.node("desktop",["a"],["tiktok.tabbit"]);
  const lease=f.k.claim(node,"tiktok.tabbit",s.j.id);
  assert.ok(lease.lease_token);
  f.k.complete(node,s.j.id,lease.lease_token,{state:"unknown",result:{submission_started:true}});
  assert.equal(f.k.claim(node,"tiktok.tabbit",s.j.id),null);
});

test("frozen conflict exposes field names and batch index while preserving unknown attempts", () => {
  const f = fixture(), s = f.seed();
  f.db.prepare("UPDATE jobs SET state='unknown',attempt_id='previous-attempt' WHERE id=?").run(s.j.id);
  s.j.version = "SENSITIVE_FROZEN_VALUE";
  s.j.pool = "x.playwright";
  let conflict;
  try { f.k.sync(s.snapshot); } catch (error) { conflict = error; }
  assert.equal(conflict.message, "FrozenJobChanged");
  assert.deepEqual(conflict.diagnostics, { conflict_index: 0, changed_fields: ["version", "pool"] });
  assert.equal(JSON.stringify(conflict.diagnostics).includes("SENSITIVE_FROZEN_VALUE"), false);
  assert.equal(f.k.one("SELECT state,attempt_id FROM jobs WHERE id=?", s.j.id).state, "unknown");
  assert.equal(f.k.one("SELECT state,attempt_id FROM jobs WHERE id=?", s.j.id).attempt_id, "previous-attempt");
});

test("account manual block fences claim, restart and leased final authorization", () => {
  const f = fixture();
  const s = f.seed();
  const node = f.node();
  const lease = f.k.claim(node, "x.edge", s.j.id);
  s.a.outbound_block_state = {
    state: "active", reason: "x_challenge", since: 1800000000,
    until: null, generation: 1,
  };
  f.k.sync(s.snapshot);
  assert.throws(() => f.k.authorize(node, s.j.id, lease.lease_token, "attempt"), /AccountOutboundBlocked/);
  f.k.complete(node, s.j.id, lease.lease_token, { state: "pending" });
  const restarted = new ControlKernel(f.storage, { clock: () => 1800000001 });
  assert.equal(restarted.claim(node, "x.edge", s.j.id), null);
});

test("receipt journal guard failure remains a durable manual account block", () => {
  const f = fixture();
  const s = f.seed();
  const node = f.node();
  const lease = f.k.claim(node, "x.edge", s.j.id);
  f.k.complete(node, s.j.id, lease.lease_token, { state: "unknown", result: {
    account_block_reason: "account_guard_unavailable", account_block_since: 1800000000,
    account_block_generation: 1,
  } });
  assert.equal(f.k.accountOutboundBlocked("x:one"), true);
});

test("snapshot omission and stale clear cannot remove an account block", () => {
  const f = fixture();
  const s = f.seed();
  const node = f.node();
  s.a.outbound_block_state = { state: "active", reason: "x_challenge", since: 1800000000, until: null, generation: 2 };
  f.k.sync(s.snapshot);
  delete s.a.outbound_block_state;
  f.k.sync(s.snapshot);
  assert.equal(f.k.claim(node, "x.edge", s.j.id), null);
  s.a.outbound_block_state = { state: "cleared", reason: "x_challenge", since: 1800000000, until: null, generation: 2, health_ok: true, health_checked_at: 1800000000 };
  f.k.sync(s.snapshot);
  assert.equal(f.k.claim(node, "x.edge", s.j.id), null);
  s.a.outbound_block_state.generation = 3;
  f.k.sync(s.snapshot);
  assert.ok(f.k.claim(node, "x.edge", s.j.id));
});

test("receipt block is independent of send outcome and old receipts cannot undo recovery", () => {
  const f = fixture();
  const s = f.seed();
  const node = f.node();
  const lease = f.k.claim(node, "x.edge", s.j.id);
  f.k.authorize(node, s.j.id, lease.lease_token, "attempt");
  f.k.complete(node, s.j.id, lease.lease_token, { state: "unknown", attempt_id: "attempt", result: {
    account_block_reason: "x_challenge", account_block_since: 1800000000,
    account_block_generation: 1,
  } });
  assert.equal(f.k.one("SELECT state FROM jobs WHERE id=?", s.j.id).state, "unknown");
  assert.equal(JSON.parse(f.k.one("SELECT payload FROM accounts WHERE id='a'").payload).outbound_block_state?.reason, "x_challenge");
  s.a.outbound_block_state = { state: "cleared", reason: "x_challenge", since: 1800000000, until: null, generation: 2, health_ok: true, health_checked_at: 1800000001 };
  f.k.sync(s.snapshot);
  f.k.complete(node, s.j.id, lease.lease_token, { state: "unknown", attempt_id: "attempt", result: { account_block_reason: "x_challenge", account_block_since: 1800000000, account_block_generation: 1 } });
  const account = JSON.parse(f.k.one("SELECT payload FROM accounts WHERE id='a'").payload);
  assert.equal(account.outbound_block_state.state, "cleared");
  assert.equal(account.outbound_block_state.generation, 2);
  assert.equal(f.k.one("SELECT state FROM jobs WHERE id=?", s.j.id).state, "unknown");
});

test("bounded page anomaly expires without labeling a challenge", () => {
  const f = fixture();
  const s = f.seed();
  const node = f.node();
  s.a.outbound_block_state = { state: "active", reason: "browser_page_anomaly", since: 1800000000, until: 1800000060, generation: 1 };
  f.k.sync(s.snapshot);
  assert.equal(f.k.claim(node, "x.edge", s.j.id), null);
  f.advance(61);
  assert.ok(f.k.claim(node, "x.edge", s.j.id));
});

test("two distinct Telegram speaking rejections pause account without permanent disable", () => {
  const f = fixture();
  const node = f.node();
  function reject(id, discussion) {
    const s = f.seed({ id, pool: "telegram.mtproto", identity: "telegram:one" });
    const lease = f.k.claim(node, "telegram.mtproto", s.j.id);
    assert.ok(lease);
    f.k.authorize(node, s.j.id, lease.lease_token, "attempt-" + id);
    f.k.complete(node, s.j.id, lease.lease_token, { state: "failed_known", attempt_id: "attempt-" + id, result: {
      reason: "TelegramSpeakingRestricted", account_restriction: "telegram_speaking_restricted", discussion_id: discussion,
    } });
  }
  reject("one-group", "123");
  reject("same-group", "123");
  assert.equal(f.k.accountOutboundBlocked("telegram:one"), false);
  reject("other-group", "456");
  assert.equal(f.k.accountOutboundBlocked("telegram:one"), true);
  assert.equal(f.k.one("SELECT disabled FROM accounts WHERE id='a'").disabled, 0);
  const next = f.seed({ id: "later-group", pool: "telegram.mtproto", identity: "telegram:one" });
  assert.equal(f.k.claim(node, "telegram.mtproto", next.j.id), null);
});

test("successful Telegram sends break the sequence of speaking rejections", () => {
  const f = fixture(), node = f.node();
  for (const [id, outcome] of [["before", "failed_known"], ["success", "confirmed"], ["after", "failed_known"]]) {
    const s = f.seed({ id, pool: "telegram.mtproto", identity: "telegram:one" });
    const lease = f.k.claim(node, "telegram.mtproto", s.j.id);
    f.k.authorize(node, s.j.id, lease.lease_token, "attempt-" + id);
    f.k.complete(node, s.j.id, lease.lease_token, { state: outcome, attempt_id: "attempt-" + id,
      result: outcome === "confirmed" ? { verified: true, external_reference: "telegram:synthetic:1" } : {
        account_restriction: "telegram_speaking_restricted", discussion_id: id === "before" ? "123" : "456",
      } });
    f.advance(1);
  }
  assert.equal(f.k.accountOutboundBlocked("telegram:one"), false);
});

test("legacy unfrozen placeholders can freeze without changing attempted jobs", () => {
  const f = fixture();
  const s = f.seed();
  s.t.phase = "starting";
  f.k.sync(s.snapshot);
  s.t.phase = "ready";
  s.t.manifest_hash = "first-frozen-manifest";
  s.j.version = "resolved-membership-time";
  f.k.sync(s.snapshot);
  assert.equal(f.k.one("SELECT version FROM jobs WHERE id=?", s.j.id).version, s.j.version);
  s.j.version = "later-change";
  assert.throws(() => f.k.sync(s.snapshot), /FrozenJobChanged/);
});

test("node heartbeat persists at most once in five minutes, including restart", () => {
  const f = fixture();
  f.node();
  const before = f.db.prepare("SELECT total_changes() n").get().n;
  f.k.node("node", "a".repeat(64));
  f.advance(299);
  f.k.node("node", "a".repeat(64));
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n, before);
  new ControlKernel(f.storage, {clock: () => 1800000299}).node("node", "a".repeat(64));
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n, before);
  f.advance(1);
  f.k.node("node", "a".repeat(64));
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n, before + 1);
});

test("freezing a legacy placeholder never resets an unknown attempt", () => {
  const f = fixture();
  const s = f.seed();
  s.t.phase = "starting";
  f.k.sync(s.snapshot);
  f.db.prepare("UPDATE jobs SET state='unknown',attempt_id='existing-attempt' WHERE id=?").run(s.j.id);
  s.t.phase = "ready";
  s.t.manifest_hash = "first-manifest";
  s.j.version = "resolved-time";
  assert.throws(() => f.k.sync(s.snapshot), /FrozenJobChanged/);
  const job = f.k.one("SELECT state,attempt_id FROM jobs WHERE id=?", s.j.id);
  assert.equal(job.state, "unknown");
  assert.equal(job.attempt_id, "existing-attempt");
});

test("quota errors are safe and retry after UTC day reset", () => {
  const result = cloudFailure(new Error("Exceeded allowed rows written in Durable Objects free tier."), 3600000);
  assert.equal(result.error, "CloudWriteQuotaExceeded");
  assert.equal(result.retry_after_seconds, 23 * 3600 + 30);
  assert.deepEqual(cloudFailure(new Error("private provider exception")), {error: "CloudRuntimeUnavailable", retry_after_seconds: 60});
});

test("new workspace is stopped and cannot publish by default", () => {
  const f = fixture();
  const empty = new ControlKernel({
    sql: f.storage.sql,
    transactionSync: f.storage.transactionSync,
  });
  assert.equal(empty.settings().live_enabled, true);
  const db = new DatabaseSync(":memory:");
  const storage = {
    transactionSync: fn => fn(),
    sql: {
      exec(sql) {
        db.exec(sql);
        return { toArray: () => [] };
      },
    },
  };
  new ControlKernel(storage);
  assert.equal(
    JSON.parse(db.prepare("SELECT payload FROM config").get().payload)
      .global_stop,
    true,
  );
  db.close();
  f.db.close();
});
test("all supported platform/connection pools can be routed", () => {
  for (const pool of POOLS) {
    const f = fixture();
    f.seed({ pool });
    const n = f.node();
    assert.equal(f.k.claim(n, pool).pool, pool);
    f.db.close();
  }
});
test("state persists across coordinator reconstruction", () => {
  const f = fixture();
  f.seed();
  f.k.claim(f.node(), "x.edge");
  const restarted = new ControlKernel(f.storage);
  assert.equal(restarted.status().jobs[0].state, "leased");
  f.db.close();
});
test("same account is serialized while other accounts run independently", () => {
  const f = fixture();
  f.seed();
  f.seed({ id: "two" });
  f.seed({ id: "three", account: "b", identity: "x:b" });
  const n = f.node();
  const first = f.k.claim(n, "x.edge"),
    second = f.k.claim(n, "x.edge");
  assert.notEqual(first.account_id, second.account_id);
  assert.equal(f.k.claim(n, "x.edge"), null);
  f.db.close();
});
test("shared desktop profile is exclusive on the same node", () => {
  const f = fixture();
  f.seed({ resource: "desktop:shared" });
  f.seed({
    id: "two",
    account: "b",
    identity: "x:b",
    resource: "desktop:shared",
  });
  const n = f.node();
  assert.ok(f.k.claim(n, "x.edge"));
  assert.equal(f.k.claim(n, "x.edge"), null);
  assert.ok(f.k.claim(f.node("node2"), "x.edge"));
  f.db.close();
});
test("attempt registration is idempotent and cannot change attempt id", () => {
  const f = fixture();
  f.seed();
  const n = f.node(),
    j = f.k.claim(n, "x.edge");
  f.k.authorize(n, j.id, j.lease_token, "attempt1");
  f.k.authorize(n, j.id, j.lease_token, "attempt1");
  assert.equal(f.k.rows("SELECT * FROM quota_events").length, 4);
  assert.throws(
    () => f.k.authorize(n, j.id, j.lease_token, "attempt2"),
    /AttemptAlreadyRecorded/,
  );
  f.db.close();
});
test("expired lease becomes unknown and a stale snapshot cannot replay it", () => {
  const f = fixture(),
    s = f.seed(),
    n = f.node(),
    j = f.k.claim(n, "x.edge");
  f.advance(181);
  f.k.sweep();
  f.k.sync(s.snapshot);
  assert.equal(f.k.claim(n, "x.edge"), null);
  assert.equal(f.k.one("SELECT state FROM jobs").state, "unknown");
  assert.throws(
    () => f.k.authorize(n, j.id, j.lease_token, "a"),
    /LeaseExpired/,
  );
  f.db.close();
});
test("late verified receipt settles unknown without resubmitting", () => {
  const f = fixture();
  f.seed();
  const n = f.node(),
    j = f.k.claim(n, "x.edge");
  f.k.authorize(n, j.id, j.lease_token, "a");
  f.advance(181);
  f.k.sweep();
  f.k.complete(n, j.id, j.lease_token, {
    state: "confirmed",
    attempt_id: "a",
    result: {
      verified: true,
      external_reference: "https://x.com/example/status/1",
    },
  });
  assert.equal(f.k.one("SELECT state FROM jobs").state, "confirmed");
  f.db.close();
});
test("success requires both authorization and verified platform receipt", () => {
  const f = fixture();
  f.seed();
  const n = f.node(),
    j = f.k.claim(n, "x.edge");
  assert.throws(
    () =>
      f.k.complete(n, j.id, j.lease_token, {
        state: "confirmed",
        result: { verified: true, external_reference: "x" },
      }),
    /AttemptNotAuthorized/,
  );
  f.k.authorize(n, j.id, j.lease_token, "a");
  assert.throws(
    () =>
      f.k.complete(n, j.id, j.lease_token, {
        state: "confirmed",
        attempt_id: "a",
        result: { verified: true },
      }),
    /VerifiedReceiptRequired/,
  );
  f.db.close();
});
test("scheduled status requires all returned ids to be independently found", () => {
  const f = fixture();
  f.seed();
  const n = f.node(),
    j = f.k.claim(n, "x.edge");
  f.k.authorize(n, j.id, j.lease_token, "a");
  assert.throws(
    () =>
      f.k.complete(n, j.id, j.lease_token, {
        state: "scheduled",
        attempt_id: "a",
        result: {
          verified: true,
          external_reference: "x",
          scheduled: true,
          active: true,
          message_ids: [1, 2],
          lookup_message_ids: [1],
        },
      }),
    /VerifiedScheduleRequired/,
  );
  f.db.close();
});
test("global quota is shared by independent nodes", () => {
  const f = fixture();
  f.k.configure({ global_limit: { daily: 10, hourly: 1 } });
  f.seed();
  f.seed({ id: "two", account: "b", identity: "x:b" });
  const n1 = f.node("n1"),
    n2 = f.node("n2");
  const a = f.k.claim(n1, "x.edge"),
    b = f.k.claim(n2, "x.edge");
  f.k.authorize(n1, a.id, a.lease_token, "a");
  assert.throws(
    () => f.k.authorize(n2, b.id, b.lease_token, "b"),
    /QuotaExceeded/,
  );
  f.db.close();
});
test("global stop revokes a leased job before submission", () => {
  const f = fixture();
  f.seed();
  const n = f.node(),
    j = f.k.claim(n, "x.edge");
  f.k.configure({ global_stop: true });
  assert.throws(
    () => f.k.authorize(n, j.id, j.lease_token, "a"),
    /ExecutionStopped/,
  );
  f.db.close();
});
test("node cannot claim another account or route", () => {
  const f = fixture();
  f.seed();
  const n = f.node("restricted", ["b"], ["telegram.mtproto"]);
  assert.throws(() => f.k.claim(n, "x.edge"), /PoolNotAssigned/);
  assert.equal(f.k.claim(f.node("b-only", ["b"]), "x.edge"), null);
  f.db.close();
});
test("node with a different secret or a different lease cannot report outcomes", () => {
  const f = fixture();
  f.seed();
  assert.throws(() => f.k.node("missing", "wrong"), /NodeUnauthorized/);
  const n = f.node(),
    j = f.k.claim(n, "x.edge");
  assert.throws(
    () =>
      f.k.complete(f.node("other"), j.id, j.lease_token, { state: "unknown" }),
    /InvalidLease/,
  );
  f.db.close();
});
test("frozen manifest references cannot be changed by a subsequent sync", () => {
  const f = fixture(),
    s = f.seed();
  s.snapshot.jobs[0].version = "changed";
  assert.throws(() => f.k.sync(s.snapshot), /FrozenJobChanged/);
  assert.equal(f.k.one("SELECT version FROM jobs").version, "payload-v1");
  f.db.close();
});
test("snapshots reject credentials and never downgrade pending review to a join retry", () => {
  const f = fixture(),
    s = f.seed({ state: "pending_admin_approval" });
  assert.equal(f.k.claim(f.node(), "x.edge"), null);
  s.snapshot.accounts[0].access_token = "secret";
  assert.throws(() => f.k.sync(s.snapshot), /CredentialInSnapshot/);
  f.db.close();
});
test("platform permissions remain enforced at final authorization", () => {
  const f = fixture(),
    s = f.seed();
  s.a.allowed_actions = [];
  f.k.sync(s.snapshot);
  const n = f.node(),
    j = f.k.claim(n, "x.edge");
  assert.throws(
    () => f.k.authorize(n, j.id, j.lease_token, "a"),
    /ActionNotEnabled/,
  );
  f.db.close();
});
test("owned X originals require frozen task opt-in and the exact owned profile", () => {
  for (const [enabled, target, allowed] of [
    [true, "https://x.com/brand", true],
    [false, "https://x.com/brand", false],
    [true, "https://x.com/other", false],
  ]) {
    const f = fixture();
    const s = f.seed({
      action: "post",
      ownedTarget: "https://x.com/brand",
      policy: { allow_owned_x_posts: enabled, execution_channel: "cloud" },
      payload: { target_owned: true, target },
    });
    const node = f.node();
    f.k.setAccountChannel({
      account_id: s.a.id,
      task_id: s.t.id,
      node_id: node.id,
      channel: "cloud",
      expected_generation: null,
      generation: "1".repeat(32),
      until: 1800000900,
    });
    const job = f.k.claim(node, "x.edge");
    if (allowed)
      assert.equal(
        f.k.authorize(node, job.id, job.lease_token, "post-one").allowed,
        true,
      );
    else
      assert.throws(
        () => f.k.authorize(node, job.id, job.lease_token, "post-one"),
        /ActionNotEnabled/,
      );
    assert.deepEqual(
      JSON.parse(f.k.one("SELECT payload FROM accounts WHERE id='a'").payload)
        .allowed_actions,
      ["reply", "join"],
    );
    f.db.close();
  }
});
test("quota ledger import does not count a recorded attempt twice", () => {
  const f = fixture(),
    s = f.seed();
  const n = f.node(),
    j = f.k.claim(n, "x.edge");
  f.k.authorize(n, j.id, j.lease_token, "a");
  s.snapshot.quota_events = [
    { attempt_id: "a", scope: "cloud_global", key: "live", at: 1800000000 },
  ];
  f.k.sync(s.snapshot);
  assert.equal(
    f.k.one("SELECT COUNT(*) n FROM quota_events WHERE scope='cloud_global'").n,
    1,
  );
  f.db.close();
});
test("live execution flags cannot be enabled by snapshot import", () => {
  const f = fixture();
  f.k.configure({ global_stop: true, live_enabled: false });
  f.seed();
  assert.equal(f.k.settings().global_stop, true);
  assert.equal(f.k.settings().live_enabled, false);
  f.db.close();
});
test("secrets use a one-way digest", async () => {
  assert.match(await sha("a-secret"), /^[a-f0-9]{64}$/);
});

test("requesting one job never leases a different pending job", () => {
  const f = fixture();
  f.seed();
  const s = f.seed({ id: "two" });
  const j = f.k.claim(f.node(), "x.edge", s.j.id);
  assert.equal(j.id, s.j.id);
  f.db.close();
});
test("an already imported current attempt is not charged twice at authorization", () => {
  const f = fixture(),
    s = f.seed();
  f.k.configure({ global_limit: { daily: 1, hourly: 1 } });
  s.snapshot.quota_events = [
    { attempt_id: "same", scope: "cloud_global", key: "live", at: 1800000000 },
  ];
  f.k.sync(s.snapshot);
  const n = f.node(),
    j = f.k.claim(n, "x.edge");
  assert.equal(f.k.authorize(n, j.id, j.lease_token, "same").allowed, true);
  f.db.close();
});
test("a manual handoff remains usable without enabling live publication", () => {
  const f = fixture();
  f.seed({ pool: "x.manual", payload: { execution_mode: "manual" } });
  f.k.configure({ live_enabled: false });
  const n = f.node(),
    j = f.k.claim(n, "x.manual");
  assert.ok(j);
  assert.throws(
    () => f.k.authorize(n, j.id, j.lease_token, "a"),
    /ManualActionCannotPublish/,
  );
  f.db.close();
});
test("task pacing is enforced across different accounts and nodes", () => {
  const f = fixture(),
    one = f.seed(),
    two = f.seed({ id: "two", account: "b", identity: "x:b" });
  one.t.policy.min_interval = 120;
  two.j.task_id = one.t.id;
  two.j.id = "send:third";
  two.j.reference_id = "third";
  f.k.sync({ accounts: [two.a], tasks: [one.t], jobs: [two.j] });
  const n = f.node(),
    a = f.k.claim(n, "x.edge", "send:one"),
    b = f.k.claim(f.node("other"), "x.edge", "send:third");
  f.k.authorize(n, a.id, a.lease_token, "a");
  assert.throws(
    () => f.k.authorize(f.node("other"), b.id, b.lease_token, "b"),
    /PacingWait/,
  );
  f.db.close();
});
test("frozen manifest stays frozen while paused", () => {
  const f = fixture(),
    s = f.seed();
  s.t.manifest_hash = "first";
  f.k.sync(s.snapshot);
  s.t.state = "paused";
  f.k.sync(s.snapshot);
  s.t.manifest_hash = "second";
  assert.throws(() => f.k.sync(s.snapshot), /FrozenTaskChanged/);
  f.db.close();
});
test("a delayed snapshot cannot undo a newer pause", () => {
  const f = fixture(),
    s = f.seed();
  s.t.state = "paused";
  f.k.sync({ ...s.snapshot, revision: 2 });
  s.t.state = "active";
  assert.equal(f.k.sync({ ...s.snapshot, revision: 1 }).stale, true);
  assert.equal(
    f.k.one("SELECT state FROM tasks WHERE id=?", s.t.id).state,
    "paused",
  );
  f.db.close();
});

function finish(f, n, j, attempt = "a", state = "confirmed", extra = {}) {
  return f.k.complete(n, j.id, j.lease_token, {
    state, attempt_id: attempt,
    result: {verified:true,external_reference:"https://example.invalid/receipt",...extra},
  });
}

test("due time and deadline remain exact across reconstruction", () => {
  const f = fixture(), s = f.seed(), n = f.node();
  f.db.prepare("UPDATE jobs SET available_at=?,expires_at=? WHERE id=?").run(1800000060,1800000090,s.j.id);
  const before = f.db.prepare("SELECT total_changes() n").get().n;
  assert.equal(f.k.claim(n,"x.edge"),null);
  f.advance(59.999);
  assert.equal(f.k.claim(n,"x.edge"),null);
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n,before);
  f.advance(0.001);
  f.k = new ControlKernel(f.storage,{clock:()=>1800000060});
  const j=f.k.claim(n,"x.edge");
  assert.ok(j);
  f.k = new ControlKernel(f.storage,{clock:()=>1800000090});
  assert.throws(()=>f.k.authorize(n,j.id,j.lease_token,"a"),/ExecutionWindowElapsed/);
  assert.equal(f.k.one("SELECT attempt_id FROM jobs").attempt_id,null);
  f.db.close();
});

test("never renew early or revive an expired lease", () => {
  const f=fixture(); f.seed(); const n=f.node(),j=f.k.claim(n,"x.edge");
  const before=f.db.prepare("SELECT total_changes() n").get().n;
  for(let i=0;i<11;i++){f.advance(10);assert.equal(f.k.renew(n,j.id,j.lease_token).lease_until,1800000180);}
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n,before);
  f.advance(10);
  const renewed=f.k.renew(n,j.id,j.lease_token);
  assert.equal(renewed.lease_until,1800000300);
  assert.equal(renewed.renew_after_seconds,120);
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n,before+1);
  f.advance(180);
  assert.throws(()=>f.k.renew(n,j.id,j.lease_token),/LeaseExpired/);
  f.k.sweep();
  assert.equal(f.k.one("SELECT state FROM jobs").state,"unknown");
  assert.equal(f.k.claim(n,"x.edge"),null);
  f.db.close();
});

test("pacing follows account identity across aliases and survives restart", () => {
  const f=fixture(),s=f.seed();
  s.a.min_interval=120;s.t.policy.pacing_scope="account";f.k.sync(s.snapshot);
  f.seed({id:"two",account:"b",identity:"x:one"});
  const n=f.node(),j=f.k.claim(n,"x.edge",s.j.id);
  f.k.authorize(n,j.id,j.lease_token,"a"); finish(f,n,j);
  assert.equal(f.k.one("SELECT next_at FROM accounts WHERE id='a'").next_at,0);
  f.k=new ControlKernel(f.storage,{clock:()=>1800000119});
  assert.equal(f.k.claim(n,"x.edge","send:two"),null);
  f.k=new ControlKernel(f.storage,{clock:()=>1800000120});
  assert.ok(f.k.claim(n,"x.edge","send:two"));
  f.db.close();
});

test("authorization response loss and duplicate uploads never create a second attempt", () => {
  const f=fixture(),s=f.seed(),n=f.node(),j=f.k.claim(n,"x.edge");
  f.k.authorize(n,j.id,j.lease_token,"lost-response");
  f.k=new ControlKernel(f.storage,{clock:()=>1800000001});
  const before=f.db.prepare("SELECT total_changes() n").get().n;
  assert.equal(f.k.authorize(n,j.id,j.lease_token,"lost-response").allowed,true);
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n,before);
  assert.throws(()=>f.k.authorize(n,j.id,j.lease_token,"new-attempt"),/AttemptAlreadyRecorded/);
  finish(f,n,j,"lost-response");
  const settled=f.db.prepare("SELECT total_changes() n").get().n;
  finish(f,n,j,"lost-response");f.k.sync(s.snapshot);
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n,settled);
  assert.equal(f.k.claim(n,"x.edge"),null);
  f.db.close();
});

test("duplicate native schedule receipt is read-only and remains unclaimable", () => {
  const f=fixture();f.seed();const n=f.node(),j=f.k.claim(n,"x.edge");
  f.k.authorize(n,j.id,j.lease_token,"a");
  const evidence={scheduled:true,active:true,message_ids:[1,2],lookup_message_ids:[1,2]};
  finish(f,n,j,"a","scheduled",evidence);
  const before=f.db.prepare("SELECT total_changes() n").get().n;
  finish(f,n,j,"a","scheduled",evidence);
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n,before);
  assert.equal(f.k.claim(n,"x.edge"),null);
  f.db.close();
});

test("all quota dimensions are enforced and echo imports add no duplicate rows", () => {
  const f=fixture(),s=f.seed(),t=f.seed({id:"two",account:"b",identity:"x:b"});
  const scopes=["target","user","campaign","pool"].map(scope=>({scope,key:"shared-"+scope,limit:{daily:1,hourly:1}}));
  // Set before admission; version/content remains frozen throughout execution.
  for(const v of [s,t])f.db.prepare("UPDATE jobs SET payload=? WHERE id=?").run(JSON.stringify({...v.j,scopes}),v.j.id);
  const n=f.node(),a=f.k.claim(n,"x.edge",s.j.id),b=f.k.claim(n,"x.edge",t.j.id);
  f.k.authorize(n,a.id,a.lease_token,"a");
  const events=f.k.rows("SELECT * FROM quota_events").map(e=>({attempt_id:e.attempt_id,scope:e.scope,key:e.scope_key,at:e.at}));
  const before=f.db.prepare("SELECT total_changes() n").get().n;
  f.k.sync({accounts:[],tasks:[],jobs:[],quota_events:events});
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n,before);
  assert.equal(f.k.one("SELECT COUNT(*) n FROM quota_history").n,0);
  assert.throws(()=>f.k.authorize(n,b.id,b.lease_token,"b"),/QuotaExceeded/);
  assert.equal(f.k.one("SELECT attempt_id FROM jobs WHERE id=?",b.id).attempt_id,null);
  f.db.close();
});

test("rolling hour and UTC-day limits include legacy imports without double counting", () => {
  const f=fixture(),s=f.seed();
  f.k.configure({global_limit:{daily:2,hourly:1}});
  const day=Math.floor(1800000000/86400)*86400;
  f.k.sync({accounts:[],tasks:[],jobs:[],quota_events:[
    {attempt_id:"historical",scope:"cloud_global",key:"live",at:1800000000-3599},
  ]});
  const n=f.node(),j=f.k.claim(n,"x.edge",s.j.id);
  assert.throws(()=>f.k.authorize(n,j.id,j.lease_token,"a"),/QuotaExceeded/);
  f.advance(1);f.k.authorize(n,j.id,j.lease_token,"a");finish(f,n,j);
  const limits=[{scope:"cloud_global",key:"live",limit:{daily:2,hourly:100}}];
  assert.throws(()=>f.k.checkQuotas(limits,"next",1800000001),/QuotaExceeded/);
  assert.doesNotThrow(()=>f.k.checkQuotas(limits,"next",day+86400));
  f.db.close();
});

test("a reused attempt id cannot bypass shared quotas for a different job", () => {
  const f=fixture();f.seed();f.seed({id:"two",account:"b",identity:"x:b"});
  const n=f.node(),a=f.k.claim(n,"x.edge"),b=f.k.claim(n,"x.edge");
  f.k.authorize(n,a.id,a.lease_token,"same");
  assert.throws(()=>f.k.authorize(n,b.id,b.lease_token,"same"),/AttemptAlreadyRecorded/);
  f.db.close();
});

test("maintenance arms only an actual expiry, advances for earlier work and stops when idle", async () => {
  const f=fixture();let alarm=null;const writes=[];
  const alarms={getAlarm:async()=>alarm,setAlarm:async at=>{alarm=at;writes.push(at);}};
  await scheduleMaintenance(f.k,alarms,1800000000000);assert.equal(writes.length,0);
  const s=f.seed();
  await scheduleMaintenance(f.k,alarms,1800000000000);assert.equal(alarm,1800000900000);
  for(let i=0;i<10;i++)await scheduleMaintenance(f.k,alarms,1800000000000+i*10000);
  assert.equal(writes.length,1);
  const n=f.node(),j=f.k.claim(n,"x.edge",s.j.id);
  await scheduleMaintenance(f.k,alarms,1800000000000);assert.equal(alarm,1800000180000);
  f.advance(120);f.k.renew(n,j.id,j.lease_token);
  await scheduleMaintenance(f.k,alarms,1800000120000);assert.equal(writes.length,2);
  f.advance(60);alarm=null;f.k.sweep();await scheduleMaintenance(f.k,alarms,1800000180000);
  assert.equal(alarm,1800000300000);
  f.advance(120);alarm=null;f.k.sweep();await scheduleMaintenance(f.k,alarms,1800000300000);
  assert.equal(alarm,null);
  assert.equal(f.k.one("SELECT state FROM jobs").state,"unknown");
  f.k.sweep();await scheduleMaintenance(f.k,alarms,1800000300000);assert.equal(writes.length,3);
  f.db.close();
});

test("v1 migration retains leases, attempts, quotas, immutable payloads and an archive", () => {
  const source=fixture(),s=source.seed();s.t.manifest_hash="frozen";source.k.sync(s.snapshot);
  const n=source.node(),j=source.k.claim(n,"x.edge");source.k.authorize(n,j.id,j.lease_token,"prior");
  source.db.prepare("UPDATE accounts SET next_at=?").run(1800000120);
  source.k.configure({global_stop:true});
  const original=source.k.one("SELECT * FROM jobs");
  const f=fixture({beforeOpen(db,storage){
    db.exec(LEGACY_SCHEMA);
    for(const table of ["config","accounts","tasks","jobs","task_pacing","snapshot_cursor","nodes","quota_events","audit"]){
      const cols=db.prepare("PRAGMA table_info("+table+")").all().map(c=>c.name);
      const rows=source.db.prepare("SELECT "+cols.join(",")+" FROM "+table).all();
      for(const row of rows)db.prepare("INSERT OR REPLACE INTO "+table+"("+cols.join(",")+") VALUES("+cols.map(()=>"?").join(",")+")").run(...cols.map(c=>row[c]));
    }
    const upgraded=new ControlKernel(storage,{clock:()=>1800000000});
    assert.equal(upgraded.settings().global_stop,true);
    assert.equal(upgraded.accountNext("x:one"),1800000120);
    assert.equal(upgraded.one("SELECT COUNT(*) n FROM quota_events").n,4);
    assert.equal(upgraded.one("SELECT COUNT(*) n FROM jobs_v1_archive").n,1);
    const preserved=upgraded.one("SELECT * FROM jobs");
    for(const key of ["payload","state","owner","lease_token","lease_until","attempt_id","version"])
      assert.equal(preserved[key],original[key]);
    assert.equal(upgraded.one("SELECT payload FROM tasks").payload,source.k.one("SELECT payload FROM tasks").payload);
    const before=db.prepare("SELECT total_changes() n").get().n;
    new ControlKernel(storage);assert.equal(db.prepare("SELECT total_changes() n").get().n,before);
  }});
  assert.equal(f.k.claim(n,"x.edge"),null);
  f.advance(180);f.k.sweep();assert.equal(f.k.one("SELECT state FROM jobs").state,"unknown");
  f.k.sync(s.snapshot);assert.equal(f.k.claim(n,"x.edge"),null);
  finish(f,n,j,"prior");assert.equal(f.k.one("SELECT state FROM jobs").state,"confirmed");
  source.db.close();f.db.close();
});

test("migration interruption rolls back schema and records, then can resume", () => {
  let checked=false;
  const f=fixture({beforeOpen(db,storage){
    db.exec(LEGACY_SCHEMA);
    db.prepare("INSERT INTO quota_events VALUES(?,?,?,?)").run("kept","cloud_global","live",1800000000);
    const exec=storage.sql.exec;
    storage.sql.exec=(sql,...args)=>{const r=exec(sql,...args);if(sql.includes("ALTER TABLE jobs"))throw new Error("quota exhausted during migration");return r;};
    assert.throws(()=>new ControlKernel(storage),/quota exhausted/);
    assert.equal(db.prepare("SELECT COUNT(*) n FROM quota_events").get().n,1);
    assert.equal(db.prepare("SELECT name FROM sqlite_master WHERE name='jobs_v1_archive'").get(),undefined);
    assert.equal(db.prepare("PRAGMA table_info(jobs)").all().some(c=>c.name==="attempt_at"),false);
    storage.sql.exec=exec;checked=true;
  }});
  assert.equal(checked,true);assert.equal(f.k.one("SELECT COUNT(*) n FROM quota_events").n,1);f.db.close();
});
