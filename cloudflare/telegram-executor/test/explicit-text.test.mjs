import assert from "node:assert/strict";
import test from "node:test";
import { submitOperation } from "../telegram-adapter.mjs";
import { ExecutorCore } from "../executor-core.mjs";
function fixture({
  member = true,
  banned = false,
  missingHash = false,
  timeout = false,
  wrongReceipt = false,
} = {}) {
  const entity = {
    className: "Channel",
    id: 2123962275n,
    megagroup: true,
    accessHash: missingHash ? null : 42n,
    defaultBannedRights: { sendMessages: banned },
  };
  let sends = 0;
  const client = {
    iterDialogs: async function* () {
      yield { entity };
    },
    getParticipant: async () => ({
      participant: {
        className: member ? "ChannelParticipantSelf" : "ChannelParticipantLeft",
      },
    }),
    sendMessage: async (e, p) => {
      sends++;
      assert.equal(p.replyTo, undefined);
      if (timeout) throw Error("timeout");
      return { id: 9 };
    },
    getMessages: async () => [
      { id: 9, out: true, message: wrongReceipt ? "other" : "synthetic text" },
    ],
  };
  const job = {
    id: "job",
    version: "v1",
    account_id: "a",
    kind: "send",
    payload: {
      action: "send_text",
      target: "-1002123962275",
      text: "synthetic text",
      reply_to: null,
      media: [],
    },
    explicit_text: {
      target: "-1002123962275",
      text: "synthetic text",
      account_id: "a",
      manifest_hash: "frozen",
    },
  };
  return { client, job, sends: () => sends };
}
test("private-group plain text has no reply context, checkpoints before RPC and verifies message", async () => {
  const f = fixture();
  let checkpointed = false;
  const send = f.client.sendMessage;
  f.client.sendMessage = async (...a) => {
    assert.equal(checkpointed, true);
    return send(...a);
  };
  const r = await submitOperation(
    f.client,
    f.job,
    async (r) => {
      checkpointed ||= !!r.submission_started;
    },
    async () => {},
  );
  assert.equal(r.status, "confirmed");
  assert.equal(f.sends(), 1);
});
test("nonmembers, individual/group bans, missing hash and changed intent fail before submission", async () => {
  for (const opt of [
    { member: false },
    { banned: true },
    { missingHash: true },
  ]) {
    const f = fixture(opt);
    await assert.rejects(
      submitOperation(
        f.client,
        f.job,
        async () => {},
        async () => {},
      ),
    );
    assert.equal(f.sends(), 0);
  }
  for (const patch of [
    { text: "other" },
    { account_id: "other" },
    { target: "-100123" },
    { manifest_hash: "" },
  ]) {
    const f = fixture();
    Object.assign(f.job.explicit_text, patch);
    await assert.rejects(
      submitOperation(
        f.client,
        f.job,
        async () => {},
        async () => {},
      ),
      /ExplicitTextAuthorizationRequired/,
    );
    assert.equal(f.sends(), 0);
  }
  const f = fixture();
  f.client.getParticipant = async () => ({
    participant: {
      className: "ChannelParticipantSelf",
      bannedRights: { sendMessages: true },
    },
  });
  await assert.rejects(
    submitOperation(
      f.client,
      f.job,
      async () => {},
      async () => {},
    ),
    /TargetWriteForbidden/,
  );
});
test("timeout after submission survives executor reconstruction and is never resent", async () => {
  const f = fixture({ timeout: true });
  const data = new Map();
  const store = {
    get: async (k) => data.get(k),
    put: async (k, v) => data.set(k, v),
  };
  const auth = async () => ({ allowed: true, job: f.job, lease_until: 2000 });
  const options = {
    store,
    authorize: auth,
    submit: (...a) => submitOperation(f.client, ...a),
    clock: () => 1000,
  };
  const req = {
    job_id: "job",
    version: "v1",
    account_id: "a",
    attempt_id: "once",
    lease_token: "lease",
  };
  const first = await new ExecutorCore(options).execute(req);
  assert.equal(first.status, "unknown");
  assert.equal(first.submission_started, true);
  const next = await new ExecutorCore(options).execute(req);
  assert.equal(next.from_durable_receipt, true);
  assert.equal(f.sends(), 1);
});
test("final gate detects account or target changes even with same version", async () => {
  for (const field of ["account_id", "payload"]) {
    const f = fixture();
    let calls = 0;
    const store = { get: async () => null, put: async () => {} };
    const core = new ExecutorCore({
      store,
      clock: () => 1000,
      authorize: async () => ({
        allowed: true,
        lease_until: 2000,
        job:
          ++calls === 1
            ? f.job
            : {
                ...f.job,
                [field]:
                  field === "payload"
                    ? { ...f.job.payload, target: "-123" }
                    : "other",
              },
      }),
      submit: (...a) => submitOperation(f.client, ...a),
    });
    await core.execute({
      job_id: "job",
      version: "v1",
      account_id: "a",
      attempt_id: "once",
      lease_token: "lease",
    });
    assert.equal(f.sends(), 0);
  }
});
test("plain text rejects reply/media and legacy reply still requires reviewed context", async () => {
  for (const patch of [{ reply_to: 7 }, { media: [{}] }]) {
    const f = fixture();
    Object.assign(f.job.payload, patch);
    await assert.rejects(
      submitOperation(
        f.client,
        f.job,
        async () => {},
        async () => {},
      ),
      /InvalidExplicitText/,
    );
  }
  const f = fixture();
  f.job.payload.action = "reply";
  f.client.getEntity = async () => ({
    id: 1,
    username: "synthetic",
    megagroup: true,
  });
  await assert.rejects(
    submitOperation(
      f.client,
      f.job,
      async () => {},
      async () => {},
    ),
    /ReplyContextRequired/,
  );
});
