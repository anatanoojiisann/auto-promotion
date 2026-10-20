import assert from "node:assert/strict";
import test from "node:test";
import { Api } from "teleproto";
import { keepQrLoginAlive } from "../qr-login-flow.mjs";

function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
async function flush() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
}
function clock() {
  let now = 0,
    serial = 0;
  const timers = new Map();
  return {
    now: () => now,
    timer: (fn, ms) => {
      const id = ++serial;
      timers.set(id, { fn, at: now + ms });
      return id;
    },
    cancel: (id) => timers.delete(id),
    async advance(ms) {
      const target = now + ms;
      for (let n = 0; n < 100; n++) {
        const next = [...timers]
          .filter(([, t]) => t.at <= target)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        timers.delete(next[0]);
        now = next[1].at;
        void next[1].fn();
        await flush();
      }
      now = target;
      await flush();
    },
  };
}
function fixture({
  invoke,
  switchDC = async () => {},
  authorized = async () => {},
} = {}) {
  const time = clock(),
    events = [];
  let emit;
  const client = {
    session: { dcId: 2 },
    addEventHandler: (fn) => {
      emit = fn;
    },
    removeEventHandler: () => {},
    invoke:
      invoke ||
      (async () =>
        new Api.auth.LoginTokenSuccess({
          authorization: new Api.auth.Authorization({
            user: new Api.UserEmpty({ id: 1 }),
          }),
        })),
    _switchDC: switchDC,
  };
  const done = keepQrLoginAlive(
    client,
    { TG_API_ID: 12345, TG_API_HASH: "synthetic-only" },
    {
      status: "waiting_device_confirmation",
      expires_at: new Date(30000).toISOString(),
    },
    {
      authorized,
      expired: async () => events.push("expired"),
      failed: async (error) => events.push(error.message),
      close: async () => events.push("closed"),
      pending: async () => events.push("pending"),
      clock: time.now,
      timer: time.timer,
      cancelTimer: time.cancel,
    },
  );
  return { time, events, done, update: () => emit(new Api.UpdateLoginToken()) };
}

test("confirmation deadline includes getMe and durable-session finalization", async () => {
  const gate = deferred();
  const f = fixture({ authorized: () => gate.promise });
  const update = f.update();
  await flush();
  try {
    await f.time.advance(20000);
    assert.ok(f.events.includes("TelegramQrConfirmationTimedOut"));
  } finally {
    gate.resolve();
    await update;
    await f.done;
  }
});
test("expiry during an active confirmation is confirmation timeout, not unconfirmed expiry", async () => {
  const gate = deferred();
  const f = fixture({ invoke: () => gate.promise });
  const update = f.update();
  await flush();
  try {
    await f.time.advance(20000);
    assert.ok(f.events.includes("TelegramQrConfirmationTimedOut"));
  } finally {
    gate.resolve(
      new Api.auth.LoginToken({
        token: Buffer.from("synthetic-only"),
        expires: 60,
      }),
    );
    await update;
    await f.done;
  }
});
test("new pending token expiry cannot be cut off by the old QR timer", async () => {
  const f = fixture({
    invoke: async () =>
      new Api.auth.LoginToken({
        token: Buffer.from("synthetic-only"),
        expires: 60,
      }),
  });
  await f.time.advance(30000);
  assert.equal(f.events.includes("expired"), false);
  await f.time.advance(30000);
  await f.done;
  assert.ok(f.events.includes("expired"));
});

test("DC migration and concurrent update/poll preserve one finalization", async () => {
  let dc = null,
    exports = 0,
    imports = 0,
    authorized = 0;
  const gate = deferred();
  const f = fixture({
    switchDC: async (value) => {
      dc = value;
    },
    authorized: async () => {
      authorized++;
    },
    invoke: async (request) => {
      if (request instanceof Api.auth.ExportLoginToken) {
        exports++;
        await gate.promise;
        return new Api.auth.LoginTokenMigrateTo({
          dcId: 4,
          token: Buffer.from("synthetic-migration"),
        });
      }
      assert.ok(request instanceof Api.auth.ImportLoginToken);
      imports++;
      return new Api.auth.LoginTokenSuccess({
        authorization: new Api.auth.Authorization({
          user: new Api.UserEmpty({ id: 1 }),
        }),
      });
    },
  });
  await f.time.advance(5000);
  const update = f.update();
  await flush();
  gate.resolve();
  await update;
  await f.done;
  assert.equal(dc, 4);
  assert.equal(exports, 1);
  assert.equal(imports, 1);
  assert.equal(authorized, 1);
});

test("refreshed tokens cannot extend the entire flow beyond its bounded lifetime", async () => {
  const f = fixture({
    invoke: async () =>
      new Api.auth.LoginToken({
        token: Buffer.from("synthetic-only"),
        expires: 1000,
      }),
  });
  await f.time.advance(120000);
  await f.done;
  assert.equal(f.events.filter((event) => event === "expired").length, 1);
  assert.equal(f.events.filter((event) => event === "closed").length, 1);
});
