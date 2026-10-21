import assert from "node:assert/strict";
import test from "node:test";
import { fixture } from "./prepared-replies-fixture.mjs";
import { TelegramPreparedReplies } from "../telegram-prepared-replies.mjs";
import { TelegramTaskBuilder } from "../telegram-task-builder.mjs";
import { similarReply } from "../telegram-reply-policy.mjs";

test("exact prepared reply flows through qualification, frozen child task, native authorization and verified receipt without a model", async () => {
  const f = await fixture();
  const prepared = await f.pipeline.prepare(f.packet);
  assert.equal((await f.pipeline.tick()).completed, 4);
  assert.equal((await f.builder.tick()).created, 1);
  const join = f.k.one(
    "SELECT * FROM telegram_generated_tasks WHERE step_id='synthetic:join'",
  );
  assert.ok(join);
  assert.equal(f.writes.length, 0, "preparation cannot submit to the platform");
  await f.gate.activate({ plan_id: f.id, source: "explicit_user_start" });
  assert.equal((await f.gate.tick()).submitted, 1);
  await f.reader.start(f.id, "explicit_user_read_start");
  f.k.sql.exec("UPDATE telegram_reader_runtime SET runs=2,next_at=0");
  await f.reader.tick();
  f.advance(2);
  assert.equal((await f.builder.tick()).created, 1);
  const send = f.k.one(
    "SELECT * FROM telegram_generated_tasks WHERE step_id='synthetic:send'",
  );
  const task = JSON.parse(
    f.k.one("SELECT payload FROM tasks WHERE id=?", send.task_id).payload,
  );
  const job = JSON.parse(
    f.k.one("SELECT payload FROM jobs WHERE id=?", send.job_id).payload,
  );
  assert.equal(job.payload.text, f.packet.text);
  assert.equal(job.payload.reply_to, f.packet.question_message_id);
  assert.deepEqual(task.policy, f.parent.policy);
  assert.equal(task.manifest_hash, send.manifest_hash);
  assert.equal((await f.gate.tick()).submitted, 1);
  assert.equal(f.writes.length, 2);
  assert.equal(f.writes[1].payload.text, f.packet.text);
  assert.ok(
    f.writes[1].scopes.some(
      (s) => s.scope === "cloud_task" && s.key === f.parent.id,
    ),
  );
  assert.equal((await f.builder.tick()).created, 0);
  assert.equal((await f.gate.tick()).submitted, 0);
  assert.equal(
    (await f.pipeline.prepare(f.packet)).packet_id,
    prepared.packet_id,
  );
  assert.equal(f.pipeline.snapshot(f.id).model_service_required, false);
  assert.equal(
    f.q.status(f.id).verified_interacted_groups,
    0,
    "sending alone cannot complete the goal",
  );
  f.advance(901);
  await f.reader.tick();
  assert.equal(f.q.status(f.id).verified_interacted_groups, 1);
});

test("restart preserves exact content and does not regenerate or replay native tasks", async () => {
  const f = await fixture();
  await f.pipeline.prepare(f.packet);
  await f.pipeline.tick();
  await f.builder.tick();
  const pipeline = new TelegramPreparedReplies(f.q, f.reader, f.env),
    builder = new TelegramTaskBuilder(f.q, f.gate, f.env);
  assert.equal(pipeline.snapshot(f.id).prepared_replies, 1);
  assert.equal((await builder.tick()).created, 0);
  assert.equal(f.k.one("SELECT COUNT(*) n FROM telegram_generated_tasks").n, 1);
  await assert.rejects(
    pipeline.prepare({
      ...f.packet,
      text: f.packet.text.replace("personal", "individual"),
    }),
    /FrozenPreparedReplyChanged/,
  );
});

test("cooldown and missing login create no read-context or native submission calls", async () => {
  for (const options of [{ cooldown: 3600 }, { authorized: false }]) {
    const f = await fixture(options);
    await f.pipeline.prepare(f.packet);
    await f.pipeline.tick();
    assert.equal(f.requests.includes("read_context"), false);
    assert.equal(f.writes.length, 0);
    assert.equal(
      f.pipeline.snapshot(f.id).state,
      options.cooldown ? "waiting_cooldown" : "waiting_cloud_login",
    );
    assert.equal((await f.builder.tick()).created, 0);
  }
});

test("no prepared text, scope exclusions, global stop and pause keep execution closed", async () => {
  const f = await fixture();
  await f.pipeline.tick();
  assert.equal(f.requests.length, 0);
  await assert.rejects(
    f.pipeline.prepare({ ...f.packet, target: "@legacy_group" }),
    /TargetNotInInteractionWorkflow/,
  );
  await f.pipeline.prepare(f.packet);
  f.k.configure({ global_stop: true });
  await f.pipeline.tick();
  assert.equal(f.requests.length, 0);
  f.k.configure({ global_stop: false });
  f.q.changeState(f.id, "paused", "explicit_user_pause");
  await f.pipeline.tick();
  assert.equal(f.requests.length, 0);
});

