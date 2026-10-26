import assert from "node:assert/strict";
import test from "node:test";
import { TelegramReadPolicy } from "../telegram-read-policy.mjs";
import { readOperation, safeError } from "../telegram-adapter.mjs";
import { ExecutorCore } from "../executor-core.mjs";

function fixture({ policy = {}, identity = "account:one", revision = 1 } = {}) {
  const data = new Map();
  let now = 1800000000,
    calls = 0;
  const store = {
    get: async (key) => structuredClone(data.get(key)),
    put: async (key, value) => data.set(key, structuredClone(value)),
    delete: async (key) => data.delete(key),
  };
  let transaction = Promise.resolve();
  store.transaction = (action) => {
    const result = transaction.then(() => action(store));
    transaction = result.catch(() => {});
    return result;
  };
  const messages = [5, 4, 3, 2, 1].map((id) => ({
    id,
    message: "sample " + id,
    date: now,
    out: false,
  }));
  const entity = {
    className: "Channel",
    id: 123n,
    accessHash: 456n,
    username: "synthetic",
    title: "Synthetic",
    megagroup: true,
  };
  const client = {
    invoke: async (request) => {
      calls++;
      return request.className === "channels.GetFullChannel"
        ? { fullChat: { about: "rules", participantsCount: 1 } }
        : request.className === "contacts.Search"
          ? { chats: [entity] }
          : {};
    },
    getEntity: async () => {
      await client.invoke({ className: "contacts.ResolveUsername" });
      return entity;
    },
    getMessages: async (e, options) => {
      await client.invoke({
        className: options.filter ? "messages.Search" : "messages.GetHistory",
      });
      if (options.filter) return [];
      if (options.ids)
        return messages.filter((m) => options.ids.includes(m.id));
      return messages
        .filter(
          (m) =>
            (!options.offsetId || m.id < options.offsetId) &&
            m.id > (options.minId || 0),
        )
        .slice(0, options.limit);
    },
    getParticipant: async () => {
      await client.invoke({ className: "channels.GetParticipant" });
      return { participant: { className: "ChannelParticipantSelf" } };
    },
  };
  const make = (overrides = {}) =>
    new TelegramReadPolicy(store, {
      accountId: "a",
      identity,
      revision,
      clock: () => now,
      policy: { ...policy, ...overrides },
    });
  async function run(operation = "inspect", input = {}, read = make()) {
    read.instrument(client);
    try {
      await client.invoke({ className: "users.GetUsers" }); // Required fresh identity check.
      return await readOperation(
        client,
        operation,
        { target: "@synthetic", ...input },
        read,
      );
    } finally {
      read.restore(client);
    }
  }
  return {
    data,
    store,
    client,
    messages,
    entity,
    make,
    run,
    calls: () => calls,
    advance: (seconds) => (now += seconds),
  };
}

test("durable identity-scoped read caches reduce repeated inspect requests while identity remains fresh", async () => {
  const f = fixture();
  const first = await f.run();
  const before = f.calls();
  const cached = await f.run();
  assert.equal(first.read_state, "fresh");
  assert.equal(cached.read_state, "cache_hit");
  assert.equal(f.calls() - before, 1);
  assert.equal(cached.rpc_counts.identity, 1);
  assert.equal(cached.read_rpc_network_attempts, null);
  assert.equal(cached.read_observed_at, first.read_observed_at);
  const changedRevision = await f.run(
    "inspect",
    {},
    new TelegramReadPolicy(f.store, {
      accountId: "a",
      identity: "account:one",
      revision: 2,
      clock: () => 1800000000,
    }),
  );
  assert.equal(changedRevision.read_state, "fresh");
});

test("partial multi-source samples resume from durable caches without inventing consumption cursors", async () => {
  const f = fixture({ policy: { messages_per_page: 2, scan_rpc_limit: 4 } });
  const first = await f.run();
  assert.equal(first.read_state, "incomplete");
  assert.equal(first.read_complete, false);
  assert.equal(first.read_cursor_next, null);
  const resumed = await f.run();
  assert.equal(resumed.read_complete, true);
  assert.deepEqual(
    resumed.recent.map((m) => m.id),
    [5, 4],
  );
  const states = [...f.data.values()].filter((v) => v?.committed !== undefined);
  assert.equal(states.length, 0);
});

test("account budget persists across restart and cache namespaces without becoming platform cooldown", async () => {
  const f = fixture({ policy: { requests_per_window: 1 } });
  const result = await f.run();
  assert.equal(result.read_state, "incomplete");
  assert.equal(result.cooldown_source, "local_read_budget");
  const read = f.make({ requests_per_window: 100 });
  read.instrument(f.client);
  await assert.rejects(
    f.client.invoke({ className: "users.GetUsers" }),
    /TelegramReadBudgetExceeded/,
  );
  read.restore(f.client);
});

