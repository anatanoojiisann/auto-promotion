import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { ControlKernel, sha } from "../kernel.mjs";
import { WorkflowQueue } from "../workflow-queue.mjs";
import { TelegramWorkflowReader } from "../telegram-workflow-reader.mjs";
import { ExecutorCore } from "../../telegram-executor/executor-core.mjs";
import {
  TelegramNativeGate,
  signNativeGate,
  verifyNativeGate,
} from "../telegram-native-gate.mjs";
async function fixture({ send = false } = {}) {
  const db = new DatabaseSync(":memory:");
  let now = 1800000000;
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
    phase: send ? "ready" : "discovered",
    ...(send ? { manifest_hash: "synthetic-frozen-manifest" } : {}),
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
    id: send ? "send:synthetic" : "join:synthetic",
    kind: send ? "send" : "join",
    reference_id: "synthetic",
    task_id: task.id,
    account_id: "a",
    pool: "telegram.mtproto",
    version: "frozen-v1",
    payload: {
      target: "@synthetic",
      action: send ? "reply" : "join",
      ...(send
        ? { text: "synthetic helpful reply", reply_to: 7, media: [] }
        : {}),
    },
    action: send ? "reply" : "join",
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
      steps: send
        ? [
            {
              id: "synthetic:membership_verify",
              stage: "membership_verify",
              depends_on: [],
              input: { target: "@synthetic" },
            },
            {
              id: "synthetic:reply_review",
              stage: "reply_review",
              depends_on: [],
              input: { target: "@synthetic" },
            },
            {
              id: "synthetic:send",
              stage: "send",
              depends_on: [
                "synthetic:reply_review",
                "synthetic:membership_verify",
              ],
              input: { target: "@synthetic" },
            },
            {
              id: "synthetic:receipt_verify",
              stage: "receipt_verify",
              depends_on: ["synthetic:send"],
              input: { target: "@synthetic" },
            },
          ]
        : [
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
  return {
    k,
    q,
    gate,
    env,
    id,
    job,
    db,
    sends: () => sends,
    advance: (n) => (now += n),
    now: () => now,
  };
}

async function sendFixture() {
  const f = await fixture({ send: true });
  const review = {
    facts: Object.fromEntries(
      [
        "automation_allowed",
        "question_unresolved",
        "context_read",
        "language_matches",
        "nonduplicate",
        "claims_verified",
      ].map((k) => [k, true]),
    ),
    question: {
      text: "synthetic question",
      message_id: 7,
      discussion_id: "123",
      published_at: new Date(f.now() * 1000).toISOString(),
    },
    reviewed_at: new Date(f.now() * 1000).toISOString(),
    text: f.job.payload.text,
    evidence: [{ checked_at: new Date(f.now() * 1000).toISOString() }],
  };
  f.k.sql.exec(
    "UPDATE worksteps SET state='completed',result=? WHERE id='synthetic:membership_verify'",
    JSON.stringify({ verified: true, discussion_id: "123" }),
  );
  f.k.sql.exec(
    "UPDATE worksteps SET state='completed',result=? WHERE id='synthetic:reply_review'",
    JSON.stringify(review),
  );
  await f.gate.activate({ plan_id: f.id, source: "synthetic_explicit_setup" });
  f.gate.link({
    plan_id: f.id,
    step_id: "synthetic:send",
    job_id: f.job.id,
    source: "synthetic_explicit_start",
  });
  return f;
}

test("accepted send followed by failed read can be reconciled without replaying send", async () => {
  const f = await sendFixture();
  let sends = 0,
    reads = 0,
    originalRequest;
  const rows = new Map();
  const core = new ExecutorCore({
    store: {
      get: async (k) => rows.get(k),
      put: async (k, v) => rows.set(k, structuredClone(v)),
    },
    clock: f.now,
    authorize: (req) => f.gate.authorize(req),
    submit: async (job, checkpoint, authorize) => {
      await authorize();
      await checkpoint({ submission_started: true });
      sends++;
      await checkpoint({
        message_ids: [42],
        external_reference: "telegram:@synthetic:42",
      });
      throw new Error("synthetic receipt lookup disconnected");
    },
  });
  f.env.TELEGRAM_EXECUTOR.fetch = async (url, request) => {
    const input = JSON.parse(request.body);
    if (input.operation === "health")
      return Response.json({ identity_verified: true });
    if (input.operation === "execute") {
      originalRequest = input;
      const result = await core.execute(input);
      // Simulate a lost/late reply crossing the original sender lease boundary.
      f.advance(181);
      f.k.sweep();
      return Response.json(result);
    }
    assert.equal(input.operation, "verify");
    assert.deepEqual(input.message_ids, [42]);
    assert.equal(input.target, "@synthetic");
    assert.equal(input.text, f.job.payload.text);
    reads++;
    return Response.json({
      status: "confirmed",
      verified: true,
      discussion_id: "123",
      message_ids: [42],
      external_reference: "telegram:@synthetic:42",
    });
  };
  await f.gate.dispatch({ plan_id: f.id, step_id: "synthetic:send" });
  const before = f.k.one("SELECT * FROM jobs WHERE id=?", f.job.id);
  assert.equal(before.state, "unknown");
  const reader = new TelegramWorkflowReader(f.q, f.env);
  await reader.start(f.id, "synthetic_read_reconciliation");
  f.k.sql.exec("UPDATE telegram_reader_runtime SET runs=3,next_at=0");
  await reader.tick();
  assert.equal(
    f.k.one("SELECT state FROM worksteps WHERE id='synthetic:receipt_verify'")
      .state,
    "completed",
  );
  assert.equal(
    f.k.one("SELECT state FROM worksteps WHERE id='synthetic:send'").state,
    "completed",
  );
  const after = f.k.one("SELECT * FROM jobs WHERE id=?", f.job.id);
  assert.equal(after.state, "confirmed");
  assert.equal(after.attempt_id, before.attempt_id);
  assert.equal(after.quota_scopes, before.quota_scopes);
  assert.equal(
    (await f.gate.dispatch({ plan_id: f.id, step_id: "synthetic:send" }))
      .submitted,
    false,
  );
  assert.equal(sends, 1);
  assert.equal(reads, 1);
  await assert.rejects(f.gate.authorize(originalRequest), /LeaseExpired/);
  const node = f.k.node(
    "cloudflare-telegram",
    await sha(f.env.TG_QUEUE_BRIDGE),
  );
  f.k.complete(node, f.job.id, originalRequest.lease_token, {
    state: "unknown",
    result: {},
    attempt_id: before.attempt_id,
  });
  assert.equal(
    f.k.one("SELECT state FROM jobs WHERE id=?", f.job.id).state,
    "confirmed",
  );
});

test("uncertain or contradictory executor refusals never requeue a possible attempt", async () => {
  for (const mode of [
    "missing-proof",
    "wrong-account",
    "after-authorize",
    "http-500",
    "non-json",
  ]) {
    const f = await sendFixture();
    f.env.TELEGRAM_EXECUTOR.fetch = async (url, request) => {
      const r = JSON.parse(request.body);
      if (mode === "after-authorize") await f.gate.authorize(r);
      if (mode === "non-json")
        return new Response("synthetic upstream unavailable", { status: 503 });
      const proof = {
        status: "waiting_cooldown",
        phase: "pre_execution",
        native_authorized: false,
        submission_started: false,
        job_id: r.job_id,
        account_id: r.account_id,
        job_version: r.version,
        attempt_id: r.attempt_id,
        retry_after_seconds: 120,
      };
      if (mode === "missing-proof") delete proof.phase;
      if (mode === "wrong-account")
        proof.account_id = "different-synthetic-account";
      return Response.json(proof, { status: mode === "http-500" ? 500 : 409 });
    };
    await f.gate.dispatch({ plan_id: f.id, step_id: "synthetic:send" });
    assert.equal(
      f.k.one("SELECT state FROM jobs WHERE id=?", f.job.id).state,
      "unknown",
      mode,
    );
    assert.equal(
      (await f.gate.dispatch({ plan_id: f.id, step_id: "synthetic:send" }))
        .submitted,
      false,
      mode,
    );
  }
});

test("unknown receipt reads require exact durable identity, target and ID evidence and no active sender lease", async () => {
  for (const mode of [
    "missing-id",
    "wrong-account",
    "wrong-version",
    "wrong-attempt",
    "wrong-target",
    "active-lease",
  ]) {
    const f = await sendFixture();
    const receipt = {
      status: "unknown",
      verified: false,
      account_id: "a",
      job_version: f.job.version,
      attempt_id: "synthetic-attempt",
      message_ids: [42],
      external_reference: "telegram:@synthetic:42",
    };
    if (mode === "missing-id") receipt.message_ids = [];
    if (mode === "wrong-account") receipt.account_id = "other";
    if (mode === "wrong-version") receipt.job_version = "other";
    if (mode === "wrong-attempt") receipt.attempt_id = "other";
    if (mode === "wrong-target")
      receipt.external_reference = "telegram:@other:42";
    f.k.sql.exec(
      "UPDATE jobs SET state=?,attempt_id='synthetic-attempt',result=?,lease_until=? WHERE id=?",
      mode === "active-lease" ? "leased" : "unknown",
      JSON.stringify(receipt),
      f.now() + 180,
      f.job.id,
    );
    f.k.sql.exec(
      "UPDATE worksteps SET state='unknown',result=? WHERE id='synthetic:send'",
      JSON.stringify(receipt),
    );
    const step = JSON.parse(
      f.k.one(
        "SELECT definition FROM worksteps WHERE id='synthetic:receipt_verify'",
      ).definition,
    );
    assert.equal(f.q.receiptSource(f.id, step), null, mode);
    assert.equal(
      f.q.claim({ id: "synthetic-reader", stages: ["receipt_verify"] }, f.id)
        .step,
      null,
      mode,
    );
  }
});

test("bounded native scans make progress past more blocked heads than the scan limit", async () => {
  const f = await sendFixture();
  for (let i = 0; i < 20; i++) {
    const id = `blocked${i}:send`;
    f.q.put(f.id, {
      id,
      stage: "send",
      depends_on: ["synthetic:reply_review", "synthetic:membership_verify"],
      input: { target: `@blocked${i}` },
    });
    f.k.sync({
      accounts: [],
      tasks: [],
      jobs: [
        {
          ...f.job,
          id: `send:blocked${i}`,
          expires_at: f.now() + 10000,
          reference_id: `blocked${i}`,
          payload: { ...f.job.payload, target: `@blocked${i}` },
        },
      ],
    });
    f.k.sql.exec(
      "INSERT INTO workflow_native_links VALUES(?,?,?,?,?)",
      f.id,
      id,
      `send:blocked${i}`,
      f.job.task_id,
      f.now(),
    );
  }
  f.q.put(f.id, {
    id: "healthy:join",
    stage: "join",
    depends_on: [],
    input: { target: "@healthy_group" },
  });
  f.k.sync({
    accounts: [],
    tasks: [],
    jobs: [
      {
        ...f.job,
        id: "join:healthy",
        expires_at: f.now() + 10000,
        kind: "join",
        action: "join",
        reference_id: "healthy",
        payload: { target: "@healthy_group", action: "join" },
        available_at: f.now(),
      },
    ],
  });
  f.k.sql.exec(
    "INSERT INTO workflow_native_links VALUES(?,?,?,?,?)",
    f.id,
    "healthy:join",
    "join:healthy",
    f.job.task_id,
    f.now(),
  );
  f.advance(901);
  const dispatch = f.gate.dispatch.bind(f.gate);
  let calls = 0,
    healthy = 0;
  f.gate.dispatch = async (b) => {
    calls++;
    if (b.step_id === "healthy:join") {
      healthy++;
      return { submitted: true };
    }
    return dispatch(b);
  };
  assert.equal((await f.gate.tick()).submitted, 0);
  assert.equal(calls, 16);
  f.advance(1801); // next cron can see old blocked entries; unseen candidates still lead.
  assert.equal((await f.gate.tick()).submitted, 1);
  assert.equal(healthy, 1);
  assert.ok(calls <= 32);
  assert.equal(
    f.k.one("SELECT attempt_id FROM jobs WHERE id=?", f.job.id).attempt_id,
    null,
  );
});

test("explicit executor cooldown or account lock before authorization stays pending", async () => {
  for (const reason of ["waiting_cooldown", "waiting_account_lock"]) {
    const f = await sendFixture();
    let calls = 0;
    f.env.TELEGRAM_EXECUTOR.fetch = async (url, request) => {
      const input = JSON.parse(request.body);
      calls++;
      return Response.json(
        {
          status: reason,
          verified: false,
          phase: "pre_execution",
          native_authorized: false,
          submission_started: false,
          job_id: input.job_id,
          account_id: input.account_id,
          job_version: input.version,
          attempt_id: input.attempt_id,
          retry_after_seconds: 120,
        },
        { status: 409 },
      );
    };
    const result = await f.gate.dispatch({
      plan_id: f.id,
      step_id: "synthetic:send",
    });
    const j = f.k.one("SELECT * FROM jobs WHERE id=?", f.job.id);
    assert.equal(result.submitted, false);
    assert.equal(j.state, "pending");
    assert.equal(j.attempt_id, null);
    assert.equal(j.available_at, f.now() + 120);
    assert.equal(
      f.k.one("SELECT state FROM worksteps WHERE id='synthetic:send'").state,
      "pending",
    );
    assert.equal(calls, 1);
  }
});

test("one stale reviewed send does not starve later eligible native jobs", async () => {
  const f = await sendFixture();
  f.advance(901);
  // The first linked job's review remains stale; a later due link is independently eligible.
  const originalDispatch = f.gate.dispatch.bind(f.gate);
  let later = 0;
  f.q.put(f.id, {
    id: "later:join",
    stage: "join",
    depends_on: [],
    input: { target: "@later_group" },
  });
  f.k.sql.exec(
    "INSERT INTO workflow_native_links VALUES(?,?,?,?,?)",
    f.id,
    "later:join",
    "join:later",
    f.job.task_id,
    f.now(),
  );
  f.k.sync({
    accounts: [],
    tasks: [],
    jobs: [
      {
        ...f.job,
        id: "join:later",
        kind: "join",
        action: "join",
        reference_id: "later",
        payload: { target: "@later_group", action: "join" },
        available_at: f.now() - 1,
      },
    ],
  });
  f.gate.dispatch = async (body) => {
    if (body.step_id === "synthetic:send") return originalDispatch(body);
    later++;
    return { submitted: true };
  };
  const result = await f.gate.tick();
  assert.equal(result.submitted, 1);
  assert.equal(later, 1);
  assert.equal(
    f.k.one("SELECT attempt_id FROM jobs WHERE id=?", f.job.id).attempt_id,
    null,
  );
  assert.equal(
    JSON.parse(
      f.k.one("SELECT result FROM worksteps WHERE id='synthetic:send'").result,
    ).reason,
    "NativeReplyContextNeedsRefresh",
  );
});
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