test("invented quotes, forbidden automation, stale rules and channel-only evidence cannot qualify", async () => {
  for (const kind of [
    "quote",
    "automation",
    "stale",
    "channel",
    "wrong-account",
    "bots",
  ]) {
    const f = await fixture();
    if (kind === "quote")
      f.packet.review.citations.topic_allowed[0].quote =
        "not in actual evidence";
    if (kind === "automation") f.observation.about += " No userbots.";
    if (kind === "stale")
      f.observation.checked_at = new Date(
        (f.now() - 90000) * 1000,
      ).toISOString();
    if (kind === "channel") f.observation.is_group = false;
    if (kind === "bots")
      f.observation.recent.forEach((m) => (m.author_is_bot = true));
    f.setObservation();
    if (kind === "wrong-account")
      f.k.sql.exec(
        'UPDATE worksteps SET result=replace(result,\'"account_id":"a"\',\'"account_id":"other"\') WHERE id=\'synthetic:inspect\'',
      );
    await f.pipeline.prepare(f.packet);
    await f.pipeline.tick();
    assert.equal(
      f.k.one(
        "SELECT state FROM worksteps WHERE id='synthetic:research_review'",
      ).state === "completed",
      false,
      kind,
    );
    assert.equal((await f.builder.tick()).created, 0);
  }
});

test("changed replies and missing full context stop publication until explicit evidence reconfirmation", async () => {
  const f = await fixture();
  await f.pipeline.prepare(f.packet);
  const evaluate = f.pipeline.evaluate.bind(f.pipeline);
  f.pipeline.evaluate = async (work, packet) => {
    if (work.stage === "reply_review")
      f.context.replies = [
        {
          id: 9,
          text: "This is already solved.",
          date: new Date(f.now() * 1000).toISOString(),
        },
      ];
    return evaluate(work, packet);
  };
  await f.pipeline.tick();
  const row = f.k.one(
    "SELECT state,result FROM worksteps WHERE id='synthetic:reply_review'",
  );
  assert.equal(row.state, "pending");
  assert.equal(
    JSON.parse(row.result).reason,
    "PreparedContextChangedNeedsReview",
  );
  assert.equal(f.writes.length, 0);
});

test("unverified product promotion is excluded while ordinary helpful text remains available", async () => {
  const f = await fixture();
  f.packet.text = f.packet.text.replace("a short note", "AIgram");
  await f.pipeline.prepare(f.packet);
  await f.pipeline.tick();
  assert.equal(
    f.k.one("SELECT state FROM worksteps WHERE id='synthetic:reply_review'")
      .state,
    "excluded",
  );
  assert.equal(f.writes.length, 0);
});

test("duplicate detection catches wording changes and case changes, not merely exact hashes", () => {
  const first =
    "Try saving a reference to the message in a personal reminder so you can return to the conversation and review what needs to happen next.";
  assert.equal(similarReply(first, first.toUpperCase()), true);
  assert.equal(
    similarReply(first, first.replace("personal reminder", "private reminder")),
    true,
  );
  assert.equal(
    similarReply(
      first,
      "Different conversations can require different translation languages. Check the language of the particular message before choosing a translation workflow and ask whether the user needs single message translation or automatic translation.",
    ),
    false,
  );
});

test("parent task pause and shared join limit are rechecked after child creation", async () => {
  const f = await fixture({ policy: { max_joins: 0 } });
  await f.pipeline.prepare(f.packet);
  await f.pipeline.tick();
  assert.equal((await f.builder.tick()).created, 0);
  const g = await fixture();
  await g.pipeline.prepare(g.packet);
  await g.pipeline.tick();
  await g.builder.tick();
  g.k.sql.exec("UPDATE tasks SET state='paused' WHERE id=?", g.parent.id);
  await assert.rejects(
    g.gate.dispatch({ plan_id: g.id, step_id: "synthetic:join" }),
    /PipelineReferenceTaskNotActive/,
  );
  assert.equal(g.writes.length, 0);
});

test("concurrent builders create one task and one durable link", async () => {
  const f = await fixture();
  await f.pipeline.prepare(f.packet);
  await f.pipeline.tick();
  const results = await Promise.all([
    f.builder.build(f.id, "synthetic:join"),
    f.builder.build(f.id, "synthetic:join"),
  ]);
  assert.equal(results.filter((r) => r.created).length, 1);
  assert.equal(f.k.one("SELECT COUNT(*) n FROM telegram_generated_tasks").n, 1);
  assert.equal(f.k.one("SELECT COUNT(*) n FROM workflow_native_links").n, 1);
});

