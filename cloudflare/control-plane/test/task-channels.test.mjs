import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { ControlKernel } from "../kernel.mjs";

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
        const statement = db.prepare(sql);
        const rows = statement.columns().length
          ? statement.all(...args)
          : (statement.run(...args), []);
        return { toArray: () => rows };
      },
    },
    transactionSync(fn) {
      db.exec("BEGIN IMMEDIATE");
      try {
        const result = fn();
        db.exec("COMMIT");
        return result;
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    },
  };
  const k = new ControlKernel(storage, {
    clock: () => now,
    legacyLocalUntil: now + 9000,
  });
  k.configure({
    global_stop: false,
    live_enabled: true,
    max_workers: 10,
    pool_workers: { "x.edge": 10 },
  });
  k.registerNode({
    id: "node",
    token_hash: "a".repeat(64),
    pools: ["x.edge", "facebook.desktop"],
    account_ids: ["a", "b", "alias"],
  });
  const node = k.node("node", "a".repeat(64));
  function seed(
    id,
    account,
    channel = "inherit",
    identity = "x:" + account,
    executionMode = "automatic",
    platform = "x",
  ) {
    const a = {
      id: account,
      identity,
      platform,
      mode: "live",
      limits: { daily: 50, hourly: 20 },
      min_interval: 0,
      allowed_actions: ["reply"],
    };
    const t = {
      id: "t-" + id,
      state: "active",
      phase: "ready",
      version: "policy-" + id,
      account_ids: [account],
      policy: {
        execution_channel: channel,
        starts_at: new Date((now - 60) * 1000).toISOString(),
        ends_at: new Date((now + 3600) * 1000).toISOString(),
        min_interval: 0,
      },
    };
    const j = {
      id: "send:" + id,
      reference_id: id,
      kind: "send",
      task_id: t.id,
      account_id: account,
      pool: platform === "facebook" ? "facebook.desktop" : "x.edge",
      version: "payload-" + id,
      payload: { text: "synthetic", execution_mode: executionMode },
      action: "reply",
      state: "not_attempted",
      available_at: now,
      expires_at: now + 900,
    };
    k.sync({ accounts: [a], tasks: [t], jobs: [j] });
    return { a, t, j };
  }
  function handoff(
    s,
    channel,
    expected = null,
    generation = "1".repeat(32),
    until = now + 1800,
  ) {
    return k.setAccountChannel({
      account_id: s.a.id,
      task_id: s.t.id,
      node_id: "node",
      store_id: "synthetic-store",
      channel,
      expected_generation: expected,
      generation,
      until,
    });
  }
  return {
    k,
    storage,
    db,
    node,
    seed,
    handoff,
    advance: (n) => {
      now += n;
    },
  };
}

test("first Facebook local handoff needs no prior route and does not attempt a job", () => {
  const f = fixture();
  const s = f.seed(
    "facebook-first",
    "a",
    "local",
    "facebook:synthetic",
    "automatic",
    "facebook",
  );
  assert.equal(f.k.accountChannel("a"), null);
  const ack = f.handoff(s, "local");
  assert.equal(ack.channel.channel, "local");
  assert.equal(ack.channel.store_id, "synthetic-store");
  assert.equal(ack.channel.node_id, "node");
  assert.equal(f.k.claim(f.node, "facebook.desktop", s.j.id), null);
  assert.equal(
    f.db
      .prepare("SELECT count(*) n FROM jobs WHERE attempt_at IS NOT NULL")
      .get().n,
    0,
  );
  assert.deepEqual(f.handoff(s, "local"), ack);
  assert.throws(
    () => f.handoff(s, "local", null, "2".repeat(32)),
    /AccountChannelGenerationConflict/,
  );
});

test("one account can return online while other legacy accounts remain fenced", () => {
  const f = fixture(),
    online = f.seed("online", "a", "cloud"),
    old = f.seed("old", "b");
  assert.equal(f.k.claim(f.node, "x.edge", online.j.id), null);
  f.handoff(online, "cloud");
  assert.ok(f.k.claim(f.node, "x.edge", online.j.id));
  assert.equal(f.k.claim(f.node, "x.edge", old.j.id), null);
});

test("microsecond policy deadlines allow the same handoff boundary but no later millisecond", () => {
  const f = fixture(),
    s = f.seed("microseconds", "a", "cloud");
  s.t.policy.ends_at = new Date(1800003600579)
    .toISOString()
    .replace(".579Z", ".579152Z");
  f.k.sync({ accounts: [], tasks: [s.t], jobs: [] });
  const boundary = Date.parse(s.t.policy.ends_at) / 1000 + 0.000152;
  assert.equal(
    f.handoff(s, "cloud", null, "1".repeat(32), boundary).channel.until_at,
    boundary,
  );
  assert.throws(
    () =>
      f.handoff(s, "cloud", "1".repeat(32), "2".repeat(32), boundary + 0.001),
    /ChannelExceedsTaskWindow/,
  );
});

