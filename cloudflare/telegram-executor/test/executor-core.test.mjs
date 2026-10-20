import assert from "node:assert/strict";
import test from "node:test";
import { ExecutorCore } from "../executor-core.mjs";
const request = {
  job_id: "send:synthetic",
  account_id: "account",
  version: "frozen-v1",
  attempt_id: "attempt-1",
  lease_token: "synthetic-lease",
  text: "untrusted caller text",
};
function fixture(submit, authorize) {
  const rows = new Map(),
    store = {
      async get(k) {
        return rows.get(k);
      },
      async put(k, v) {
        rows.set(k, structuredClone(v));
      },
    };
  const allowed = {
    allowed: true,
    lease_until: 200,
    job: {
      id: request.job_id,
      version: request.version,
      account_id: "account",
      kind: "send",
      payload: { target: "@synthetic", text: "frozen text" },
    },
  };
  return {
    rows,
    allowed,
    core: new ExecutorCore({
      store,
      submit,
      authorize: authorize || (async () => allowed),
      clock: () => 100,
    }),
  };
}
test("an unleased or stopped job cannot create an attempt or call Telegram", async () => {
  let calls = 0;
  const f = fixture(
    async () => {
      calls++;
    },
    async () => ({ allowed: false }),
  );
  await assert.rejects(
    f.core.execute(request),
    /NativeQueueAuthorizationRequired/,
  );
  await assert.rejects(f.core.execute({ job_id: "x" }), /NativeLeaseRequired/);
  assert.equal(calls, 0);
  assert.equal(f.rows.size, 0);
});
test("only the server-frozen payload is passed to the platform; receipt exists before submission", async () => {
  const f = fixture(async (job, checkpoint, authorize) => {
    assert.equal(job.payload.text, "frozen text");
    assert.equal(f.rows.get("receipt:" + request.job_id).status, "unknown");
    await authorize();
    await checkpoint({ submission_started: true, message_ids: [42] });
    return { status: "confirmed", verified: true, message_ids: [42] };
  });
  const r = await f.core.execute(request);
  assert.equal(r.status, "confirmed");
  assert.deepEqual(r.message_ids, [42]);
});
test("a stop between claim and submission quarantines the attempt without sending", async () => {
  let auth = 0,
    sends = 0;
  const f = fixture(
    async (job, checkpoint, authorize) => {
      await authorize();
      sends++;
      return { status: "confirmed" };
    },
    async () =>
      ++auth === 1
        ? {
            allowed: true,
            lease_until: 200,
            job: {
              id: request.job_id,
              version: request.version,
              account_id: "account",
            },
          }
        : { allowed: false },
  );
  assert.equal((await f.core.execute(request)).status, "unknown");
  assert.equal(sends, 0);
  assert.equal((await f.core.execute(request)).from_durable_receipt, true);
  assert.equal(sends, 0);
});
test("lost verification never repeats a write and keeps accepted message ids", async () => {
  let sends = 0;
  const f = fixture(async (job, checkpoint, authorize) => {
    await authorize();
    sends++;
    await checkpoint({ submission_started: true, message_ids: [12] });
    throw new Error("synthetic timeout");
  });
  let r = await f.core.execute(request);
  assert.equal(r.status, "unknown");
  assert.deepEqual(r.message_ids, [12]);
  r = await f.core.execute(request);
  assert.equal(r.from_durable_receipt, true);
  assert.equal(sends, 1);
  await assert.rejects(
    f.core.execute({ ...request, version: "changed" }),
    /FrozenExecutorReceiptChanged/,
  );
});
test("wrong account, payload version and expired leases stay closed", async () => {
  for (const change of [
    { lease_until: 99 },
    {
      job: {
        id: request.job_id,
        account_id: "other",
        version: request.version,
      },
    },
    { job: { id: request.job_id, account_id: "account", version: "other" } },
  ]) {
    const f = fixture(async () => {
      throw new Error("must not send");
    });
    Object.assign(f.allowed, change);
    await assert.rejects(
      f.core.execute(request),
      /NativeQueueAuthorizationRequired/,
    );
    assert.equal(f.rows.size, 0);
  }
});
