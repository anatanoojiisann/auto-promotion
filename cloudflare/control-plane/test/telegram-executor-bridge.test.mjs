import assert from "node:assert/strict";
import test from "node:test";
import { telegramExecutorBridge } from "../telegram-executor-bridge.mjs";
const plan = { definition: { account_ids: ["only-account"] } },
  workflows = { plan: () => plan };
test("bridge cannot be used to submit Telegram writes or read a private target", async () => {
  let calls = 0;
  const env = {
    TELEGRAM_EXECUTOR_ACCOUNT: "only-account",
    TELEGRAM_EXECUTOR: {
      fetch: async () => {
        calls++;
        return Response.json({ installed: true });
      },
    },
  };
  for (const read of [
    { operation: "execute", target: "@public" },
    { operation: "join", target: "@public" },
    { operation: "inspect", target: "me" },
  ]) {
    await assert.rejects(
      telegramExecutorBridge(env, workflows, "/v1/executor/telegram/read", {
        plan_id: "p",
        read,
      }),
    );
  }
  assert.equal(calls, 0);
  const r = await telegramExecutorBridge(
    env,
    workflows,
    "/v1/executor/telegram/status",
    { plan_id: "p" },
  );
  assert.equal(r.installed, true);
  assert.equal(calls, 1);
});
test("executor account is taken from the deployment, never a caller field", async () => {
  let observed;
  const env = {
    TELEGRAM_EXECUTOR_ACCOUNT: "only-account",
    TELEGRAM_EXECUTOR: {
      fetch: async (url, request) => {
        observed = JSON.parse(request.body);
        return Response.json({ status: "healthy" });
      },
    },
  };
  await telegramExecutorBridge(env, workflows, "/v1/executor/telegram/read", {
    plan_id: "p",
    read: { operation: "health", account_id: "other" },
  });
  assert.equal(observed.account_id, "only-account");
  env.TELEGRAM_EXECUTOR_ACCOUNT = "other";
  await assert.rejects(
    telegramExecutorBridge(env, workflows, "/v1/executor/telegram/status", {
      plan_id: "p",
    }),
    /ExecutorAccountNotAssigned/,
  );
});

test("login bridge forwards flow ownership and rejects malformed generations", async () => {
  const observed = [];
  const requests = [];
  const env = {
    TELEGRAM_EXECUTOR_ACCOUNT: "only-account",
    TELEGRAM_EXECUTOR: {
      fetch: async (url, request) => {
        observed.push(JSON.parse(request.body));
        requests.push(request.headers["X-AP-Request-ID"]);
        return Response.json({ status: "waiting_device_confirmation" });
      },
    },
  };
  const flow_id = "00000000-0000-4000-8000-000000000001";
  const result = await telegramExecutorBridge(
    env,
    workflows,
    "/v1/executor/telegram/login-poll",
    {
      plan_id: "p",
      source: "explicit_login_setup",
      flow_id,
      generation: 2,
    },
    flow_id,
  );
  assert.equal(result.request_id, flow_id);
  assert.deepEqual(requests, [flow_id]);
  assert.deepEqual(observed, [
    {
      account_id: "only-account",
      operation: "login_poll",
      flow_id,
      generation: 2,
    },
  ]);
  for (const generation of [true, 0, -1, 1.5, "2"]) {
    await assert.rejects(
      telegramExecutorBridge(
        env,
        workflows,
        "/v1/executor/telegram/login-poll",
        {
          plan_id: "p",
          source: "explicit_login_setup",
          flow_id,
          generation,
        },
      ),
      /InvalidLoginFlow/,
    );
  }
  assert.equal(observed.length, 1);
});
