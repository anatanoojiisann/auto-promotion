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
