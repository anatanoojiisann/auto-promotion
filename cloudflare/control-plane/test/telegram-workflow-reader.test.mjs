import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { ControlKernel } from "../kernel.mjs";
import { WorkflowQueue } from "../workflow-queue.mjs";
import { TelegramWorkflowReader } from "../telegram-workflow-reader.mjs";
async function fixture({ authorized = true, rules = "Stay on topic.", readResult = {} } = {}) {
  const db = new DatabaseSync(":memory:");
  let now = 1800000000,
    calls = 0;
  const requests = [];
  const storage = {
    sql: {
      exec(sql, ...args) {
        if (sql.includes("CREATE TABLE")) {
          db.exec(sql);
          return { toArray: () => [] };
        }
        const s = db.prepare(sql),
          r = s.columns().length ? s.all(...args) : (s.run(...args), []);
        return { toArray: () => r };
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
  k.configure({ global_stop: false });
  k.sync({
    accounts: [
      {
        id: "a",
        identity: "telegram:a",
        platform: "telegram",
        mode: "live",
        limits: { daily: 7, hourly: 3 },
        allowed_actions: ["join", "reply"],
      },
    ],
    tasks: [],
    jobs: [],
  });
  const q = new WorkflowQueue(k),
    id = "WF-AUTHENTICATED-READS";
  await q.create({
    source: "explicit_user_start",
    definition: {
      id,
      name: "synthetic",
      goal: 100,
      account_ids: ["a"],
      execution_channel: "cloud",
      local_fallback_allowed: false,
      native_task_ids: [],
      native_job_ids: [],
      requirements: {},
      steps: [
        {
          id: "discover",
          stage: "discover",
          depends_on: [],
          input: { query: "synthetic" },
        },
        {
          id: "first:inspect",
          stage: "inspect",
          depends_on: [],
          input: { target: "@first_group" },
        },
        {
          id: "first:research_review",
          stage: "research_review",
          depends_on: ["first:inspect"],
          input: { target: "@first_group" },
        },
      ],
    },
  });
  const env = {
    TELEGRAM_EXECUTOR_ACCOUNT: "a",
    TG_QUEUE_BRIDGE: "synthetic-only",
    TELEGRAM_EXECUTOR: {
      fetch: async (url, req) => {
        calls++;
        const r = JSON.parse(req.body);
        requests.push(r);
        assert.equal(r.account_id, "a");
        if (r.operation === "health")
          return Response.json(
            { identity_verified: authorized },
            { status: authorized ? 200 : 409 },
          );
        if (r.operation === "discover")
          return Response.json({ candidates: [], rpc_counts: { identity: 1, directory: 1 } });
        return Response.json({
          identity_checked: true,
          about: rules,
          pinned: [],
          recent: [
            { id: 42, text: "synthetic", author_id: "public-author-id" },
          ],
          checked_at: new Date(now * 1000).toISOString(),
          ...readResult,
        });
      },
    },
  };
  return {
    k,
    q,
    id,
    reader: new TelegramWorkflowReader(q, env),
    env,
    requests,
    calls: () => calls,
    advance: (n) => (now += n),
  };
}
test("authenticated reader requires verified cloud login before registration", async () => {
  const f = await fixture({ authorized: false });
  await assert.rejects(
    f.reader.start(f.id, "explicit_setup"),
    /ExecutorLoginNotVerified/,
  );
  assert.equal(f.k.one("SELECT COUNT(*) n FROM workflow_nodes").n, 0);
  assert.equal(f.k.one("SELECT COUNT(*) n FROM jobs").n, 0);
});
test("authenticated reads collect evidence and hash authors without promoting research or creating native writes", async () => {
  const f = await fixture();
  await f.reader.start(f.id, "explicit_setup");
  await f.reader.tick();
  f.advance(901);
  await f.reader.tick();
  const row = f.k.one(
    "SELECT state,result FROM worksteps WHERE id='first:inspect'",
  );
  assert.equal(row.state, "completed");
  const r = JSON.parse(row.result);
  assert.equal(r.facts.rules_read, true);
  assert.equal(r.evidence[0].observation.recent[0].author_id, undefined);
  assert.equal(r.evidence[0].observation.recent[0].author_hash.length, 64);
  assert.equal(
    f.k.one("SELECT state FROM worksteps WHERE id='first:research_review'")
      .state,
    "pending",
  );
  assert.equal(f.k.one("SELECT COUNT(*) n FROM jobs").n, 0);
});
test("external rules remain incomplete and scoped stop prevents further reads", async () => {
  const f = await fixture({ rules: "Rules: https://example.com/rules" });
  await f.reader.start(f.id, "explicit");
  await f.reader.tick();
  f.advance(901);
  await f.reader.tick();
  const row = f.k.one(
    "SELECT state,result FROM worksteps WHERE id='first:inspect'",
  );
  assert.equal(row.state, "pending");
  assert.equal(JSON.parse(row.result).facts.rules_read, false);
  const before = f.calls();
  f.reader.stop(f.id, "explicit_stop");
  f.advance(901);
  await f.reader.tick();
  assert.equal(f.calls(), before);
});

test("incomplete budget-limited scans cannot become complete research evidence or platform cooldown", async () => {
  const f = await fixture({ readResult: { read_state: "incomplete", read_complete: false,
    cooldown_source: "local_read_budget", retry_after_seconds: 10,
    rpc_counts: { identity: 1, history: 2 }, read_cursor_next: { recent: { committed: 0, pending_before: 50 } } } });
  await f.reader.start(f.id, "explicit");
  await f.reader.tick(); f.advance(901); await f.reader.tick();
  const row = f.k.one("SELECT state,result FROM worksteps WHERE id='first:inspect'");
  assert.equal(row.state, "pending");
  assert.equal(JSON.parse(row.result).verdict, "defer");
  assert.equal(f.k.one("SELECT cooldown_until FROM accounts WHERE id='a'").cooldown_until, 0);
  assert.equal(f.reader.snapshot(f.id).last_read_state, "incomplete");
});

test("per-round plan scan budget rotates fairly across persisted reader runtimes", async () => {
  const f = await fixture();
  f.env.TG_READ_POLICY_JSON = JSON.stringify({ groups_per_scan: 1 });
  const second = "WF-SECOND-READER";
  await f.q.create({ source: "explicit", definition: { ...f.q.plan(f.id).definition, id: second } });
  await f.reader.start(f.id, "explicit"); await f.reader.start(second, "explicit");
  await f.reader.tick();
  assert.equal(f.reader.snapshot(f.id).runs + f.reader.snapshot(second).runs, 1);
  f.advance(901); await f.reader.tick();
  assert.equal(f.reader.snapshot(f.id).runs, 1);
  assert.equal(f.reader.snapshot(second).runs, 1);
});

test("one tick shares its RPC budget across plans instead of granting each plan a fresh budget", async () => {
  const f = await fixture();
  f.env.TG_READ_POLICY_JSON = JSON.stringify({ groups_per_scan: 10, scan_rpc_limit: 4 });
  for (const id of ["WF-SECOND", "WF-THIRD"]) {
    await f.q.create({ source: "explicit", definition: { ...f.q.plan(f.id).definition, id } });
    await f.reader.start(id, "explicit");
  }
  await f.reader.start(f.id, "explicit");
  await f.reader.tick();
  const reads = f.requests.filter(r => r.operation === "discover");
  assert.equal(reads.length, 2);
  assert.deepEqual(reads.map(r => r.read_policy.scan_rpc_limit), [4, 2]);
});

test("transport or JSON failure stops the tick because consumed RPC count is unknown", async () => {
  for (const failure of ["transport", "json"]) {
    const f = await fixture();
    const second = "WF-SECOND-FAILURE";
    await f.q.create({ source: "explicit", definition: { ...f.q.plan(f.id).definition, id: second } });
    await f.reader.start(f.id, "explicit"); await f.reader.start(second, "explicit");
    let requests = 0;
    f.env.TELEGRAM_EXECUTOR.fetch = async () => {
      requests++;
      if (failure === "transport") throw new Error("SyntheticLostResponse");
      return new Response("{", { status: 200 });
    };
    await f.reader.tick();
    assert.equal(requests, 1);
    const row = f.k.one("SELECT rpc_counts,last_read_state FROM telegram_reader_runtime WHERE state='waiting_retry'");
    assert.equal(JSON.parse(row.rpc_counts), null);
    assert.equal(row.last_read_state, "incomplete");
  }
});
