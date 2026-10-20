import { ControlError } from "./kernel.mjs";

const PREFIX = "/v1/executor/telegram/";
export async function telegramExecutorBridge(env, workflows, path, body) {
  if (!env.TELEGRAM_EXECUTOR)
    throw new ControlError("TelegramExecutorNotBound", 409);
  const plan = workflows.plan(body.plan_id);
  const account = env.TELEGRAM_EXECUTOR_ACCOUNT;
  if (!account || !plan.definition.account_ids.includes(account))
    throw new ControlError("ExecutorAccountNotAssigned", 403);
  const action = path.slice(PREFIX.length);
  let input = { account_id: account };
  if (action === "status") input.operation = "status";
  else if (action === "login-begin" || action === "login-poll") {
    if (!body.source)
      throw new ControlError("ExplicitExecutorSetupRequired", 400);
    input.operation = action === "login-begin" ? "login_begin" : "login_poll";
  } else if (action === "read") {
    const operation = body.read?.operation;
    if (
      ![
        "health",
        "discover",
        "inspect",
        "membership_verify",
        "verify",
        "read_context",
      ].includes(operation)
    )
      throw new ControlError("ReadOnlyExecutorBridge", 403);
    if (
      !["health", "discover"].includes(operation) &&
      !/^@[a-zA-Z][a-zA-Z0-9_]{3,31}$/.test(body.read.target || "")
    )
      throw new ControlError("PublicTargetRequired", 400);
    input = { ...body.read, operation, account_id: account };
  } else throw new ControlError("NotFound", 404);
  const response = await env.TELEGRAM_EXECUTOR.fetch(
    "https://telegram-executor/internal",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
  const result = await response.json();
  if (!response.ok)
    throw new ControlError(
      result.reason || result.error || "TelegramExecutorUnavailable",
      response.status,
    );
  return result;
}