test("local task cannot be claimed online even without the legacy global fence", () => {
  const f = fixture(),
    local = f.seed("local", "a", "local");
  f.k.legacyLocalUntil = 0;
  assert.equal(f.k.claim(f.node, "x.edge", local.j.id), null);
  assert.throws(() => f.handoff(local, "cloud"), /TaskChannelMismatch/);
});

test("local handoff retains server stop and account cooldown", () => {
  const f = fixture(),
    local = f.seed("local", "a", "local");
  f.k.configure({ global_stop: true });
  assert.throws(() => f.handoff(local, "local"), /ExecutionStopped/);
  assert.equal(f.k.accountChannel("a"), null);
  f.k.configure({ global_stop: false });
  f.k.sync({
    accounts: [{ ...local.a, cooldown_until: 1800000060 }],
    tasks: [],
    jobs: [],
  });
  assert.throws(() => f.handoff(local, "local"), /ExecutionStopped/);
  f.advance(61);
  assert.equal(f.handoff(local, "local").channel.channel, "local");
});

test("manual local handoff does not require enabling live publication", () => {
  const f = fixture(),
    manual = f.seed("manual", "a", "local", "x:a", "manual");
  f.k.configure({ live_enabled: false });
  assert.equal(f.handoff(manual, "local").channel.channel, "local");
  const automatic = f.seed("automatic", "b", "local");
  assert.throws(() => f.handoff(automatic, "local"), /ExecutionStopped/);
});

test("handoff is idempotent, requires the current generation and preserves attempted jobs", () => {
  const f = fixture(),
    first = f.seed("first", "a", "cloud"),
    next = f.seed("next", "a", "local");
  f.handoff(first, "cloud");
  const changes = f.db.prepare("SELECT total_changes() n").get().n;
  f.handoff(first, "cloud");
  assert.equal(f.db.prepare("SELECT total_changes() n").get().n, changes);
  const lease = f.k.claim(f.node, "x.edge", first.j.id);
  assert.throws(
    () => f.handoff(next, "local", "1".repeat(32), "2".repeat(32)),
    /AccountChannelHandoffBusy/,
  );
  f.advance(181);
  f.k.sweep();
  assert.throws(
    () => f.handoff(next, "local", null, "2".repeat(32)),
    /AccountChannelGenerationConflict/,
  );
  f.handoff(next, "local", "1".repeat(32), "2".repeat(32));
  assert.equal(
    f.k.one("SELECT state FROM jobs WHERE id=?", first.j.id).state,
    "unknown",
  );
  assert.throws(
    () => f.k.authorize(f.node, first.j.id, lease.lease_token, "late"),
    /LeaseExpired/,
  );
  const reopened = new ControlKernel(f.storage, { clock: () => 1800000181 });
  assert.equal(reopened.accountChannel("a").channel, "local");
  assert.equal(reopened.claim(f.node, "x.edge", first.j.id), null);
});

test("same-generation renewal is monotone and bound to the original store and tuple", () => {
  const f = fixture(),
    s = f.seed("renew", "a", "local");
  const first = f.handoff(s, "local", null, "1".repeat(32), 1800000050);
  const renewed = f.k.setAccountChannel({
    account_id: "a",
    task_id: s.t.id,
    node_id: "node",
    store_id: "synthetic-store",
    channel: "local",
    generation: first.channel.generation,
    expected_generation: first.channel.generation,
    until: 1800000100,
  });
  assert.equal(renewed.channel.until_at, 1800000100);
  assert.equal(renewed.channel.store_id, "synthetic-store");
  assert.throws(
    () =>
      f.k.setAccountChannel({
        account_id: "a",
        task_id: s.t.id,
        node_id: "node",
        store_id: "wrong-store",
        channel: "local",
        generation: first.channel.generation,
        expected_generation: first.channel.generation,
        until: 1800000150,
      }),
    /AccountChannelGenerationChanged/,
  );
  assert.throws(
    () =>
      f.handoff(
        s,
        "local",
        first.channel.generation,
        first.channel.generation,
        1800000090,
      ),
    /AccountChannelGenerationChanged/,
  );
});

test("channel ownership spans identity aliases and local expiry does not reopen online claims", () => {
  const f = fixture(),
    local = f.seed("local", "a", "local", "same-user"),
    alias = f.seed("alias", "alias", "inherit", "same-user");
  f.handoff(local, "local", null, "1".repeat(32), 1800000050);
  assert.equal(f.k.claim(f.node, "x.edge", alias.j.id), null);
  f.advance(60);
  f.k.legacyLocalUntil = 0;
  assert.equal(f.k.claim(f.node, "x.edge", alias.j.id), null);
});
