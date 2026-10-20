// Diagnostic inputs are deliberately NOT serialized. Only these fixed values
// and bounded numeric/UUID metadata can reach either the ring or the log sink.
const PHASES = new Set([
  "save_flow",
  "connect",
  "load_session",
  "decrypt_session",
  "existing_identity",
  "export_login_token",
  "import_login_token",
  "migrate_login",
  "login_update_received",
  "login_token_authorized",
  "login_token_pending",
  "waiting_phone",
  "verify_identity",
  "encrypt_session",
  "save_pending_session",
  "read_saved_session",
  "decrypt_saved_session",
  "restore_connect",
  "restore_identity",
  "authorized_session_saved",
  "cleanup",
]);
const EVENTS = new Set([
  "flow_started",
  "request_seen",
  "stage_started",
  "stage_completed",
  "stage_failed",
  "flow_terminal",
  "qr_refreshed",
  "timer_armed",
  "timeout_fired",
  "update_received",
  "update_ignored",
  "confirmation_duplicate",
  "stale_callback_ignored",
  "storage_failed",
  "cleanup_started",
  "cleanup_completed",
  "cleanup_timed_out",
]);
const SOURCES = new Set([
  "setup",
  "qr_expiry",
  "confirmation",
  "token_rpc",
  "cleanup",
  "storage",
  "poll",
  "update",
]);
const STATUSES = new Set([
  "authorized",
  "login_failed",
  "login_expired",
  "confirmation_timed_out",
  "identity_mismatch",
  "needs_two_step_verification",
  "login_interrupted",
]);
const CODES = new Set([
  "TelegramIdentityMismatch",
  "TelegramTwoStepLoginRequired",
  "TelegramLoginRequired",
  "TelegramConnectionUnavailable",
  "TelegramRiskRestriction",
  "TelegramCooldown",
  "CloudSessionPersistenceRequired",
  "CloudflareRuntimeCapabilityMissing",
  "TelegramQrFlowSuperseded",
  "TelegramQrConfirmationTimedOut",
  "TelegramQrTokenCheckTimedOut",
  "TelegramQrExpiredWithoutConfirmation",
  "TelegramQrTokenInvalid",
  "TelegramQrGenerationInvalid",
  "TelegramQrConnectionLost",
  "TelegramLoginUnavailable",
  "TelegramExecutorError",
]);
const CLASSES = new Set([
  "Error",
  "TypeError",
  "RangeError",
  "SyntaxError",
  "AbortError",
  "TimeoutError",
  "DOMException",
  "RpcError",
  "AuthKeyError",
  "FloodWaitError",
  "PasswordHashInvalidError",
  "SessionPasswordNeededError",
]);
const CAUSES = new Map([
  ["ETIMEDOUT", "timeout"],
  ["ECONNRESET", "connection_reset"],
  ["ECONNREFUSED", "connection_refused"],
  ["ENOTFOUND", "dns"],
  ["EAI_AGAIN", "dns"],
  ["ERR_TLS_CERT_ALTNAME_INVALID", "tls"],
  ["CERT_HAS_EXPIRED", "tls"],
  ["AUTH_KEY_UNREGISTERED", "authorization"],
  ["SESSION_REVOKED", "authorization"],
  ["SESSION_EXPIRED", "authorization"],
  ["SESSION_PASSWORD_NEEDED", "two_step"],
  ["SQLITE_BUSY", "storage_busy"],
  ["SQLITE_FULL", "storage_full"],
  ["QuotaExceededError", "storage_quota"],
]);
export function requestId(value) {
  return typeof value === "string" &&
    /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(
      value,
    )
    ? value
    : crypto.randomUUID();
}
export function safeLoginError(error, proposedReason) {
  let exception_type = "OtherError",
    cause = "unknown",
    reason = "TelegramExecutorError";
  try {
    if (CODES.has(proposedReason)) reason = proposedReason;
    else if (CODES.has(error?.name)) reason = error.name;
    else if (CODES.has(error?.message)) reason = error.message;
    if (CLASSES.has(error?.name)) exception_type = error.name;
    else if (CODES.has(error?.name)) exception_type = "LoginFlowError";
    const seen = new Set();
    let current = error;
    for (let depth = 0; current && depth < 4 && !seen.has(current); depth++) {
      seen.add(current);
      for (const candidate of [
        current.code,
        current.errorMessage,
        current.name,
      ]) {
        if (CAUSES.has(candidate)) {
          cause = CAUSES.get(candidate);
          break;
        }
      }
      if (cause !== "unknown") break;
      current = current.cause;
    }
  } catch {
    /* A throwing error getter is not diagnostic evidence. */
  }
  return { reason, exception_type, cause };
}
const bounded = (value, max = 1200000) =>
  Number.isFinite(value) && value >= 0
    ? Math.min(max, Math.floor(value))
    : undefined;

