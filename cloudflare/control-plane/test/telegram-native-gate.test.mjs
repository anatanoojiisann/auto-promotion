import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { ControlKernel } from "../kernel.mjs";
import { WorkflowQueue } from "../workflow-queue.mjs";
import {
  TelegramNativeGate,
  signNativeGate,
  verifyNativeGate,
} from "../telegram-native-gate.mjs";
async function fixture() {
  const db = new DatabaseSync(":memory:"),
    now = 1800000000;
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
  k.configure({ global_stop: false, live_enabled: true });
  const task = {
    id: "native-task",
    name: "synthetic",
    state: "active",
    phase: "discovered",
    version: "policy-v1",
    account_ids: ["a"],
    policy: {
      execution_channel: "cloud",
      starts_at: new Date((now - 60) * 1000).toISOString(),
      ends_at: new Date((now + 3600) * 1000).toISOString(),
      min_interval: 1200,
      daily_limit: 7,
    },
  };
  const job = {
    id: "join:synthetic",
    kind: "join",
    reference_id: "synthetic",
    task_id: task.id,
    account_id: "a",
    pool: "telegram.mtproto",
    version: "frozen-v1",
    payload: { target: "@synthetic", action: "join" },
    action: "join",
    state: "not_attempted",
    available_at: now - 1,
    expires_at: now + 1800,
  };
  k.sync({
    accounts: [
      {
        id: "a",
        identity: "telegram:a",
        platform: "telegram",
        mode: "live",
        limits: { daily: 7, hourly: 3 },
        min_interval: 1200,
        allowed_actions: ["join", "reply"],
      },
    ],
    tasks: [task],
    jobs: [job],
  });
  const q = new WorkflowQueue(k),
    id = "WF-NATIVE-SYNTHETIC";
  await q.create({
    source: "explicit_user_start",
    definition: {
      id,
      name: "synthetic",
      goal: 100,
      account_ids: ["a"],
      execution_channel: "cloud",
      local_fallback_allowed: false,
      native_task_ids: [task.id],
      native_job_ids: [],
      requirements: {},
      steps: [
        {
          id: "synthetic:research_review",
          stage: "research_review",
          depends_on: [],
          input: { target: "@synthetic" },
        },
        {
          id: "synthetic:join",
          stage: "join",
          depends_on: ["synthetic:research_review"],
          input: { target: "@synthetic" },
        },
      ],
    },
  });
  let sends = 0;
  let gate;
  const env = {
    TELEGRAM_EXECUTOR_ACCOUNT: "a",
    TG_QUEUE_BRIDGE: "synthetic-link-only",
    TELEGRAM_EXECUTOR: {
      fetch: async (url, r) => {
        const req = JSON.parse(r.body);
        if (req.operation === "health")
          return Response.json({ identity_verified: true, status: "healthy" });
        const permit = await gate.authorize(req);
        assert.equal(permit.job.payload.target, "@synthetic");
        sends++;
        return Response.json({
          status: "joined",
          verified: true,
          discussion_id: "123",
          external_reference: "telegram:membership:123",
        });
      },
    },
  };
  gate = new TelegramNativeGate(q, env);
  return { k, q, gate, env, id, job, db, sends: () => sends };
}
test("signed gate accepts exact bytes only and never accepts an absent key", async () => {
  const s = await signNativeGate("synthetic", '{"job_id":"one"}');
  assert.equal(
    await verifyNativeGate("synthetic", '{"job_id":"one"}', s),
    true,
  );
  assert.equal(
    await verifyNativeGate("synthetic", '{"job_id":"two"}', s),
    false,
  );
  assert.equal(await verifyNativeGate(null, "{}", s), false);
});
test("native job cannot link until research and automation rules pass", async () => {
  const f = await fixture();
  const b = {
    plan_id: f.id,
    step_id: "synthetic:join",
    job_id: f.job.id,
    source: "explicit_user_start",
  };
  assert.throws(() => f.gate.link(b), /WorkflowQualificationIncomplete/);
  f.k.sql.exec(
    "UPDATE worksteps SET state='completed',result='{}' WHERE id='synthetic:research_review'",
  );
  assert.throws(() => f.gate.link(b), /AutomatedJoinNotVerified/);
  assert.equal(
    f.k.one("SELECT attempt_id FROM jobs WHERE id=?", f.job.id).attempt_id,
    null,
  );
});
test("verified activation and qualified link use the existing native quota and receipt ledger once", async () => {
  const f = await fixture();
  f.k.sql.exec(
    "UPDATE worksteps SET state='completed',result=? WHERE id='synthetic:research_review'",
    JSON.stringify({ facts: { automation_allowed: true } }),
  );
  await f.gate.activate({
    plan_id: f.id,
    source: "explicit_user_executor_setup",
  });
  f.gate.link({
    plan_id: f.id,
    step_id: "synthetic:join",
    job_id: f.job.id,
    source: "explicit_user_start",
  });
  const r = await f.gate.dispatch({ plan_id: f.id, step_id: "synthetic:join" });
  assert.equal(r.result.status, "joined");
  assert.equal(f.sends(), 1);
  const j = f.k.one(
    "SELECT state,attempt_id,quota_scopes FROM jobs WHERE id=?",
    f.job.id,
  );
  assert.equal(j.state, "joined");
  assert.ok(j.attempt_id);
  assert.ok(JSON.parse(j.quota_scopes).length);
  assert.equal(
    f.k.one("SELECT state FROM worksteps WHERE id='synthetic:join'").state,
    "completed",
  );
  assert.equal(
    (await f.gate.dispatch({ plan_id: f.id, step_id: "synthetic:join" }))
      .submitted,
    false,
  );
  assert.equal(f.sends(), 1);
});
test("unknown and existing attempts cannot be linked again; stop prevents activation", async () => {
  const f = await fixture();
  f.k.sql.exec(
    "UPDATE worksteps SET state='completed',result=? WHERE id='synthetic:research_review'",
    JSON.stringify({ facts: { automation_allowed: true } }),
  );
  f.k.sql.exec(
    "UPDATE jobs SET state='unknown',attempt_id='previous' WHERE id=?",
    f.job.id,
  );
  assert.throws(
    () =>
      f.gate.link({
        plan_id: f.id,
        step_id: "synthetic:join",
        job_id: f.job.id,
        source: "explicit",
      }),
    /NativeAttemptCannotReplay/,
  );
  f.k.configure({ global_stop: true });
  await assert.rejects(
    f.gate.activate({ plan_id: f.id, source: "explicit" }),
    /ExecutionStopped/,
  );
  assert.equal(f.sends(), 0);
});

