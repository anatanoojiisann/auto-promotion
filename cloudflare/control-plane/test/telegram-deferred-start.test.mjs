import assert from "node:assert/strict";
import test from "node:test";
import { fixture } from "./prepared-replies-fixture.mjs";

test("saved login queues readers and ownership activation until cooldown without platform API calls", async () => {
  const f = await fixture({ cooldown: 1200 });
  const expected = f.now() + 1200;
  const reader = await f.reader.start(
    f.id,
    "explicit_user_start_after_device_confirmation",
  );
  assert.equal(reader.started, true);
  assert.equal(reader.not_before, expected);
  assert.equal(f.reader.snapshot(f.id).state, "waiting_cooldown");
  const activation = await f.gate.activate({
    plan_id: f.id,
    source: "explicit_user_start_after_device_confirmation",
  });
  assert.equal(activation.queued, true);
  assert.equal(activation.activated, false);
  assert.equal(f.k.accountChannel("a"), null);
  await f.reader.tick();
  await f.gate.tick();
  assert.deepEqual(f.requests, ["status", "status"]);
  assert.equal(f.writes.length, 0);
  f.advance(1201);
  await f.gate.tick();
  assert.equal(f.gate.activationSnapshot(f.id).state, "active");
  assert.equal(f.k.accountChannel("a").node_id, "cloudflare-telegram");
  assert.equal(f.requests.at(-1), "health");
  assert.equal(
    f.k.one("SELECT cooldown_until FROM accounts WHERE id='a'").cooldown_until,
    expected,
  );
});

test("phone confirmation without durable identity proof cannot queue a deferred start", async () => {
  const f = await fixture({ cooldown: 1200 });
  f.env.TELEGRAM_EXECUTOR.fetch = async () =>
    Response.json({
      account_id: "a",
      login_state: "authorized",
      last_verified_at: new Date(f.now() * 1000).toISOString(),
      connection_status: "last_identity_verified",
      login_diagnostics: { persistence_verified: false },
    });
  await assert.rejects(
    f.reader.start(f.id, "explicit_user_start"),
    /ExecutorLoginNotVerified/,
  );
  await assert.rejects(
    f.gate.activate({ plan_id: f.id, source: "explicit_user_start" }),
    /ExecutorLoginNotVerified/,
  );
  assert.equal(f.reader.snapshot(f.id), null);
  assert.equal(f.gate.activationSnapshot(f.id), null);
});

test("deferred activation respects global stop, plan pause and a longer platform wait", async () => {
  const f = await fixture({ cooldown: 1200 });
  await f.gate.activate({ plan_id: f.id, source: "explicit_user_start" });
  f.k.configure({ global_stop: true });
  f.advance(1201);
  await f.gate.tick();
  assert.equal(f.requests.includes("health"), false);
  f.k.configure({ global_stop: false });
  f.q.changeState(f.id, "paused", "explicit_user_pause");
  await f.gate.tick();
  assert.equal(f.requests.includes("health"), false);
  f.q.changeState(f.id, "active", "explicit_user_resume");
  const until = f.now() + 1800;
  f.k.sql.exec("UPDATE accounts SET cooldown_until=? WHERE id='a'", until);
  await f.gate.tick();
  assert.equal(f.gate.activationSnapshot(f.id).next_at, until);
  assert.equal(f.requests.includes("health"), false);
  f.advance(1801);
  await f.gate.tick();
  assert.equal(f.gate.activationSnapshot(f.id).state, "active");
});

test("deferred activation cannot seize an account with another pending campaign", async () => {
  const f = await fixture({ cooldown: 10 });
  await f.gate.activate({ plan_id: f.id, source: "explicit_user_start" });
  const other = { ...f.parent, id: "other-campaign" };
  f.k.sync({
    accounts: [],
    tasks: [other],
    jobs: [
      {
        id: "other-job",
        kind: "join",
        reference_id: "r",
        task_id: other.id,
        account_id: "a",
        pool: "telegram.mtproto",
        version: "v",
        payload: { target: "@second_group", action: "join" },
        action: "join",
        state: "not_attempted",
        available_at: f.now(),
        expires_at: f.now() + 3600,
      },
    ],
  });
  f.advance(11);
  await f.gate.tick();
  assert.equal(
    f.gate.activationSnapshot(f.id).last_error,
    "ExecutorAccountHasOtherPendingTasks",
  );
  assert.equal(f.k.accountChannel("a"), null);
  assert.equal(f.writes.length, 0);
});
