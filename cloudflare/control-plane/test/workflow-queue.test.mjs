import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { ControlKernel } from "../kernel.mjs";
import { WorkflowQueue } from "../workflow-queue.mjs";

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
  const q = new WorkflowQueue(k);
  const definition = {
    id: "WF-SYNTHETIC-100",
    name: "synthetic whole workflow",
    execution_channel: "cloud",
    local_fallback_allowed: false,
    goal: 100,
    account_ids: ["a"],
    native_task_ids: [],
    native_job_ids: [],
    requirements: { language: ["en", "ru"] },
    product_facts: { recommendations_enabled: false, links_enabled: false },
    steps: [
      {
        id: "search",
        stage: "discover",
        depends_on: [],
        input: { query: "synthetic" },
      },
      {
        id: "inspect",
        stage: "inspect",
        depends_on: ["search"],
        input: { target: "@synthetic" },
      },
      {
        id: "join",
        stage: "join",
        depends_on: ["inspect"],
        input: { target: "@synthetic" },
      },
      { id: "goal", stage: "goal_check", depends_on: [], input: { goal: 100 } },
    ],
  };
  function node(stages = ["discover", "inspect", "goal_check"]) {
    q.registerNode({
      id: "remote",
      token_hash: "a".repeat(64),
      runtime: "cloud",
      stages,
    });
    return q.node("remote", "a".repeat(64));
  }
  return {
    k,
    q,
    db,
    definition,
    node,
    advance: (n) => {
      now += n;
    },
  };
}
test("whole workflow survives coordinator reconstruction; dependencies and missing executor remain visible", async () => {
  const f = fixture();
  await f.q.create({
    definition: f.definition,
    source: "explicit_human_start",
  });
  let s = new WorkflowQueue(f.k).status(f.definition.id);
  assert.equal(s.total, 4);
  assert.equal(s.counts.waiting_cloud_executor, 2);
  assert.equal(s.counts.waiting_dependency, 2);
  assert.equal(s.goal_completed, false);
  assert.equal(s.verified_interacted_groups, 0);
  assert.deepEqual(s.stages.join, { waiting_dependency: 1 });
});

test("scoped pause stops new claims and leaves the global stop and definition untouched", async () => {
  const f = fixture();
  await f.q.create({ definition: f.definition, source: "explicit" });
  const n = f.node();
  const hash = f.q.plan(f.definition.id).manifest_hash;
  f.q.changeState(f.definition.id, "paused", "explicit_user_pause");
  assert.equal(f.q.claim(n, f.definition.id).step, null);
  assert.equal(f.k.settings().global_stop, false);
  assert.equal(f.q.plan(f.definition.id).manifest_hash, hash);
  f.q.changeState(f.definition.id, "active", "explicit_user_resume");
  assert.ok(f.q.claim(n, f.definition.id).step);
});

