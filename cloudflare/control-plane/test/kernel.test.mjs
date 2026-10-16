import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { ControlKernel, POOLS, sha } from "../kernel.mjs";

function fixture() {
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
  const k = new ControlKernel(storage, { clock: () => now });
  function seed({
    id = "one",
    pool = "x.edge",
    identity = "x:one",
    account = "a",
    resource = null,
    state = "not_attempted",
    payload = {},
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
      action: "reply",
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

test("new workspace is stopped and cannot publish by default", () => {
  const f = fixture();
  const empty = new ControlKernel({
    sql: f.storage.sql,
    transactionSync: f.storage.transactionSync,
  });
  assert.equal(empty.settings().live_enabled, true);
  const db = new DatabaseSync(":memory:");
  const storage = {
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
