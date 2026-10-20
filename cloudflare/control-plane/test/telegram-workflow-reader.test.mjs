import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { ControlKernel } from "../kernel.mjs";
import { WorkflowQueue } from "../workflow-queue.mjs";
import { TelegramWorkflowReader } from "../telegram-workflow-reader.mjs";
async function fixture({ authorized = true, rules = "Stay on topic." } = {}) {
  const db = new DatabaseSync(":memory:");
  let now = 1800000000,
    calls = 0;
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
        assert.equal(r.account_id, "a");
        if (r.operation === "health")
          return Response.json(
            { identity_verified: authorized },
            { status: authorized ? 200 : 409 },
          );
        if (r.operation === "discover")
          return Response.json({ candidates: [] });
        return Response.json({
          identity_checked: true,
          about: rules,
          pinned: [],
          recent: [
            { id: 42, text: "synthetic", author_id: "public-author-id" },
          ],
          checked_at: new Date(now * 1000).toISOString(),
        });
      },
    },
  };
  return {
    k,
    q,
    id,
    reader: new TelegramWorkflowReader(q, env),
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
