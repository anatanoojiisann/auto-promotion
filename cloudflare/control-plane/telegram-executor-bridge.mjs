import { ControlError } from "./kernel.mjs";

const PREFIX = "/v1/executor/telegram/";
// Registration may be queued during a platform read/write wait once the
// independent login has already proved identity and durable session recovery.
// This function never calls a Telegram API operation through the actor.
export async function verifiedCooldownStart(kernel, env) {
  const account = kernel.one(
    "SELECT identity FROM accounts WHERE id=?",
    env.TELEGRAM_EXECUTOR_ACCOUNT,
  );
  if (!account) throw new ControlError("ExecutorAccountNotAssigned", 403);
  const state = kernel.one(
    "SELECT MAX(cooldown_until) cooldown_until,MAX(disabled) disabled FROM accounts WHERE identity=?",
    account.identity,
  );
  if (state.disabled) throw new ControlError("ExecutionStopped");
  if (state.cooldown_until <= kernel.clock()) return null;
  const response = await env.TELEGRAM_EXECUTOR.fetch(
    "https://telegram-executor/internal",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_id: env.TELEGRAM_EXECUTOR_ACCOUNT,
        operation: "status",
      }),
    },
  );
  const status = await response.json();
  const verifiedAt = Date.parse(status.last_verified_at) / 1000;
  if (
    !response.ok ||
    status.account_id !== env.TELEGRAM_EXECUTOR_ACCOUNT ||
    status.login_state !== "authorized" ||
    status.login_diagnostics?.persistence_verified !== true ||
    status.connection_status !== "last_identity_verified" ||
    !Number.isFinite(verifiedAt) ||
    verifiedAt > kernel.clock() + 60 ||
    verifiedAt < kernel.clock() - 86400
  )
    throw new ControlError("ExecutorLoginNotVerified", 409);
  const notBefore = Math.max(
    state.cooldown_until,
    Number(status.account_not_before || 0),
  );
  if (!Number.isFinite(notBefore))
    throw new ControlError("InvalidAccountCooldown", 409);
  return { not_before: notBefore, login_verified_at: status.last_verified_at };
}
export function executorRequestId(value) {
  return typeof value === "string" &&
    /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(
      value,
    )
    ? value
    : crypto.randomUUID();
}
export async function telegramExecutorBridge(
  env,
  workflows,
  path,
  body,
  incomingRequestId,
) {
  const request_id = executorRequestId(incomingRequestId);
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
    for (const key of ["flow_id", "replace_flow_id"]) {
      if (body[key] !== undefined) {
        if (!/^[a-f0-9-]{36}$/.test(body[key]))
          throw new ControlError("InvalidLoginFlow", 400);
        input[key] = body[key];
      }
    }
    if (body.generation !== undefined) {
      if (!Number.isSafeInteger(body.generation) || body.generation < 1)
        throw new ControlError("InvalidLoginFlow", 400);
      input.generation = body.generation;
    }
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
      headers: {
        "Content-Type": "application/json",
        "X-AP-Request-ID": request_id,
      },
      body: JSON.stringify(input),
    },
  );
  const result = await response.json();
  if (!response.ok)
    throw new ControlError(
      result.reason || result.error || "TelegramExecutorUnavailable",
      response.status,
    );
  return { ...result, request_id };
}