test("membership and receipt checks never use optional read cache", async () => {
  const f = fixture();
  await f.run("membership_verify");
  const before = f.calls();
  await f.run("membership_verify");
  assert.ok(f.calls() - before >= 2);
  const noIds = await f.run("verify", { message_ids: [] });
  assert.equal(noIds.status, "unknown");
});

test("concurrent workers sharing the durable account budget cannot reserve the final request twice", async () => {
  const f = fixture({ policy: { requests_per_window: 1 } });
  const results = await Promise.allSettled([
    f.make().reserve("history"),
    f.make().reserve("history"),
  ]);
  assert.equal(results.filter((r) => r.status === "fulfilled").length, 1);
  assert.equal(f.data.get("read-budget:a").used, 1);
});

test("malformed samples and failed cache writes never create consumption cursors", async () => {
  const f = fixture();
  f.client.getMessages = async () => [{ id: null, date: 1800000000 }];
  const malformed = await f.run();
  assert.equal(malformed.read_complete, false);
  assert.equal(malformed.read_incomplete_reason, "TelegramReadMalformedPage");
  assert.ok(![...f.data.values()].some((v) => v?.committed > 0));
  const g = fixture(),
    put = g.store.put;
  g.store.put = async (key, value) => {
    if (key.includes('"sample"')) throw new Error("SyntheticStorageFailure");
    return put(key, value);
  };
  await assert.rejects(g.run(), /SyntheticStorageFailure/);
  assert.ok(![...g.data.values()].some((v) => v?.committed > 0));
});

test("same fixture before/after request comparison distinguishes cached samples from fresh invokes", async () => {
  const legacy = fixture();
  for (let i = 0; i < 2; i++) {
    await legacy.client.invoke({ className: "users.GetUsers" });
    await readOperation(legacy.client, "inspect", { target: "@synthetic" });
  }
  const optimized = fixture();
  await optimized.run();
  const cached = await optimized.run();
  assert.equal(legacy.calls(), 10);
  assert.equal(optimized.calls(), 6);
  assert.equal(cached.read_fresh_samples, 0);
  assert.equal(cached.read_state, "cache_hit");
  console.log(
    JSON.stringify({
      fixture: "two_identical_inspects",
      before_logical_invoke_attempts: legacy.calls(),
      after_logical_invoke_attempts: optimized.calls(),
      second_inspect_identity_attempts: cached.rpc_counts.identity,
      physical_rpc_count: null,
    }),
  );
});

test("zero cache TTL disables caches and policy changes cannot reset account budget", async () => {
  const f = fixture({ policy: { cache_ttl_seconds: 0 } });
  await f.run();
  const second = await f.run();
  assert.equal(second.read_state, "fresh");
  assert.equal(second.read_cache_hits, 0);
  const limited = fixture({
    policy: { requests_per_window: 1, window_seconds: 3600 },
  });
  await limited.make().reserve("identity");
  const changed = new TelegramReadPolicy(limited.store, {
    accountId: "a",
    identity: "new",
    revision: 99,
    clock: () => 1800000000,
    policy: { requests_per_window: 1000, window_seconds: 60 },
  });
  await assert.rejects(
    changed.reserve("identity"),
    /TelegramReadBudgetExceeded/,
  );
});

test("send-side reads share budget, exact send RPC remains once-only and accepted IDs stay unknown", async () => {
  const f = fixture({ policy: { requests_per_window: 1 } });
  const read = f.make();
  read.instrument(f.client);
  read.category = "read";
  const request = {
    job_id: "send:one",
    account_id: "a",
    version: "v",
    attempt_id: "once",
    lease_token: "l",
  };
  const rows = new Map();
  let sends = 0;
  const core = new ExecutorCore({
    store: {
      get: async (key) => rows.get(key),
      put: async (key, value) => rows.set(key, structuredClone(value)),
    },
    authorize: async () => ({
      allowed: true,
      lease_until: 1800000100,
      job: { id: request.job_id, account_id: "a", version: "v" },
    }),
    clock: () => 1800000000,
    classifyError: safeError,
    submit: async (job, checkpoint) => {
      await f.client.invoke({ className: "channels.GetParticipant" });
      await checkpoint({ submission_started: true });
      await f.client.invoke({ className: "messages.SendMessage" });
      sends++;
      await checkpoint({ message_ids: [77] });
      await f.client.invoke({ className: "messages.GetMessages" });
    },
  });
  const result = await core.execute(request);
  assert.equal(result.status, "unknown");
  assert.deepEqual(result.message_ids, [77]);
  assert.equal(result.cooldown_source, "local_read_budget");
  assert.equal(result.rate_limited, undefined);
  await core.execute(request);
  assert.equal(sends, 1);
  assert.equal(f.data.get("read-budget:a").used, 1);
  assert.equal(read.counts.mutation, 1);
  read.restore(f.client);
  const before = fixture({ policy: { requests_per_window: 1 } }),
    policy = before.make();
  await policy.reserve("identity");
  let error;
  try {
    await policy.reserve("membership");
  } catch (e) {
    error = e;
  }
  assert.equal(
    safeError(error, { submission_started: false }).confirmed_not_submitted,
    true,
  );
  assert.equal(
    safeError(error, { submission_started: false }).status,
    "failed_known",
  );
});

