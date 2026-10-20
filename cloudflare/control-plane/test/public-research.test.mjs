import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import test from "node:test";
import { ControlKernel } from "../kernel.mjs";
import { WorkflowQueue } from "../workflow-queue.mjs";
import { PublicResearch, telegramTargets } from "../public-research.mjs";

async function fixture(fetcher) {
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
          rows = s.columns().length ? s.all(...args) : (s.run(...args), []);
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
  const q = new WorkflowQueue(k),
    id = "WF-PUBLIC-RESEARCH";
  await q.create({
    source: "explicit_human_start",
    definition: {
      id,
      name: "synthetic",
      execution_channel: "cloud",
      local_fallback_allowed: false,
      goal: 100,
      account_ids: ["a"],
      native_task_ids: [],
      native_job_ids: [],
      requirements: { language: ["en", "ru"] },
      steps: [
        {
          id: "search",
          stage: "discover",
          depends_on: [],
          input: { query: "synthetic tools" },
        },
        {
          id: "one:inspect",
          stage: "inspect",
          depends_on: [],
          input: { target: "@test_one" },
        },
        {
          id: "two:inspect",
          stage: "inspect",
          depends_on: [],
          input: { target: "@test_two" },
        },
        {
          id: "one:join",
          stage: "join",
          depends_on: ["one:inspect"],
          input: { target: "@test_one" },
        },
      ],
    },
  });
  const p = new PublicResearch(q, { fetcher });
  return { k, q, p, id, db, advance: (n) => (now += n) };
}
test("public executor reads three pages through cloud leases; partial profile evidence never qualifies a group", async () => {
  const urls = [];
  const f = await fixture(async (url) => {
    urls.push(url);
    return new Response(
      url.includes("bing.com")
        ? '<a href="https://t.me/new_group">synthetic result</a>'
        : "<h1>Synthetic profile only</h1>",
    );
  });
  await f.p.start(f.id, "explicit_start", "synthetic-secret");
  const r = await f.p.tick();
  assert.equal(r.runs[0].public_reads, 3);
  assert.equal(r.runs[0].telegram_actions, 0);
  assert.equal(urls.filter((u) => u.startsWith("https://t.me/")).length, 2);
  const work = f.k.one(
    "SELECT state,result FROM worksteps WHERE id=?",
    "one:inspect",
  );
  assert.equal(work.state, "pending");
  const result = JSON.parse(work.result);
  assert.equal(result.verdict, "defer");
  assert.equal(result.partial.rules_fully_verified, false);
  assert.equal(result.evidence[0].http_status, 200);
  assert.equal(result.evidence[0].sha256.length, 64);
  assert.equal(f.q.status(f.id).verified_interacted_groups, 0);
  assert.equal(f.k.one("SELECT COUNT(*) n FROM jobs").n, 0);
  assert.equal(f.p.snapshot(f.id).reads, 3);
  await f.p.tick();
  assert.equal(urls.length, 3); // cadence survives another request
});
test("stop, daily budget and remote overlap prevent additional public work", async () => {
  let calls = 0;
  const f = await fixture(async () => {
    calls++;
    return new Response("<h1>Public profile</h1>");
  });
  await f.p.start(f.id, "explicit_start", "synthetic-secret");
  f.k.configure({ global_stop: true });
  await f.p.tick();
  assert.equal(calls, 0);
  f.k.configure({ global_stop: false });
  f.k.sql.exec(
    "UPDATE public_workflow_runtime SET day=?,daily_runs=96",
    new Date(1800000000000).toISOString().slice(0, 10),
  );
  await f.p.tick();
  assert.equal(calls, 0);
  f.k.sql.exec(
    "UPDATE public_workflow_runtime SET daily_runs=0,lease_until=?",
    1800000100,
  );
  await f.p.tick();
  assert.equal(calls, 0);
});
test("challenge pages are retained as unavailable evidence without bypass or invented discoveries", async () => {
  const f = await fixture(
    async () =>
      new Response("Please verify you are human CAPTCHa", { status: 403 }),
  );
  await f.p.start(f.id, "explicit_start", "synthetic-secret");
  await f.p.tick();
  const row = f.k.one(
    "SELECT state,result FROM worksteps WHERE id=?",
    "search",
  );
  assert.equal(row.state, "pending");
  assert.equal(
    JSON.parse(row.result).reason,
    "public_source_unavailable_or_challenge",
  );
  assert.equal(f.q.status(f.id).total, 4);
  assert.equal(f.k.one("SELECT COUNT(*) n FROM jobs").n, 0);
});
test("public target parser accepts observed Telegram links and ignores sharing and invite endpoints", () => {
  assert.deepEqual(
    telegramTargets(
      '<a href="https://t.me/new_group">x</a> https://t.me/share/url?url=x https://t.me/joinchat/invite https://t.me/s/new_group/42',
    ),
    ["@new_group"],
  );
});

test("scoped public stop does not affect other queues and prevents further reads", async () => {
  let calls = 0;
  const f = await fixture(async () => {
    calls++;
    return new Response("Public page");
  });
  await f.p.start(f.id, "explicit_start", "synthetic-secret");
  f.p.stop(f.id, "explicit_user_stop");
  await f.p.tick();
  assert.equal(calls, 0);
  assert.equal(f.p.snapshot(f.id).enabled, 0);
  assert.equal(f.q.plan(f.id).state, "active");
  assert.equal(f.k.settings().global_stop, false);
});