test("a different job id cannot rejoin a target that was already attempted", async () => {
  const f = await fixture();
  f.k.sql.exec(
    "UPDATE worksteps SET state='completed',result=? WHERE id='synthetic:research_review'",
    JSON.stringify({ facts: { automation_allowed: true } }),
  );
  f.k.sync({
    accounts: [],
    tasks: [],
    jobs: [
      { ...f.job, id: "join:older", reference_id: "older", state: "unknown" },
    ],
  });
  f.k.sql.exec(
    "UPDATE jobs SET attempt_id='old-attempt' WHERE id='join:older'",
  );
  assert.throws(
    () =>
      f.gate.link({
        plan_id: f.id,
        step_id: "synthetic:join",
        job_id: f.job.id,
        source: "explicit",
      }),
    /TelegramTargetAlreadyAttempted/,
  );
});

test("other pending campaigns on the same account prevent an ownership handoff", async () => {
  const f = await fixture();
  const original = JSON.parse(
    f.k.one("SELECT payload FROM tasks WHERE id=?", "native-task").payload,
  );
  f.k.sync({
    accounts: [],
    tasks: [{ ...original, id: "other-task" }],
    jobs: [
      {
        ...f.job,
        id: "join:other",
        reference_id: "other",
        task_id: "other-task",
        payload: { target: "@other_group", action: "join" },
      },
    ],
  });
  await assert.rejects(
    f.gate.activate({ plan_id: f.id, source: "explicit_setup" }),
    /ExecutorAccountHasOtherPendingTasks/,
  );
  assert.equal(f.k.accountChannel("a"), null);
  assert.equal(f.sends(), 0);
});
