import { DatabaseSync } from "node:sqlite";
import { ControlKernel, sha } from "../kernel.mjs";
import { WorkflowQueue, targetSteps } from "../workflow-queue.mjs";
import { TelegramNativeGate } from "../telegram-native-gate.mjs";
import { TelegramTaskBuilder } from "../telegram-task-builder.mjs";
import { TelegramPreparedReplies } from "../telegram-prepared-replies.mjs";
import { TelegramWorkflowReader } from "../telegram-workflow-reader.mjs";
import { ExecutorCore } from "../../telegram-executor/executor-core.mjs";

export async function fixture({
  cooldown = 0,
  authorized = true,
  rules,
  policy = {},
} = {}) {
  const db = new DatabaseSync(":memory:");
  let now = 1800000000,
    requests = [],
    writes = [];
  const storage = {
    sql: {
      exec(sql, ...args) {
        if (/CREATE TABLE/.test(sql)) {
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
      } catch (e) {
        db.exec("ROLLBACK");
        throw e;
      }
    },
  };
  const k = new ControlKernel(storage, { clock: () => now });
  k.configure({ global_stop: false, live_enabled: true });
  const parent = {
    id: "parent-task",
    state: "active",
    phase: "discovered",
    version: "parent-v1",
    account_ids: ["a"],
    policy: {
      execution_channel: "cloud",
      starts_at: new Date((now - 60) * 1000).toISOString(),
      ends_at: new Date((now + 86400) * 1000).toISOString(),
      min_interval: 1,
      daily_limit: 7,
      max_joins: 7,
      pacing_scope: "task",
      target_daily_limit: 1,
      user_daily_limit: 1,
      target_allowlist: ["@synthetic", "@second_group"],
      ...policy,
    },
  };
  k.sync({
    accounts: [
      {
        id: "a",
        identity: "telegram:a",
        platform: "telegram",
        mode: "live",
        cooldown_until: now + cooldown,
        limits: { daily: 100, hourly: 100 },
        min_interval: 1,
        allowed_actions: ["join", "reply"],
      },
    ],
    tasks: [parent],
    jobs: [],
  });
  const q = new WorkflowQueue(k),
    id = "WF-PREPARED-REPLIES";
  await q.create({
    source: "explicit_user_start",
    definition: {
      id,
      name: "synthetic",
      goal: 100,
      account_ids: ["a"],
      execution_channel: "cloud",
      local_fallback_allowed: false,
      native_task_ids: [parent.id],
      native_job_ids: [],
      requirements: {
        historical_attempted_targets_not_replayed: ["@legacy_group"],
      },
      product_facts: { recommendations_enabled: false, links_enabled: false },
      steps: [
        ...targetSteps("@synthetic"),
        ...targetSteps("@second_group"),
        {
          id: "legacy_group:inspect",
          stage: "inspect",
          depends_on: [],
          input: { target: "@legacy_group", outbound_excluded: true },
        },
      ],
    },
  });
  const questionText =
    "How can I keep a Telegram message for a personal follow-up without losing the original chat context?";
  const about =
    rules ||
    "Telegram message organization discussion. Free public group. Automation and helpful replies are allowed. English only. No product promotions or links.";
  const observation = {
    identity_checked: true,
    is_group: true,
    target: "@synthetic",
    discussion_id: "123",
    about,
    pinned: [],
    recent: [
      {
        id: 7,
        text: questionText,
        date: new Date(now * 1000).toISOString(),
        author_hash: await sha("1"),
        author_is_bot: false,
      },
      {
        id: 8,
        text: "My notes lose the source conversation too.",
        reply_to: 7,
        date: new Date(now * 1000).toISOString(),
        author_hash: await sha("2"),
        author_is_bot: false,
      },
    ],
    checked_at: new Date(now * 1000).toISOString(),
  };
  const context = {
    target: "@synthetic",
    discussion_id: "123",
    source: {
      id: 7,
      text: questionText,
      date: new Date(now * 1000).toISOString(),
      out: false,
      author_id: "1",
      author_is_bot: false,
    },
    replies: [],
    sample_truncated: false,
  };
  const env = {
    TELEGRAM_EXECUTOR_ACCOUNT: "a",
    TG_QUEUE_BRIDGE: "synthetic-bridge",
  };
  let gate;
  const receipts = new Map();
  const core = new ExecutorCore({
    store: {
      get: async (key) => receipts.get(key),
      put: async (key, value) => receipts.set(key, value),
    },
    clock: () => now,
    authorize: (input) => gate.authorize(input),
    submit: async (job, checkpoint, authorize) => {
      await authorize();
      await checkpoint({ submission_started: true });
      writes.push(structuredClone(job));
      return job.kind === "join"
        ? {
            status: "joined",
            verified: true,
            discussion_id: "123",
            external_reference: "telegram:membership:123",
          }
        : {
            status: "confirmed",
            verified: true,
            discussion_id: "123",
            message_ids: [42],
            external_reference: "telegram:@synthetic:42",
          };
    },
  });
  env.TELEGRAM_EXECUTOR = {
    fetch: async (url, request) => {
      const input = JSON.parse(request.body);
      requests.push(input.operation);
      if (input.operation === "status")
        return Response.json({
          login_state: authorized ? "authorized" : "not_logged_in",
          last_verified_at: authorized
            ? new Date(now * 1000).toISOString()
            : null,
        });
      if (input.operation === "health")
        return Response.json({ identity_verified: authorized });
      if (input.operation === "read_context")
        return Response.json({
          ...structuredClone(context),
          checked_at: new Date(now * 1000).toISOString(),
        });
      if (input.operation === "inspect")
        return Response.json({
          ...structuredClone(observation),
          checked_at: new Date(now * 1000).toISOString(),
        });
      if (input.operation === "execute")
        return Response.json(await core.execute(input));
      if (input.operation === "membership_verify")
        return Response.json({
          verified: true,
          discussion_id: "123",
          external_reference: "telegram:membership:123",
        });
      if (input.operation === "verify")
        return Response.json({
          verified: true,
          discussion_id: "123",
          message_ids: [42],
          external_reference: "telegram:@synthetic:42",
        });
      throw new Error("UnexpectedSyntheticOperation");
    },
  };
  gate = new TelegramNativeGate(q, env);
  const reader = new TelegramWorkflowReader(q, env),
    pipeline = new TelegramPreparedReplies(q, reader, env),
    builder = new TelegramTaskBuilder(q, gate, env);
  const text =
    "For a personal follow-up, keep a short note describing your next action and include a reference to the original conversation. That gives you somewhere to return when you need context. What reminder method are you using now?";
  const cite = (source, quote) => [{ source, quote }];
  const packet = {
    plan_id: id,
    target: "@synthetic",
    question_message_id: 7,
    text,
    source: "explicit_user_prepared_reply",
    review: {
      language: "en",
      topic_allowed: true,
      free_real_identity: true,
      automation_allowed: true,
      rules_allow_replies: true,
      question_unresolved: true,
      claims_verified: true,
      language_matches: true,
      support_only: false,
      requires_ai_disclosure: false,
      citations: {
        topic_allowed: cite(
          "about",
          "Telegram message organization discussion",
        ),
        free_real_identity: cite("about", "Free public group"),
        automation_allowed: cite(
          "about",
          "Automation and helpful replies are allowed",
        ),
        rules_allow_replies: cite("about", "helpful replies are allowed"),
        question_unresolved: cite("context:source", questionText),
        claims_verified: cite("context:source", "personal follow-up"),
        language_matches: cite("about", "English only"),
      },
    },
  };
  const setObservation = () =>
    k.sql.exec(
      "UPDATE worksteps SET state='completed',result=? WHERE plan_id=? AND id='synthetic:inspect'",
      JSON.stringify({
        verdict: "pass",
        facts: {
          identity_checked: true,
          rules_read: true,
          recent_context_read: true,
        },
        evidence: [
          { transport: "authenticated_mtproto", account_id: "a", observation },
        ],
      }),
      id,
    );
  setObservation();
  await pipeline.start({
    plan_id: id,
    reference_task_id: parent.id,
    source: "explicit_user_start",
  });
  return {
    db,
    k,
    q,
    env,
    id,
    gate,
    builder,
    pipeline,
    reader,
    parent,
    packet,
    context,
    observation,
    setObservation,
    requests,
    writes,
    now: () => now,
    advance: (seconds) => (now += seconds),
  };
}
