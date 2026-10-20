import assert from "node:assert/strict";
import test from "node:test";
import {
  LoginDiagnostics,
  requestId,
  safeLoginError,
} from "../qr-login-diagnostics.mjs";

const flowId = "00000000-0000-4000-8000-000000000001";
test("diagnostics whitelist keys and values rather than serializing secrets or errors", () => {
  const canaries = [
    "token-CANARY",
    "session-CANARY",
    "123456-code-CANARY",
    "+15555550123",
    "api_hash-CANARY",
    "key-CANARY",
    "https://secret.invalid/?token=CANARY",
  ];
  const error = new Error(canaries.join(" "));
  error.name = canaries[0];
  error.code = canaries[1];
  error.stack = canaries[2];
  error.cause = { code: "ECONNRESET", message: canaries[3] };
  error.cause.cause = error;
  const entries = [];
  const diagnostics = new LoginDiagnostics({
    flowId,
    request: canaries[4],
    clock: () => 0,
    sink: (e) => entries.push(e),
  });
  diagnostics.emit("stage_failed", {
    phase: "restore_identity",
    error,
    reason: canaries[5],
    token: canaries[0],
    session: canaries[1],
    phone: canaries[3],
    url: canaries[6],
  });
  diagnostics.emit("stage_started", {
    phase: canaries[0],
    source: canaries[1],
    request_id: canaries[2],
  });
  const encoded = JSON.stringify([entries, diagnostics.snapshot()]);
  for (const canary of canaries)
    assert.equal(encoded.includes(canary), false, canary);
  assert.equal(entries[0].reason, "TelegramExecutorError");
  assert.equal(entries[0].cause, "connection_reset");
  assert.equal(entries[0].exception_type, "OtherError");
  assert.equal(entries[1].phase, undefined);
  assert.equal(entries[1].source, undefined);
  assert.match(requestId("bad\nsecret"), /^[a-f0-9-]{36}$/);
  assert.equal(requestId(flowId), flowId);
  assert.deepEqual(
    safeLoginError({
      get cause() {
        throw new Error("secret");
      },
    }),
    {
      reason: "TelegramExecutorError",
      exception_type: "OtherError",
      cause: "unknown",
    },
  );
});

test("bounded history aggregates noisy events and preserves the first error and terminal evidence", () => {
  const entries = [];
  let now = 0;
  const diagnostics = new LoginDiagnostics({
    flowId,
    request: flowId,
    clock: () => now++,
    sink: (e) => entries.push(e),
  });
  diagnostics.emit("flow_started");
  const error = Object.assign(new Error("do-not-log"), { code: "ETIMEDOUT" });
  diagnostics.emit("stage_failed", { phase: "import_login_token", error });
  for (let i = 0; i < 400; i++)
    diagnostics.emit("request_seen", { source: "poll" });
  for (let i = 0; i < 90; i++)
    diagnostics.emit("storage_failed", {
      phase: "save_pending_session",
      error,
    });
  diagnostics.emit("flow_terminal", { status: "login_failed", error });
  const before = diagnostics.snapshot();
  assert.ok(before.events.length <= 64);
  assert.ok(entries.length <= 96);
  assert.equal(before.first_error.phase, "import_login_token");
  assert.equal(before.events.at(-1).event, "flow_terminal");
  for (let i = 0; i < 200; i++)
    diagnostics.emit("request_seen", { source: "poll" });
  assert.deepEqual(
    diagnostics.snapshot(),
    before,
    "terminal evidence is immutable under polls",
  );
  assert.ok(entries.every((entry) => JSON.stringify(entry).length < 768));
  assert.ok(
    entries.some((entry) => entry.event === "flow_terminal"),
    "sink reserves terminal capacity despite noise",
  );
});

test("stage and timer evidence retain per-generation deadlines and durations", () => {
  const d = new LoginDiagnostics({
    flowId,
    request: flowId,
    clock: () => 0,
    sink: () => {},
  });
  for (const generation of [1, 2])
    for (const qr_generation of [1, 2]) {
      d.emit("stage_started", {
        phase: "export_login_token",
        generation,
        qr_generation,
        remaining_ms: 100 * qr_generation,
      });
      d.emit("stage_completed", {
        phase: "export_login_token",
        generation,
        qr_generation,
        elapsed_ms: 10 * qr_generation,
      });
      d.emit("qr_refreshed", {
        generation,
        qr_generation,
        deadline_ms: 1000 * qr_generation,
      });
    }
  const events = d.snapshot().events;
  assert.equal(events.length, 12);
  assert.deepEqual(
    events
      .filter((e) => e.event === "stage_completed")
      .map((e) => [e.generation, e.qr_generation, e.elapsed_ms]),
    [
      [1, 1, 10],
      [1, 2, 20],
      [2, 1, 10],
      [2, 2, 20],
    ],
  );
});

test("throwing or rejecting diagnostic sinks and cyclic error causes never escape", async () => {
  const error = new Error("CANARY");
  error.cause = error;
  for (const sink of [
    () => {
      throw error;
    },
    () => Promise.reject(error),
  ]) {
    const diagnostics = new LoginDiagnostics({
      flowId,
      request: flowId,
      sink,
      clock: () => 0,
    });
    assert.doesNotThrow(() =>
      diagnostics.emit("stage_failed", { phase: "verify_identity", error }),
    );
    assert.equal(diagnostics.snapshot().first_error.cause, "unknown");
  }
  await Promise.resolve();
});