test("generated children share the original task daily quota", async () => {
  const f = await fixture({ policy: { daily_limit: 1 } });
  await f.pipeline.prepare(f.packet);
  await f.pipeline.tick();
  await f.builder.tick();
  await f.gate.activate({ plan_id: f.id, source: "explicit_user_start" });
  await f.gate.tick();
  await f.reader.start(f.id, "explicit_user_read_start");
  f.k.sql.exec("UPDATE telegram_reader_runtime SET runs=2,next_at=0");
  await f.reader.tick();
  f.advance(2);
  await f.builder.tick();
  const job = f.k.one("SELECT j.* FROM jobs j WHERE kind='send'");
  const node = f.k.node(
    "cloudflare-telegram",
    await (await import("../kernel.mjs")).sha(f.env.TG_QUEUE_BRIDGE),
  );
  const lease = f.k.claim(node, "telegram.mtproto", job.id);
  await assert.rejects(
    f.gate.authorize({
      job_id: job.id,
      account_id: "a",
      version: job.version,
      lease_token: lease.lease_token,
      attempt_id: "synthetic-next-attempt",
    }),
    /QuotaExceeded/,
  );
  assert.equal(
    f.k.one("SELECT attempt_id FROM jobs WHERE id=?", job.id).attempt_id,
    null,
  );
  assert.equal(f.writes.length, 1);
});

test("reconfirmation refreshes evidence without changing text, and cannot replay an attempted send", async () => {
  const f = await fixture();
  const p = await f.pipeline.prepare(f.packet);
  await f.pipeline.tick();
  const changed = structuredClone(f.packet.review);
  changed.citations.claims_verified = [
    { source: "context:source", quote: "original chat context" },
  ];
  const r = await f.pipeline.reconfirm({
    plan_id: f.id,
    packet_id: p.packet_id,
    expected_review_hash: p.review_hash,
    review: changed,
    source: "explicit_context_review",
  });
  assert.equal(r.text_unchanged, true);
  assert.equal(
    f.k.one("SELECT state FROM worksteps WHERE id='synthetic:reply_review'")
      .state,
    "pending",
  );
  await assert.rejects(
    f.pipeline.reconfirm({
      plan_id: f.id,
      packet_id: p.packet_id,
      expected_review_hash: p.review_hash,
      review: changed,
      source: "stale_review",
    }),
    /PreparedReviewVersionChanged/,
  );
  f.k.sql.exec(
    "INSERT INTO telegram_generated_tasks VALUES(?,?,?,?,?,?,?)",
    f.id,
    "synthetic:send",
    p.packet_id,
    "attempted-task",
    "attempted-job",
    "hash",
    f.now(),
  );
  f.k.sql.exec(
    "INSERT INTO jobs(id,kind,reference_id,task_id,account_id,pool,version,payload,state,available_at,expires_at,attempt_id) VALUES('attempted-job','send','ref','attempted-task','a','telegram.mtproto','v','{}','unknown',?,?, 'already-attempted')",
    f.now(),
    f.now() + 100,
  );
  await assert.rejects(
    f.pipeline.reconfirm({
      plan_id: f.id,
      packet_id: p.packet_id,
      expected_review_hash: r.review_hash,
      review: changed,
      source: "explicit_context_review",
    }),
    /NativeAttemptCannotReplay/,
  );
});

test("new automation bans discovered at final review block the prepared text", async () => {
  const f = await fixture();
  await f.pipeline.prepare(f.packet);
  const evaluate = f.pipeline.evaluate.bind(f.pipeline);
  f.pipeline.evaluate = async (work, packet) => {
    if (work.stage === "reply_review")
      f.observation.about += " AI-generated comments are forbidden.";
    return evaluate(work, packet);
  };
  await f.pipeline.tick();
  assert.equal(
    f.k.one("SELECT state FROM worksteps WHERE id='synthetic:reply_review'")
      .state,
    "excluded",
  );
  assert.equal(f.writes.length, 0);
});

test("stopping the pipeline blocks already generated native tasks", async () => {
  const f = await fixture();
  await f.pipeline.prepare(f.packet);
  await f.pipeline.tick();
  await f.builder.tick();
  f.pipeline.stop({ plan_id: f.id, source: "explicit_user_stop" });
  await assert.rejects(
    f.gate.dispatch({ plan_id: f.id, step_id: "synthetic:join" }),
    /GeneratedTaskAuthorizationMissing/,
  );
  assert.equal(f.writes.length, 0);
});

test("a broadly qualified opportunity cannot create even a join until its exact reply passes final review", async () => {
  const f = await fixture();
  await f.pipeline.prepare(f.packet);
  await f.pipeline.tick();
  f.k.sql.exec("UPDATE worksteps SET state='pending' WHERE id='synthetic:reply_review'");
  await assert.rejects(f.builder.build(f.id, "synthetic:join"), /PreparedReplyDoesNotMatchReview/);
  assert.equal(f.k.one("SELECT COUNT(*) n FROM jobs").n, 0);
});