test("discovery continues after each batch and fans out deduplicated full pipelines", async () => {
  const f = fixture();
  await f.q.create({ definition: f.definition, source: "explicit" });
  const n = f.node(["discover"]);
  const c = f.q.claim(n, f.definition.id).step;
  const result = {
    verdict: "pass",
    evidence: ["synthetic search context"],
    targets: ["new_chat", "new_chat"],
  };
  f.q.finish(n, {
    plan_id: f.definition.id,
    id: c.id,
    lease_token: c.lease_token,
    result,
  });
  let status = f.q.status(f.definition.id);
  assert.equal(status.total, 14);
  assert.equal(
    status.steps.find((s) => s.id === "search").state,
    "waiting_schedule",
  );
  assert.equal(status.steps.filter((s) => s.stage === "send").length, 1);
  f.advance(3601);
  const again = f.q.claim(n, f.definition.id).step;
  f.q.finish(n, {
    plan_id: f.definition.id,
    id: again.id,
    lease_token: again.lease_token,
    result,
  });
  assert.equal(f.q.status(f.definition.id).total, 14);
  assert.equal(f.k.one("SELECT count(*) n FROM jobs").n, 0);
});
test("workflow manifest is idempotent and immutable", async () => {
  const f = fixture(),
    input = { definition: f.definition, source: "explicit_human_start" };
  const first = await f.q.create(input);
  assert.equal((await f.q.create(input)).manifest_hash, first.manifest_hash);
  f.definition.goal = 101;
  await assert.rejects(f.q.create(input), /FrozenWorkflowChanged/);
});
test("local executors and write-capable workflow nodes cannot register or consume", async () => {
  const f = fixture();
  await f.q.create({ definition: f.definition, source: "explicit" });
  assert.throws(
    () =>
      f.q.registerNode({
        id: "local",
        token_hash: "a".repeat(64),
        runtime: "local",
        stages: ["discover"],
      }),
    /InvalidCloudWorkflowNode/,
  );
  assert.throws(
    () =>
      f.q.registerNode({
        id: "remote",
        token_hash: "a".repeat(64),
        runtime: "cloud",
        stages: ["join"],
      }),
    /InvalidCloudWorkflowNode/,
  );
  assert.throws(
    () => f.q.node("windows-primary", "a".repeat(64)),
    /CloudWorkflowNodeRequired/,
  );
});
test("no native attempts are created; read leases resume after expiry without unlocking writes", async () => {
  const f = fixture();
  await f.q.create({ definition: f.definition, source: "explicit" });
  const n = f.node();
  const claim = f.q.claim(n, f.definition.id).step;
  assert.equal(claim.id, "search");
  f.advance(181);
  assert.throws(
    () =>
      f.q.finish(n, {
        plan_id: f.definition.id,
        id: "search",
        lease_token: claim.lease_token,
        result: { verdict: "pass", evidence: ["synthetic"] },
      }),
    /WorkflowLeaseExpired/,
  );
  assert.equal(f.q.claim(n, f.definition.id).step.id, "search");
  assert.equal(f.k.one("SELECT count(*) n FROM jobs").n, 0);
});
test("global stop prevents claims; exclusions and delays retain honest dependency states", async () => {
  const f = fixture();
  await f.q.create({ definition: f.definition, source: "explicit" });
  const n = f.node();
  f.k.configure({ global_stop: true });
  assert.equal(f.q.claim(n, f.definition.id).step, null);
  f.k.configure({ global_stop: false });
  const c = f.q.claim(n, f.definition.id).step;
  f.q.finish(n, {
    plan_id: f.definition.id,
    id: c.id,
    lease_token: c.lease_token,
    result: { verdict: "defer", reason: "no suitable new results" },
  });
  assert.equal(f.q.status(f.definition.id).counts.waiting_schedule, 1);
  assert.equal(f.q.status(f.definition.id).counts.waiting_dependency, 2);
});
test("completing an easy stage cannot declare the 100 group goal achieved", async () => {
  const f = fixture();
  await f.q.create({ definition: f.definition, source: "explicit" });
  const n = f.node(["goal_check"]);
  const c = f.q.claim(n, f.definition.id).step;
  assert.throws(
    () =>
      f.q.finish(n, {
        plan_id: f.definition.id,
        id: c.id,
        lease_token: c.lease_token,
        result: { verdict: "pass", evidence: ["synthetic"] },
      }),
    /WorkflowGoalNotReached/,
  );
});
test("invalid graphs and credentials never enter durable workflow storage", async () => {
  const f = fixture();
  f.definition.steps[0].depends_on = ["inspect"];
  await assert.rejects(
    f.q.create({ definition: f.definition, source: "explicit" }),
    /WorkflowCycle/,
  );
  f.definition.steps[0].depends_on = [];
  f.definition.requirements.session_string = "synthetic";
  await assert.rejects(
    f.q.create({ definition: f.definition, source: "explicit" }),
    /CredentialInWorkflow/,
  );
  assert.equal(f.k.one("SELECT count(*) n FROM workplans").n, 0);
});
test("research and replies require concrete evidence; unverified product mentions stay blocked", async () => {
  const f = fixture();
  const p = { definition: f.definition };
  assert.throws(
    () =>
      f.q.checkEvidence(
        { stage: "research_review" },
        { evidence: ["synthetic"], facts: {} },
        p,
      ),
    /WorkflowQualificationIncomplete/,
  );
  const r = {
    evidence: ["synthetic"],
    facts: {
      automation_allowed: true,
      question_unresolved: true,
      context_read: true,
      language_matches: true,
      nonduplicate: true,
      claims_verified: true,
    },
    question: {
      message_id: 1,
      discussion_id: "@synthetic",
      published_at: new Date(1800000000000).toISOString(),
    },
    text: "Try AIgram for this.",
  };
  assert.throws(
    () => f.q.checkEvidence({ stage: "reply_review" }, r, p),
    /WorkflowProductNotVerified/,
  );
  r.text = "Which Android client and version are you using?";
  assert.doesNotThrow(() => f.q.checkEvidence({ stage: "reply_review" }, r, p));
});