test("directory query and reply context caches are separate and resumed samples retain first observation time", async () => {
  const f = fixture();
  await f.run("discover", { query: "one" });
  const same = await f.run("discover", { query: "one" });
  const changed = await f.run("discover", { query: "two" });
  assert.equal(same.read_state, "cache_hit");
  assert.equal(changed.read_state, "fresh");
  await f.run("read_context", { message_id: 3 });
  const cached = await f.run("read_context", { message_id: 3 });
  assert.equal(cached.read_state, "cache_hit");
  assert.equal(cached.read_fresh_samples, 0);
  const partial = fixture({
    policy: { messages_per_page: 2, scan_rpc_limit: 4 },
  });
  const first = await partial.run();
  partial.advance(10);
  const resumed = await partial.run();
  assert.equal(resumed.read_complete, true);
  assert.equal(resumed.read_observed_at, first.read_observed_at);
  assert.equal(resumed.read_result_scope, "bounded_observation_sample");
});

test("malformed ordered samples are incomplete and never create a cursor", async () => {
  const f = fixture();
  f.client.getMessages = async () => [
    { id: 1, date: 1800000000 },
    { id: 5, date: 1800000000 },
  ];
  const result = await f.run();
  assert.equal(result.read_complete, false);
  assert.equal(result.read_incomplete_reason, "TelegramReadMalformedPage");
  assert.ok(![...f.data.values()].some((v) => v?.committed > 0));
});

test("cold reads stay bounded and TTL refresh sees newly pinned old IDs without walking history", async () => {
  const f = fixture();
  f.messages.splice(
    0,
    f.messages.length,
    ...Array.from({ length: 1000 }, (_, i) => ({
      id: 1000 - i,
      message: "sample",
      date: 1800000000,
    })),
  );
  let pinned = 900;
  const messages = f.client.getMessages;
  f.client.getMessages = async (entity, options) => {
    if (options.filter) {
      await f.client.invoke({ className: "messages.Search" });
      return [{ id: pinned, message: "rules", date: 1800000000 }];
    }
    return messages(entity, options);
  };
  const first = await f.run();
  assert.equal(f.calls(), 5);
  assert.equal(first.recent.length, 100);
  assert.equal(first.read_sample_truncated, true);
  assert.equal(first.read_cursor_scope, "bounded_sample_no_consumption_cursor");
  assert.equal(first.read_cursor_next, null);
  pinned = 1;
  f.advance(301);
  const refreshed = await f.run();
  assert.equal(refreshed.pinned[0].id, 1);
  assert.equal(f.calls(), 10);
  assert.ok(
    ![...f.data.keys()].some(
      (key) => key.includes('"page"') || key.includes('"scan"'),
    ),
  );
});

test("parallel SDK invokes cannot exceed the per-operation cap while durable reservations await", async () => {
  const f = fixture({ policy: { scan_rpc_limit: 1, requests_per_window: 10 } });
  const read = f.make();
  read.instrument(f.client);
  try {
    const result = await Promise.allSettled([
      f.client.invoke({ className: "users.GetUsers" }),
      f.client.invoke({ className: "users.GetUsers" }),
    ]);
    assert.equal(result.filter((r) => r.status === "fulfilled").length, 1);
    assert.equal(f.calls(), 1);
    assert.equal(f.data.get("read-budget:a").used, 1);
  } finally {
    read.restore(f.client);
  }
});

test("oversized bounded samples stay fresh without exceeding durable cache value limits", async () => {
  const f = fixture();
  f.messages.splice(
    0,
    f.messages.length,
    ...Array.from({ length: 100 }, (_, i) => ({
      id: 100 - i,
      message: "x".repeat(4000),
      date: 1800000000,
    })),
  );
  const put = f.store.put;
  f.store.put = async (key, value) => {
    assert.ok(new TextEncoder().encode(JSON.stringify(value)).length < 128000);
    return put(key, value);
  };
  const result = await f.run();
  assert.equal(result.read_complete, true);
  assert.equal(result.read_cache_skipped_oversize, 1);
  assert.equal(result.recent.length, 100);
  const again = await f.run();
  assert.equal(again.read_state, "fresh");
  assert.equal(again.read_cache_skipped_oversize, 1);
});
