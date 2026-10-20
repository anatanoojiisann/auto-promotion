import assert from "node:assert/strict";
import test from "node:test";
import { submitOperation, readOperation } from "../telegram-adapter.mjs";
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
