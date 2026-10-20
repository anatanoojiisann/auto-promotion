import { Api } from "teleproto";
import { Raw } from "teleproto/events/Raw.js";
import { loginToken } from "./telegram-adapter.mjs";

export function loginError(code) {
  const error = new Error(code);
  error.name = code;
  return error;
}

// Timeout does not cancel RPCs. The same live guard fences every continuation.
export function keepQrLoginAlive(
  client,
  env,
  initial,
  {
    authorized,
    expired,
    failed,
    close,
    pending = async () => {},
    observe = async () => {},
    clock = () => Date.now(),
    timer = setTimeout,
    cancelTimer = clearTimeout,
    current = () => true,
    deadlineAt = clock() + 120000,
    diagnose = () => {},
  },
) {
  let done = false,
    importing = false,
    timeout,
    pollTimeout,
    resolve;
  let confirmationDeadline = null;
  let qrGeneration = 1,
    currentQr = initial.qr_uri;
  function log(event, fields = {}) {
    try {
      diagnose(event, { qr_generation: qrGeneration, ...fields });
    } catch {}
  }
  const complete = new Promise((r) => {
    resolve = r;
  });
  const event = new Raw({});
  let deadline = Math.min(deadlineAt, Date.parse(initial.expires_at));
  if (!Number.isFinite(deadline)) deadline = clock();
  const guard = {
    check() {
      if (done || !current()) throw loginError("TelegramQrFlowSuperseded");
      if (clock() >= deadline)
        throw loginError("TelegramQrConfirmationTimedOut");
    },
    remaining: () => Math.max(0, deadline - clock()),
  };
  async function settleBounded(action) {
    let cleanupTimer;
    try {
      await Promise.race([
        Promise.resolve().then(action),
        new Promise((resolve) => {
          cleanupTimer = timer(resolve, 2000);
        }),
      ]);
    } finally {
      cancelTimer(cleanupTimer);
    }
  }
  async function stop(action) {
    if (done) return;
    done = true;
    cancelTimer(timeout);
    cancelTimer(pollTimeout);
    client.removeEventHandler(onUpdate, event);
    try {
      await settleBounded(action);
    } finally {
      try {
        await settleBounded(close);
      } finally {
        resolve();
      }
    }
  }
  function arm() {
    cancelTimer(timeout);
    const source =
      confirmationDeadline !== null
        ? "confirmation"
        : importing
          ? "token_rpc"
          : "qr_expiry";
    log("timer_armed", {
      source,
      remaining_ms: Math.max(0, deadline - clock()),
      deadline_ms: deadline,
    });
    timeout = timer(
      () => {
        log("timeout_fired", {
          source:
            confirmationDeadline !== null
              ? "confirmation"
              : importing
                ? "token_rpc"
                : "qr_expiry",
          remaining_ms: 0,
          deadline_ms: deadline,
        });
        return stop(() =>
          confirmationDeadline !== null
            ? failed(loginError("TelegramQrConfirmationTimedOut"))
            : importing
              ? failed(loginError("TelegramQrTokenCheckTimedOut"))
              : expired(),
        );
      },
      Math.max(0, deadline - clock()),
    );
  }
  async function progress(value) {
    guard.check();
    if (
      confirmationDeadline === null &&
      [
        "login_update_received",
        "migrate_login",
        "login_token_authorized",
      ].includes(value.phase)
    ) {
      confirmationDeadline = Math.min(deadlineAt, clock() + 20000);
      deadline = confirmationDeadline;
      arm();
    }
    await observe(value, guard);
    guard.check();
  }
  async function confirm() {
    if (done || !current()) {
      log("stale_callback_ignored");
      return;
    }
    if (importing) {
      log("confirmation_duplicate");
      return;
    }
    importing = true;
    cancelTimer(pollTimeout);
    try {
      const result = await loginToken(client, env, progress);
      guard.check();
      if (result.status === "authorized") {
        await progress({ phase: "login_token_authorized" });
        // The timer covers identity, persistence and the different restored client.
        await authorized(guard);
        guard.check();
        await stop(async () => {});
      } else {
        const expires = Date.parse(result.expires_at);
        if (!Number.isFinite(expires) || expires <= clock())
          throw loginError("TelegramQrTokenInvalid");
        if (result.qr_uri !== currentQr) {
          qrGeneration++;
          currentQr = result.qr_uri;
        }
        if (confirmationDeadline === null) {
          deadline = Math.min(deadlineAt, expires);
          arm();
        }
        log("qr_refreshed", {
          remaining_ms: Math.max(0, deadline - clock()),
          deadline_ms: deadline,
        });
        await pending(result, guard);
        guard.check();
      }
    } catch (error) {
      if (!done) await stop(() => failed(error));
    } finally {
      importing = false;
      if (!done && current())
        pollTimeout = timer(
          confirm,
          Math.min(5000, Math.max(0, deadline - clock())),
        );
    }
  }
  async function onUpdate(update) {
    if (!(update instanceof Api.UpdateLoginToken)) return;
    if (done || !current()) {
      log("update_ignored", { source: "update" });
      return;
    }
    try {
      await progress({ phase: "login_update_received" });
      await confirm();
    } catch (error) {
      if (!done) await stop(() => failed(error));
    }
  }
  client.addEventHandler(onUpdate, event);
  arm();
  pollTimeout = timer(confirm, Math.min(5000, Math.max(0, deadline - clock())));
  complete.cancel = () => stop(async () => {});
  return complete;
}
