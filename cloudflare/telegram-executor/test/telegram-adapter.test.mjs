import assert from "node:assert/strict";
import test from "node:test";
import { submitOperation, readOperation, safeError } from "../telegram-adapter.mjs";
import { ExecutorCore } from "../executor-core.mjs";
function fixture({ newReply = false, changed = false } = {}) {
  let sends = 0,
    authorizations = 0;
  const now = Date.now(),
    entity = { id: 123, username: "synthetic", megagroup: true };
  const client = {
    getEntity: async () => entity,
    getParticipant: async () => ({
      participant: { className: "ChannelParticipantSelf" },
    }),
    getMessages: async (e, opts) => {
      if (opts.replyTo)
        return newReply ? [{ id: 8, date: Math.floor(now / 1000) + 5 }] : [];
      if (opts.ids[0] === 7)
        return [
          {
            id: 7,
            out: false,
            message: changed ? "changed" : "original question",
            date: Math.floor(now / 1000),
          },
        ];
      return [
        {
          id: 9,
          out: true,
          message: "frozen helpful reply",
          replyTo: { replyToMsgId: 7 },
        },
      ];
    },
    sendMessage: async (e, opts) => {
      sends++;
      assert.equal(opts.message, "frozen helpful reply");
      assert.equal(opts.replyTo, 7);
      return { id: 9 };
    },
  };
  const job = {
    kind: "send",
    payload: {
      target: "@synthetic",
      action: "reply",
      text: "frozen helpful reply",
      reply_to: 7,
      media: [],
    },
    workflow_context: {
      question: {
        message_id: 7,
        text: "original question",
        discussion_id: "123",
        published_at: new Date(Math.floor(now / 1000) * 1000).toISOString(),
      },
      reviewed_at: new Date(now).toISOString(),
    },
  };
  return {
    client,
    job,
    sends: () => sends,
    authorize: async () => {
      authorizations++;
    },
    authorizations: () => authorizations,
  };
}
test("text executor rechecks the original message, submits once and independently verifies its receipt", async () => {
  const f = fixture(),
    checkpoints = [];
  const r = await submitOperation(
    f.client,
    f.job,
    async (value) => checkpoints.push(value),
    f.authorize,
  );
  assert.equal(f.sends(), 1);
  assert.equal(f.authorizations(), 1);
  assert.equal(r.verified, true);
  assert.equal(r.status, "confirmed");
  assert.ok(checkpoints[0].submission_started);
  assert.deepEqual(checkpoints.at(-1).message_ids, [9]);
});
test("changed questions or new replies stop submission before the final native authorization", async () => {
  for (const opts of [{ changed: true }, { newReply: true }]) {
    const f = fixture(opts);
    await assert.rejects(
      submitOperation(f.client, f.job, async () => {}, f.authorize),
      /ReplyContextChanged/,
    );
    assert.equal(f.sends(), 0);
    assert.equal(f.authorizations(), 0);
  }
});
test("public usernames belonging to people cannot be used to read private conversations", async () => {
  let reads = 0;
  const client = {
    getEntity: async () => ({ username: "person", className: "User" }),
    getMessages: async () => {
      reads++;
      return [];
    },
  };
  const r = await readOperation(client, "read_context", {
    target: "@person",
    message_id: 7,
  });
  assert.equal(r.status, "unavailable");
  assert.equal(reads, 0);
});

test("channel speaking rejection is known failure, keeps once-only receipt and never disables globally", async () => {
  const f = fixture(), rows = new Map();
  let sends = 0;
  f.client.sendMessage = async () => { sends++; throw { errorMessage: "USER_BANNED_IN_CHANNEL" }; };
  const request = { job_id: "send:rejected", account_id: "a", version: "v", attempt_id: "one", lease_token: "l" };
  const core = new ExecutorCore({
    store: { get: async k => rows.get(k), put: async (k,v) => rows.set(k, structuredClone(v)) },
    authorize: async () => ({ allowed: true, lease_until: Date.now()/1000+60, job: { ...f.job, id: request.job_id, account_id: "a", version: "v" } }),
    submit: (job, checkpoint, authorize) => submitOperation(f.client, job, checkpoint, authorize),
    classifyError: safeError,
  });
  const rejected = await core.execute(request);
  assert.equal(rejected.status, "failed_known");
  assert.equal(rejected.account_restriction, "telegram_speaking_restricted");
  assert.equal(rejected.risk_warning, undefined);
  assert.equal(rejected.discussion_id, "123");
  assert.equal(rejected.confirmed_rejected, true);
  assert.equal(rejected.confirmed_not_submitted, false);
  await core.execute(request);
  assert.equal(sends, 1);
});

test("channel restriction after accepted ID stays unknown while timeouts remain ambiguous", () => {
  const rejected = { errorMessage: "USER_BANNED_IN_CHANNEL" };
  assert.equal(safeError(rejected, { submission_started: true, message_ids: [9] }).status, "unknown");
  assert.equal(safeError(rejected, { submission_started: true, message_ids: [9] }).confirmed_rejected, false);
  assert.equal(safeError(rejected, { submission_started: false }).confirmed_not_submitted, true);
  assert.equal(safeError(rejected, { submission_started: false }).confirmed_rejected, false);
  assert.equal(safeError(new Error("socket timeout"), { submission_started: true }).status, undefined);
  assert.equal(safeError(rejected).risk_warning, undefined);
});
