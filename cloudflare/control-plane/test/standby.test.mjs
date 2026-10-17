import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { LEGACY_SCHEMA } from "../kernel.mjs";
import { standbyCheck } from "../standby.mjs";

function fixture() {
  const db = new DatabaseSync(":memory:");
  db.exec(LEGACY_SCHEMA);
  const now = 1800000000, fence = { node: "only-node", store: "only-db" };
  const config = { global_stop: false, live_enabled: true,
    max_workers: 6, global_limit: { daily: 100, hourly: 20 } };
  db.prepare("UPDATE config SET payload=?").run(JSON.stringify(config));
  db.prepare("INSERT INTO nodes VALUES(?,?,?,?,1,0)").run("only-node", "hash", '["x.edge"]', '["a"]');
  const account = { limits: { daily: 20, hourly: 4 }, min_interval: 480 };
  db.prepare("INSERT INTO accounts VALUES(?,?,?,?,0,0,0)")
    .run("a", "x:one", "x", JSON.stringify(account));
  db.prepare("INSERT INTO tasks VALUES('t','active','ready','policy','{}')").run();
  db.prepare("INSERT INTO jobs(id,kind,reference_id,task_id,account_id,pool,version,payload,state,available_at,expires_at) VALUES('j','send','i','t','a','x.edge','v','{}','pending',?,?)").run(now - 1, now + 900);
  const queries = [];
  const storage = { sql: { exec(q, ...args) {
    queries.push(q);
    assert.match(q, /^SELECT /, "standby must still work when ALL storage writes fail");
    return { toArray: () => db.prepare(q).all(...args) };
  } } };
  const input = { store_id: "only-db", job: { id: "j", account_id: "a", task_id: "t", pool: "x.edge", version: "v" } };
  const check = (options = {}) => standbyCheck(storage, options.node || "only-node",
    options.token || "hash", options.input || input, options.fence || fence, now);
  return { db, now, config, queries, input, check };
}

test("standby works with the legacy schema without migrations, heartbeats or writes", () => {
  const f = fixture();
  const result = f.check();
  assert.equal(result.allowed, true);
  assert.equal(result.node_id, "only-node");
  assert.deepEqual(result.attempts, []);
  assert.ok(f.queries.every(q => q.startsWith("SELECT ")));
});

test("standby requires matching deployment fence, registered node and token", () => {
  const f = fixture();
  assert.throws(() => f.check({ node: "different" }), /BindingMismatch/);
  assert.throws(() => f.check({ token: "bad" }), /Unauthorized/);
  assert.throws(() => f.check({ input: { ...f.input, store_id: "clone-db" } }), /BindingMismatch/);
  assert.throws(() => f.check({ fence: {} }), /BindingMismatch/);
  f.db.prepare("INSERT INTO nodes VALUES('other','hash','[]','[]',1,0)").run();
  assert.throws(() => f.check(), /RequiresSingleNode/);
});

test("standby retains server stop, live flag, account disable, cooldown and task pause", () => {
  for (const sql of [
    "UPDATE config SET payload=json_set(payload,'$.global_stop',json('true'))",
    "UPDATE config SET payload=json_set(payload,'$.live_enabled',json('false'))",
    "UPDATE accounts SET disabled=1",
    "UPDATE accounts SET cooldown_until=1800000100",
    "UPDATE accounts SET next_at=1800000100",
    "UPDATE tasks SET state='paused'",
  ]) {
    const f = fixture();
    f.db.exec(sql);
    assert.throws(() => f.check(), /ExecutionStopped/, sql);
  }
});

test("cloud leases and attempted/settled/mismatched jobs cannot be taken over", () => {
  const f = fixture();
  f.db.exec("UPDATE jobs SET state='leased',lease_until=1800000180");
  assert.throws(() => f.check(), /CloudLeaseActive/);
  f.db.exec("UPDATE jobs SET lease_until=0,attempt_id='uncertain'");
  assert.throws(() => f.check(), /JobNotEligible/);
  f.db.exec("UPDATE jobs SET attempt_id=NULL,state='confirmed'");
  assert.throws(() => f.check(), /JobNotEligible/);
  f.db.exec("UPDATE jobs SET state='pending',version='changed'");
  assert.throws(() => f.check(), /JobNotEligible/);
});

test("cloud-only attempts and quota history are returned for local ledger coverage", () => {
  const f = fixture();
  f.db.exec("UPDATE jobs SET attempt_id='remote-attempt',state='unknown'");
  f.db.prepare("INSERT INTO quota_events VALUES('historic','global','live',?)").run(f.now - 60);
  const result = f.check({ input: { store_id: "only-db" } });
  assert.equal(result.attempts[0].attempt_id, "remote-attempt");
  assert.deepEqual(result.historical_attempts, ["historic"]);
});