export class LoginDiagnostics {
  constructor({
    flowId,
    request: requestValue,
    clock = () => Date.now(),
    sink = (entry) => console.info(JSON.stringify(entry)),
  }) {
    this.flowId = requestId(flowId);
    this.request = requestId(requestValue);
    this.clock = clock;
    this.sink = sink;
    this.events = [];
    this.sent = 0;
    this.normalSent = 0;
    this.sequence = 0;
    this.dropped = 0;
    this.closed = false;
  }
  emit(event, input = {}) {
    try {
      if (!EVENTS.has(event)) return;
      const entry = {
        schema: 1,
        event,
        flow_id: this.flowId,
        request_id: requestId(input.request_id || this.request),
        sequence: (this.sequence = Math.min(1000000, this.sequence + 1)),
        at: new Date(this.clock()).toISOString(),
      };
      if (PHASES.has(input.phase)) entry.phase = input.phase;
      if (SOURCES.has(input.source)) entry.source = input.source;
      if (STATUSES.has(input.status)) entry.status = input.status;
      if (Number.isSafeInteger(input.generation) && input.generation > 0)
        entry.generation = input.generation;
      if (
        Number.isSafeInteger(input.qr_generation) &&
        input.qr_generation > 0 &&
        input.qr_generation <= 1000000
      )
        entry.qr_generation = input.qr_generation;
      if (Number.isSafeInteger(input.dc) && input.dc > 0 && input.dc < 10)
        entry.dc = input.dc;
      for (const key of ["elapsed_ms", "remaining_ms", "deadline_ms"]) {
        const value = bounded(
          input[key],
          key === "deadline_ms" ? 8640000000000000 : 1200000,
        );
        if (value !== undefined) entry[key] = value;
      }
      if (input.error)
        Object.assign(entry, safeLoginError(input.error, input.reason));
      if (JSON.stringify(entry).length > 768) return;
      if (input.error && !this.firstError && !this.closed)
        this.firstError = structuredClone(entry);
      if (!this.closed) {
        const previous = this.events.find(
          (e) =>
            e.event === event &&
            e.phase === entry.phase &&
            e.source === entry.source &&
            e.generation === entry.generation &&
            e.qr_generation === entry.qr_generation,
        );
        if (
          previous &&
          [
            "request_seen",
            "update_ignored",
            "confirmation_duplicate",
            "stale_callback_ignored",
          ].includes(event)
        ) {
          previous.count = Math.min(1000000, (previous.count || 1) + 1);
          previous.last_at = entry.at;
          if (entry.dc) previous.dc = entry.dc;
        } else {
          if (this.events.length >= 64) {
            let index = this.events.findIndex(
              (e) =>
                !(
                  [
                    "flow_started",
                    "stage_failed",
                    "storage_failed",
                    "flow_terminal",
                  ].includes(e.event) ||
                  (["stage_started", "stage_completed"].includes(e.event) &&
                    !["export_login_token", "login_token_pending"].includes(
                      e.phase,
                    ))
                ),
            );
            if (index < 0) index = 1;
            this.events.splice(index, 1);
            this.dropped = Math.min(1000000, this.dropped + 1);
          }
          this.events.push(entry);
        }
        if (event === "flow_terminal") this.closed = true;
      }
      const reserved = [
        "flow_terminal",
        "cleanup_started",
        "cleanup_completed",
        "cleanup_timed_out",
      ].includes(event);
      const important =
        reserved || ["stage_failed", "storage_failed"].includes(event);
      if (
        this.sent < 96 &&
        (reserved || this.sent < 92) &&
        (important || this.normalSent < 64)
      ) {
        this.sent++;
        if (!important) this.normalSent++;
        try {
          const result = this.sink(structuredClone(entry));
          result?.catch?.(() => {});
        } catch {}
      }
    } catch {
      /* Logging can never change login/control flow. */
    }
  }
  snapshot(terminal) {
    const events = structuredClone(this.events);
    let dropped = this.dropped;
    if (
      STATUSES.has(terminal) &&
      !events.some((e) => e.event === "flow_terminal")
    ) {
      // This projection is committed atomically WITH the terminal/session row.
      // A rolled-back write never publishes a successful terminal assertion.
      const last = events.findLast((e) => e.generation);
      const base = {
        schema: 1,
        flow_id: this.flowId,
        request_id: this.request,
        at: new Date(this.clock()).toISOString(),
        ...(last?.generation ? { generation: last.generation } : {}),
        source: "storage",
      };
      const append = (entry) => {
        if (events.length >= 64) {
          const index = events.findIndex(
            (e) =>
              !["flow_started", "stage_failed", "storage_failed"].includes(
                e.event,
              ),
          );
          events.splice(index < 0 ? 1 : index, 1);
          dropped = Math.min(1000000, dropped + 1);
        }
        events.push(entry);
      };
      if (terminal === "authorized")
        append({
          ...base,
          event: "stage_completed",
          phase: "authorized_session_saved",
          sequence: Math.min(1000000, this.sequence + 1),
        });
      append({
        ...base,
        event: "flow_terminal",
        status: terminal,
        sequence: Math.min(1000000, this.sequence + 2),
      });
    }
    return {
      schema: 1,
      events,
      dropped,
      ...(this.firstError
        ? { first_error: structuredClone(this.firstError) }
        : {}),
      ...(STATUSES.has(terminal) ? { terminal } : {}),
    };
  }
}
