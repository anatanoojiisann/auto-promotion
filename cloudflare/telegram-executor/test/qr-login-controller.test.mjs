import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { Api } from "teleproto";
import { QrLoginController } from "../qr-login-controller.mjs";
import { requestId } from "../qr-login-diagnostics.mjs";
import { preExecutionDeferral, ExecutorCore } from "../executor-core.mjs";

const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
};
async function flush() {
  for (let i = 0; i < 150; i++) await Promise.resolve();
}
function fixture() {
  let now = 0,
    serial = 0,
    rows = new Map(),
    tail = Promise.resolve(),
    emit;
  const timers = new Map(),
    clients = [],
    promises = [];
  const f = {
    logs: [],
    logError: null,
    finalGate: null,
    restoredGate: null,
    closeGate: null,
    sealError: null,
    saveError: null,
    restoreError: null,
    invokeGate: null,
  };
  const storage = {
    get: async (key) => {
      await tail;
      return structuredClone(rows.get(key));
    },
    transaction(fn) {
      const result = tail.then(async () => {
        const copy = new Map(structuredClone([...rows]));
        const value = await fn({
          get: async (key) => structuredClone(copy.get(key)),
          put: async (packet) => {
            if (packet.qr_pending_session && f.saveError) throw f.saveError;
            if (packet.last_error && f.terminalGate)
              await f.terminalGate.promise;
            if (packet.cloud_session && f.finalGate) {
              f.finalWriteStarted = true;
              await f.finalGate.promise;
            }
            for (const [key, value] of Object.entries(packet))
              copy.set(key, structuredClone(value));
          },
        });
        rows = copy;
        return value;
      });
      tail = result.catch(() => {});
      return result;
    },
  };
  function createClient(session) {
    const index = clients.length;
    const client = {
      input: session,
      destroyed: false,
      session: { dcId: 2, save: () => "synthetic-session" },
      connect: async () => {},
      destroy: async () => {
        client.destroyed = true;
        if (f.closeGate) await f.closeGate.promise;
      },
      getMe: async () => {
        if (index > 0 && f.restoredGate) {
          f.restoreIdentityStarted = true;
          await f.restoredGate.promise;
        }
        if (index > 0 && f.restoreError) throw f.restoreError;
        return { id: 42 };
      },
      addEventHandler: (fn) => {
        emit = fn;
      },
      removeEventHandler: () => {},
      invoke: async () => {
        if (f.invokeError) throw f.invokeError;
        return f.invokeGate
          ? f.invokeGate.promise
          : !session && !f.accepted
            ? new Api.auth.LoginToken({
                token: Buffer.from("synthetic-only"),
                expires: f.fixedExpiry ?? Math.ceil(now / 1000) + 30,
              })
            : new Api.auth.LoginTokenSuccess({
                authorization: new Api.auth.Authorization({
                  user: new Api.UserEmpty({ id: 42 }),
                }),
              });
      },
    };
    clients.push(client);
    return client;
  }
  const options = {
    store: storage,
    env: {
      TG_API_ID: 12345,
      TG_API_HASH: "synthetic-only",
      TG_EXPECTED_ID: "42",
    },
    createClient,
    seal: async (session) => {
      if (f.sealError) throw f.sealError;
      return { encrypted: "synthetic-ciphertext", length: session.length };
    },
    unseal: async (sealed) => {
      assert.equal(sealed.encrypted, "synthetic-ciphertext");
      return "synthetic-restored-session";
    },
    waitUntil: (promise) => promises.push(promise),
    clock: () => now,
    diagnosticSink: (entry) => {
      f.logs.push(entry);
      if (f.logError) throw f.logError;
    },
    timer: (fn, ms) => {
      const id = ++serial;
      timers.set(id, { fn, at: now + ms });
      return id;
    },
    cancelTimer: (id) => timers.delete(id),
  };
  const controller = new QrLoginController(options);
  return Object.assign(f, {
    controller,
    options,
    clients,
    storage,
    promises,
    newController: () => new QrLoginController(options),
    rows: () => rows,
    update: () => {
      f.accepted = true;
      return emit(new Api.UpdateLoginToken());
    },
    async until(predicate) {
      for (let i = 0; i < 2000 && !predicate(); i++) await Promise.resolve();
      assert.ok(
        predicate(),
        "expected fake RPC/storage gate must be entered before advancing time",
      );
    },
    async advance(ms) {
      const target = now + ms;
      for (let i = 0; i < 100; i++) {
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
  });
}

test("authorized requires a different client restored from persisted ciphertext", async () => {
  const f = fixture();
  const first = await f.controller.begin();
  assert.equal(first.status, "waiting_device_confirmation");
  await f.update();
  await Promise.all(f.promises);
  const status = await f.controller.view();
  assert.equal(status.status, "authorized");
  assert.equal(status.flow_id, first.flow_id);
  assert.equal(f.clients.length, 2);
  assert.equal(f.clients[1].input, "synthetic-restored-session");
  assert.ok(status.last_verified_at);
  assert.equal(status.reader_started, false);
  assert.equal(f.rows().get("login_flow").phase, "authorized_session_saved");
  assert.equal(f.rows().get("qr_pending_session"), null);
  assert.ok(f.clients.every((c) => c.destroyed));
});
test("slow restored getMe times out without claiming authorization or a late promotion", async () => {
  const f = fixture();
  f.restoredGate = deferred();
  await f.controller.begin();
  const update = f.update();
  await f.until(() => f.restoreIdentityStarted);
  await f.advance(20000);
  assert.equal(f.rows().get("login_state"), "confirmation_timed_out");
  assert.equal(f.rows().has("cloud_session"), false);
  f.restoredGate.resolve();
  await update;
  await Promise.all(f.promises);
  assert.equal((await f.controller.view()).status, "confirmation_timed_out");
  assert.equal(f.rows().has("cloud_session"), false);
});
test("a final storage write completing after timeout rolls back instead of publishing authorized", async () => {
  const f = fixture();
  f.finalGate = deferred();
  await f.controller.begin();
  const update = f.update();
  await f.until(() => f.finalWriteStarted);
  await f.advance(20000);
  f.finalGate.resolve();
  await update;
  await Promise.all(f.promises);
  assert.equal((await f.controller.view()).status, "confirmation_timed_out");
  assert.equal(f.rows().has("cloud_session"), false);
  assert.equal(f.rows().get("qr_pending_session"), null);
});

test("old final write and cleanup cannot overwrite or unlock a replacement flow", async () => {
  const f = fixture();
  f.finalGate = deferred();
  const first = await f.controller.begin();
  const oldUpdate = f.update();
  await f.until(() => f.finalWriteStarted);
  await f.advance(20000);
  f.accepted = false;
  const replacement = f.controller.begin({ replace_flow_id: first.flow_id });
  await flush();
  f.finalGate.resolve();
  const next = await replacement;
  await oldUpdate;
  await flush();
  assert.notEqual(next.flow_id, first.flow_id);
  assert.equal(next.generation, first.generation + 1);
  assert.equal(next.status, "waiting_device_confirmation");
  assert.equal(f.controller.active, true);
  assert.equal(f.rows().has("cloud_session"), false);
  const stale = await f.controller.view({
    flow_id: first.flow_id,
    generation: first.generation,
  });
  assert.equal(stale.status, "flow_superseded");
  assert.equal(stale.qr_uri, undefined);
  await f.controller.current.run.cancel();
});

test("a new isolate reports lost pending connection without opening another QR", async () => {
  const f = fixture();
  const first = await f.controller.begin();
  await f.controller.current.run.cancel();
  const before = f.clients.length;
  const storedBefore = structuredClone(f.rows());
  const restarted = f.newController();
  const status = await restarted.view({
    flow_id: first.flow_id,
    generation: first.generation,
  });
  assert.equal(status.status, "login_interrupted");
  assert.equal(status.login_diagnostics.reason, "TelegramQrConnectionLost");
  assert.equal(status.qr_uri, undefined);
  assert.equal((await restarted.view()).status, "login_interrupted");
  assert.equal(f.clients.length, before);
  assert.deepEqual(
    f.rows(),
    storedBefore,
    "poll must not persist derived interruption or clear pending state",
  );
});

test("legacy authorized state without restore proof has no fabricated verification time", async () => {
  const f = fixture();
  await f.storage.transaction((tx) => tx.put({ login_state: "authorized" }));
  const status = await f.controller.view();
  assert.equal(status.status, "verification_required");
  assert.equal(status.last_verified_at, null);
  assert.equal(f.clients.length, 0);
});

test("encryption persistence and restored-auth errors retain their exact safe phase", async () => {
  for (const [field, phase] of [
    ["sealError", "encrypt_session"],
    ["saveError", "save_pending_session"],
    ["restoreError", "restore_identity"],
  ]) {
    const f = fixture();
    const error = new Error("sensitive-data-must-not-be-exported");
    error.name = "SyntheticStorageError";
    f[field] = error;
    await f.controller.begin();
    await f.update();
    await Promise.all(f.promises);
    const status = await f.controller.view();
    assert.equal(status.status, "login_failed");
    assert.equal(status.login_diagnostics.phase, phase);
    assert.equal(f.rows().has("cloud_session"), false);
    const encoded = JSON.stringify(status);
    for (const secret of [
      "sensitive-data",
      "synthetic-session",
      "synthetic-ciphertext",
      "TG_API_HASH",
    ])
      assert.equal(encoded.includes(secret), false);
    const clients = f.clients.length;
    await f.controller.view();
    assert.equal(f.clients.length, clients);
  }
});

test("cloud confirmation signal does not claim phone behavior and cleanup is bounded", async () => {
  const f = fixture();
  f.closeGate = deferred();
  f.fixedExpiry = 30;
  const first = await f.controller.begin();
  await f.advance(30000);
  await f.advance(4000);
  const status = await f.controller.view();
  assert.equal(status.status, "login_expired");
  assert.equal(status.login_diagnostics.cloud_confirmation_observed, false);
  assert.equal(
    status.login_diagnostics.reason,
    "TelegramQrExpiredWithoutConfirmation",
  );
  assert.equal(f.controller.active, false);
  f.accepted = false;
  f.fixedExpiry = null;
  f.closeGate.resolve();
  const next = await f.controller.begin();
  assert.notEqual(first.flow_id, next.flow_id);
  assert.equal(next.status, "waiting_device_confirmation");
  await f.controller.current.run.cancel();
});

test("a hanging token RPC has an RPC timeout classification, not an expiry claim", async () => {
  const f = fixture();
  await f.controller.begin();
  f.invokeGate = deferred();
  await f.advance(30000);
  await flush();
  const status = await f.controller.view();
  assert.equal(status.status, "login_failed");
  assert.equal(status.login_diagnostics.phase, "export_login_token");
  assert.equal(status.login_diagnostics.reason, "TelegramQrTokenCheckTimedOut");
  f.invokeGate.resolve(
    new Api.auth.LoginToken({
      token: Buffer.from("synthetic-only"),
      expires: 60,
    }),
  );
  await Promise.all(f.promises);
  assert.equal((await f.controller.view()).status, "login_failed");
});

test("terminal storage failure cannot keep the owned flow busy forever", async () => {
  const f = fixture();
  f.fixedExpiry = 30;
  f.terminalGate = deferred();
  await f.controller.begin();
  const flow = f.controller.current;
  await f.advance(34000);
  assert.equal(f.controller.active, false);
  const status = await f.controller.view();
  assert.equal(status.status, "login_expired");
  assert.equal(status.login_diagnostics.persistence_verified, false);
  f.terminalGate.resolve();
  await flow.run;
  assert.equal((await f.controller.view()).status, "login_expired");
});

test("actual Worker routes fence native/read clients during QR login while polls stay read-only", async () => {
  const f = fixture();
  let platformCalls = 0;
  // Run audited Worker methods with an injected DO base and fake transport.
  const source = (
    await readFile(new URL("../worker.mjs", import.meta.url), "utf8")
  )
    .replace(/^import[\s\S]*?from "[^\"]+";\n/gm, "")
    .replace("export class TelegramExecutor", "class TelegramExecutor")
    .replace("export default {", "const worker = {");
  const unsupported = () => {
    platformCalls++;
    throw new Error("UnexpectedPlatformCall");
  };
  const Worker = new vm.Script(source + "\nTelegramExecutor;").runInNewContext({
    DurableObject: class {
      constructor(ctx, env) {
        this.ctx = ctx;
        this.env = env;
      }
    },
    QrLoginController: class extends QrLoginController {
      constructor(options) {
        super({
          ...options,
          clock: f.options.clock,
          timer: f.options.timer,
          cancelTimer: f.options.cancelTimer,
          diagnosticSink: f.options.diagnosticSink,
        });
      }
    },
    createClient: f.options.createClient,
    ExecutorCore,
    preExecutionDeferral,
    readOperation: unsupported,
    submitOperation: unsupported,
    authorizeNative: unsupported,
    safeError: unsupported,
    LIBRARY_VERSION: "synthetic-only",
    Response,
    Buffer,
    crypto,
    requestId,
    Date,
  });
  const worker = new Worker(
    { storage: f.storage, waitUntil: (p) => f.promises.push(p) },
    { ...f.options.env, TG_SESSION_KEK: "00".repeat(32) },
  );
  const request = (operation) =>
    worker.fetch({
      json: async () => ({ operation, account_id: "synthetic-account" }),
      headers: new Headers({
        "X-AP-Request-ID": "00000000-0000-4000-8000-000000000001",
      }),
    });
  try {
    const first = await (await request("login_begin")).json();
    assert.equal(first.status, "waiting_device_confirmation");
    assert.equal(first.request_id, "00000000-0000-4000-8000-000000000001");
    const stored = structuredClone(f.rows());
    const clients = f.clients.length;
    for (const operation of ["health", "execute", "discover"]) {
      const response = await request(operation);
      assert.equal(response.status, 409);
      assert.equal((await response.json()).status, "waiting_account_lock");
    }
    assert.equal(f.clients.length, clients);
    assert.equal(platformCalls, 0);
    assert.equal(
      (await (await request("status")).json()).login_state,
      "waiting_device_confirmation",
    );
    const poll = await (await request("login_poll")).json();
    assert.equal(poll.flow_id, first.flow_id);
    assert.equal(poll.generation, first.generation);
    assert.deepEqual(
      f.rows(),
      stored,
      "status/poll and lock rejection cannot change durable state",
    );
  } finally {
    await worker.login.current?.run?.cancel();
  }
  const configuredUntil = Date.now() / 1000 + 3600;
  worker.env.TG_ACCOUNT_NOT_BEFORE = String(configuredUntil);
  const clients = f.clients.length;
  for (const operation of ["health", "execute", "discover"]) {
    const response = await request(operation);
    assert.equal(response.status, 409);
    const outcome = await response.json();
    assert.equal(outcome.status, "waiting_cooldown");
    assert.equal(outcome.reason, "AccountCooldown");
    assert.equal(outcome.retry_at, configuredUntil);
    if (operation === "execute") {
      assert.equal(outcome.submission_started, false);
      assert.equal(outcome.native_authorized, false);
    }
  }
  assert.equal((await request("status")).status, 200);
  assert.equal(
    f.clients.length,
    clients,
    "read/write cooldown does not create a platform client",
  );
  try {
    const login = await request("login_begin");
    assert.equal(
      login.status,
      200,
      "historical message limits do not block independent device authorization",
    );
    assert.equal((await login.json()).status, "waiting_device_confirmation");
    const count = f.clients.length;
    assert.equal((await request("login_poll")).status, 200);
    assert.equal(f.clients.length, count, "polls remain metadata-only");
  } finally {
    await worker.login.current?.run?.cancel();
  }
  const afterLogin = f.clients.length;
  const loginUntil = configuredUntil + 1800;
  await f.storage.transaction((tx) =>
    tx.put({ login_cooldown_until: loginUntil }),
  );
  const loginBlocked = await request("login_begin");
  assert.equal(loginBlocked.status, 409);
  assert.equal((await loginBlocked.json()).reason, "TelegramLoginCooldown");
  assert.equal((await request("login_poll")).status, 200);
  const storedUntil = configuredUntil + 7200;
  await f.storage.transaction((tx) => tx.put({ cooldown_until: storedUntil }));
  assert.equal((await (await request("health")).json()).retry_at, storedUntil);
  worker.env.TG_ACCOUNT_NOT_BEFORE = "invalid";
  const invalid = await request("health");
  assert.equal(invalid.status, 409);
  assert.equal((await invalid.json()).reason, "InvalidAccountCooldown");
  assert.equal(
    f.clients.length,
    afterLogin,
    "login-specific cooldown/configuration rejection creates no SDK client",
  );
  assert.equal(platformCalls, 0);
});

test("a platform limit during device authorization persists its own deadline without clearing message limits", async () => {
  const f = fixture();
  const error = new Error("synthetic rate limit");
  error.name = "FloodWaitError";
  error.errorMessage = "FLOOD_WAIT_120";
  error.seconds = 120;
  f.invokeError = error;
  await f.storage.transaction((tx) => tx.put({ cooldown_until: 5000 }));
  const result = await f.controller.begin();
  assert.equal(result.status, "login_failed");
  assert.equal(result.login_diagnostics.reason, "TelegramCooldown");
  assert.equal(f.rows().get("login_cooldown_until"), 120);
  assert.equal(f.rows().get("cooldown_until"), 5000);
  assert.equal(
    (await f.newController().view()).login_diagnostics.retry_at,
    120,
  );
});

test("logging failures do not alter successful authorization and terminal trace survives repeated polls", async () => {
  const f = fixture();
  f.logError = new Error("session-CANARY");
  const first = await f.controller.begin({
    request_id: "00000000-0000-4000-8000-000000000001",
  });
  await f.update();
  await Promise.all(f.promises);
  const status = await f.controller.view();
  assert.equal(status.status, "authorized");
  const trace = structuredClone(status.diagnostic_trace);
  for (let i = 0; i < 10; i++)
    assert.deepEqual((await f.controller.view()).diagnostic_trace, trace);
  assert.equal(trace.events.at(-1).event, "flow_terminal");
  for (const phase of [
    "verify_identity",
    "encrypt_session",
    "save_pending_session",
    "restore_connect",
    "restore_identity",
  ]) {
    const start = f.logs.find(
      (e) => e.event === "stage_started" && e.phase === phase,
    );
    const complete = f.logs.find(
      (e) => e.event === "stage_completed" && e.phase === phase,
    );
    assert.ok(start);
    assert.ok(complete);
    assert.ok(start.sequence < complete.sequence);
    assert.ok(complete.elapsed_ms >= 0);
    assert.equal(start.flow_id, first.flow_id);
  }
  assert.ok(f.rows().get("cloud_session"));
  assert.equal(
    JSON.stringify([f.logs, status]).includes("session-CANARY"),
    false,
  );
});

test("timeout trace keeps the active restore phase and confirmation timer source", async () => {
  const f = fixture();
  f.restoredGate = deferred();
  await f.controller.begin();
  const update = f.update();
  await f.until(() => f.restoreIdentityStarted);
  await f.advance(20000);
  const status = await f.controller.view();
  assert.equal(status.status, "confirmation_timed_out");
  assert.ok(
    f.logs.some(
      (e) =>
        e.event === "timeout_fired" &&
        e.source === "confirmation" &&
        e.phase === "restore_identity",
    ),
  );
  assert.ok(status.diagnostic_trace.first_error);
  assert.equal(status.diagnostic_trace.events.at(-1).event, "flow_terminal");
  const trace = structuredClone(status.diagnostic_trace);
  f.restoredGate.resolve();
  await update;
  await Promise.all(f.promises);
  assert.deepEqual((await f.controller.view()).diagnostic_trace, trace);
  const restarted = await f.newController().view();
  assert.equal(restarted.status, "confirmation_timed_out");
  assert.ok(
    restarted.diagnostic_trace.events.some(
      (e) => e.event === "timeout_fired" && e.source === "confirmation",
    ),
  );
  assert.equal(restarted.diagnostic_trace.events.at(-1).event, "flow_terminal");
});

test("authorized durable trace retains verification timing and atomic promotion completion after reconstruction", async () => {
  const f = fixture();
  await f.controller.begin();
  await f.update();
  await Promise.all(f.promises);
  const restarted = await f.newController().view();
  const events = restarted.diagnostic_trace.events;
  assert.equal(events.at(-1).event, "flow_terminal");
  assert.equal(events.at(-1).status, "authorized");
  for (const phase of [
    "verify_identity",
    "encrypt_session",
    "save_pending_session",
    "restore_identity",
  ]) {
    const start = events.find(
      (e) => e.event === "stage_started" && e.phase === phase,
    );
    const complete = events.find(
      (e) => e.event === "stage_completed" && e.phase === phase,
    );
    assert.ok(start);
    assert.ok(complete);
    assert.ok(complete.elapsed_ms >= 0);
    assert.ok(start.sequence < complete.sequence);
  }
  const promotion = events.filter(
    (e) => e.phase === "authorized_session_saved",
  );
  assert.deepEqual(
    promotion.map((e) => e.event),
    ["stage_started", "stage_completed"],
  );
  assert.equal(promotion[1].source, "storage");
});
