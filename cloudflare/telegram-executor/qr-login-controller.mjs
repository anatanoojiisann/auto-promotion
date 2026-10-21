import { keepQrLoginAlive, loginError } from "./qr-login-flow.mjs";
import { loginToken, safeError } from "./telegram-adapter.mjs";
import {
  LoginDiagnostics,
  requestId,
  safeLoginError,
} from "./qr-login-diagnostics.mjs";

const ACTIVE = new Set([
  "connecting",
  "waiting_device_confirmation",
  "confirming",
]);

// No native authorization, reader or submission interface is available here.
export class QrLoginController {
  constructor({
    store,
    env,
    createClient,
    seal,
    unseal,
    waitUntil = () => {},
    clock = () => Date.now(),
    timer = (callback, milliseconds) =>
      globalThis.setTimeout(callback, milliseconds),
    cancelTimer = (handle) => globalThis.clearTimeout(handle),
    diagnosticSink,
  }) {
    Object.assign(this, {
      store,
      env,
      createClient,
      seal,
      unseal,
      waitUntil,
      clock,
      timer,
      cancelTimer,
      diagnosticSink,
    });
    this.current = null;
  }
  get active() {
    return !!this.current && (!this.current.terminal || !!this.current.closing);
  }
  owns(flow) {
    return this.current === flow;
  }
  diagnose(flow, event, fields = {}) {
    flow?.diagnostics?.emit(event, {
      phase: flow.phase,
      generation: flow.generation,
      qr_generation: flow.qrGeneration,
      ...fields,
    });
  }
  guard(flow) {
    return {
      check: () => {
        if (!this.owns(flow) || flow.terminal) {
          this.diagnose(flow, "stale_callback_ignored");
          throw loginError("TelegramQrFlowSuperseded");
        }
        if (this.clock() >= flow.deadline)
          throw loginError("TelegramQrConfirmationTimedOut");
      },
      remaining: () => Math.max(0, flow.deadline - this.clock()),
    };
  }
  async bounded(flow, guard, action) {
    guard.check();
    let timeout;
    try {
      const result = await Promise.race([
        Promise.resolve().then(action),
        new Promise((_, reject) => {
          timeout = this.timer(
            () => reject(loginError("TelegramQrConfirmationTimedOut")),
            guard.remaining(),
          );
        }),
      ]);
      guard.check();
      return result;
    } finally {
      this.cancelTimer(timeout);
    }
  }
  async write(flow, changes, extra = {}, guard = null, terminal = false) {
    if (!this.owns(flow) || (!terminal && flow.terminal))
      throw loginError("TelegramQrFlowSuperseded");
    guard?.check();
    let next;
    try {
      next = await this.store.transaction(async (tx) => {
        const saved = await tx.get("login_flow");
        if (
          !saved ||
          saved.flow_id !== flow.id ||
          saved.generation !== flow.generation ||
          !ACTIVE.has(saved.status) ||
          !this.owns(flow)
        )
          throw loginError("TelegramQrFlowSuperseded");
        guard?.check();
        const value = {
          ...saved,
          ...changes,
          observed_at: new Date(this.clock()).toISOString(),
          diagnostic_trace: flow.diagnostics.snapshot(changes.status),
        };
        await tx.put({
          ...extra,
          login_flow: value,
          login_state: value.status,
          login_diagnostics: value,
          login_expires_at: value.expires_at || null,
        });
        // A timeout/new generation while an awaited write was in flight aborts the
        // transaction, rather than publishing a late authorized/session update.
        if (!this.owns(flow) || (!terminal && flow.terminal))
          throw loginError("TelegramQrFlowSuperseded");
        guard?.check();
        return value;
      });
    } catch (error) {
      this.diagnose(flow, "storage_failed", { source: "storage", error });
      throw error;
    }
    if (this.owns(flow)) flow.snapshot = next;
    return next;
  }
  async stage(flow, phase, action, guard) {
    flow.phase = phase;
    const started = this.clock();
    this.diagnose(flow, "stage_started", {
      remaining_ms: guard.remaining(),
      deadline_ms: this.clock() + guard.remaining(),
    });
    try {
      await this.bounded(flow, guard, () =>
        this.write(flow, { phase }, {}, guard),
      );
      const result = await this.bounded(flow, guard, action);
      this.diagnose(flow, "stage_completed", {
        elapsed_ms: this.clock() - started,
        remaining_ms: guard.remaining(),
      });
      return result;
    } catch (error) {
      this.diagnose(flow, "stage_failed", {
        elapsed_ms: this.clock() - started,
        remaining_ms: guard.remaining(),
        error,
      });
      throw error;
    }
  }
  async close(flow) {
    // destroy sets teleproto's destroyed fence synchronously. Do not hold an
    // account lock forever if a socket's physical cleanup fails to finish.
    let timeout;
    const started = this.clock();
    let timedOut = false;
    this.diagnose(flow, "cleanup_started", {
      phase: "cleanup",
      source: "cleanup",
    });
    const cleanup = Promise.allSettled(
      [flow.client, flow.restored]
        .filter(Boolean)
        .map((client) => Promise.resolve().then(() => client.destroy())),
    );
    try {
      await Promise.race([
        cleanup,
        new Promise((resolve) => {
          timeout = this.timer(() => {
            timedOut = true;
            resolve();
          }, 2000);
        }),
      ]);
    } finally {
      this.cancelTimer(timeout);
      flow.closing = false;
      this.diagnose(
        flow,
        timedOut ? "cleanup_timed_out" : "cleanup_completed",
        {
          phase: "cleanup",
          source: "cleanup",
          elapsed_ms: this.clock() - started,
        },
      );
    }
  }
  async end(flow, error, status = "login_failed") {
    if (!this.owns(flow) || flow.terminal) return;
    const platformError = safeError(error);
    const safe = safeLoginError(error, platformError.reason);
    const retryAt =
      (platformError.rate_limited || platformError.risk_warning) &&
      Number.isFinite(platformError.retry_after_seconds) &&
      platformError.retry_after_seconds > 0
        ? this.clock() / 1000 + platformError.retry_after_seconds
        : null;
    if (safe.reason === "TelegramIdentityMismatch")
      status = "identity_mismatch";
    if (safe.reason === "TelegramTwoStepLoginRequired")
      status = "needs_two_step_verification";
    if (safe.reason === "TelegramQrConfirmationTimedOut")
      status = "confirmation_timed_out";
    flow.terminal = true;
    flow.closing = true;
    this.diagnose(flow, "flow_terminal", {
      status,
      error,
      reason: safe.reason,
    });
    flow.snapshot = {
      ...flow.snapshot,
      status,
      reason: safe.reason,
      phase: flow.phase,
      exception_type: safe.exception_type,
      diagnostic_trace: flow.diagnostics.snapshot(status),
      finished_at: new Date(this.clock()).toISOString(),
      persistence_verified: false,
      ...(retryAt ? { retry_at: retryAt } : {}),
    };
    flow.qr = null;
    try {
      await this.write(
        flow,
        { ...flow.snapshot, persistence_verified: true },
        {
          last_error: {
            flow_id: flow.id,
            generation: flow.generation,
            phase: flow.phase,
            reason: safe.reason,
            exception_type: safe.exception_type,
          },
          qr_pending_session: null,
          ...(retryAt ? { login_cooldown_until: retryAt } : {}),
        },
        null,
        true,
      );
    } catch {
      // Keep a fail-closed in-memory terminal view if storage itself is down.
      flow.snapshot.persistence_verified = false;
    }
  }
  validate(me) {
    if (
      !me ||
      String(me.id) !== this.env.TG_EXPECTED_ID ||
      me.bot ||
      me.restricted ||
      me.deleted
    )
      throw loginError("TelegramIdentityMismatch");
  }
  async authorize(flow, guard) {
    this.validate(
      await this.stage(
        flow,
        "verify_identity",
        () => flow.client.getMe(),
        guard,
      ),
    );
    const session = flow.client.session.save();
    if (!session) throw loginError("CloudSessionPersistenceRequired");
    const sealed = await this.stage(
      flow,
      "encrypt_session",
      () => this.seal(session),
      guard,
    );
    await this.stage(
      flow,
      "save_pending_session",
      () =>
        this.write(
          flow,
          {},
          {
            qr_pending_session: {
              flow_id: flow.id,
              generation: flow.generation,
              sealed,
            },
          },
          guard,
        ),
      guard,
    );
    const pending = await this.stage(
      flow,
      "read_saved_session",
      () => this.store.get("qr_pending_session"),
      guard,
    );
    if (
      !pending ||
      pending.flow_id !== flow.id ||
      pending.generation !== flow.generation
    )
      throw loginError("CloudSessionPersistenceRequired");
    const restored = await this.stage(
      flow,
      "decrypt_saved_session",
      () => this.unseal(pending.sealed),
      guard,
    );
    if (!restored) throw loginError("CloudSessionPersistenceRequired");
    flow.restored = this.createClient(restored);
    if (flow.restored === flow.client)
      throw loginError("CloudSessionPersistenceRequired");
    await this.stage(
      flow,
      "restore_connect",
      () => flow.restored.connect(),
      guard,
    );
    this.validate(
      await this.stage(
        flow,
        "restore_identity",
        () => flow.restored.getMe(),
        guard,
      ),
    );
    flow.phase = "authorized_session_saved";
    const commitStarted = this.clock();
    this.diagnose(flow, "stage_started", { remaining_ms: guard.remaining() });
    await this.bounded(flow, guard, () =>
      this.write(
        flow,
        {
          status: "authorized",
          phase: flow.phase,
          persistence_verified: true,
          last_verified_at: new Date(this.clock()).toISOString(),
          reason: null,
        },
        {
          cloud_session: pending.sealed,
          qr_pending_session: null,
          last_error: null,
        },
        guard,
      ),
    );
    flow.terminal = true;
    flow.qr = null;
    this.diagnose(flow, "stage_completed", {
      elapsed_ms: this.clock() - commitStarted,
    });
    this.diagnose(flow, "flow_terminal", { status: "authorized" });
  }
  async observe(flow, event, guard) {
    const phase = event.phase;
    if (["stage_completed", "stage_failed"].includes(event.event)) {
      this.diagnose(flow, event.event, {
        phase,
        dc: event.dc,
        elapsed_ms: event.elapsed_ms,
        error: event.error,
      });
      return; // Extra timing logs never add mandatory diagnostic storage writes.
    }
    this.diagnose(
      flow,
      phase === "login_update_received" ? "update_received" : "stage_started",
      { phase, dc: event.dc, remaining_ms: guard.remaining() },
    );
    if (
      phase === "login_update_received" ||
      phase === "migrate_login" ||
      phase === "login_token_authorized"
    )
      flow.confirmation = true;
    const history = [
      ...(flow.snapshot.history || []),
      { phase, at: new Date(this.clock()).toISOString() },
    ].slice(-24);
    flow.phase = phase;
    await this.write(
      flow,
      {
        phase,
        history,
        cloud_confirmation_observed: !!flow.confirmation,
        ...(flow.confirmation ? { status: "confirming" } : {}),
        checks:
          (flow.snapshot.checks || 0) +
          (phase === "export_login_token" ? 1 : 0),
        ...(Number.isSafeInteger(event.dc) && event.dc > 0 && event.dc < 10
          ? { dc: event.dc }
          : {}),
      },
      {},
      guard,
    );
  }
  async view(input = {}) {
    const request_id = requestId(input.request_id);
    if (this.current)
      this.diagnose(this.current, "request_seen", {
        request_id,
        source: "poll",
      });
    let saved =
      this.current?.terminal &&
      this.current.snapshot.persistence_verified === false
        ? this.current.snapshot
        : await this.store.get("login_flow");
    if (!saved) {
      const legacy = await this.store.get("login_state");
      return {
        status:
          legacy === "authorized"
            ? "verification_required"
            : legacy || "not_logged_in",
        reader_started: false,
        last_verified_at: null,
        request_id,
      };
    }
    if (
      ACTIVE.has(saved.status) &&
      (!this.current || this.current.id !== saved.flow_id)
    ) {
      // Poll/status are read-only. Losing a socket changes the derived view,
      // never durable state or pending ciphertext; explicit begin owns cleanup.
      saved = {
        ...saved,
        status: "login_interrupted",
        reason: "TelegramQrConnectionLost",
        state_source: "derived_lost_connection",
      };
    }
    const local = this.current;
    if (
      local?.id === saved.flow_id &&
      local.terminal &&
      local.snapshot.status !== "authorized"
    )
      saved = local.snapshot;
    if (
      (input.flow_id && input.flow_id !== saved.flow_id) ||
      (input.generation !== undefined && input.generation !== saved.generation)
    )
      return {
        status: "flow_superseded",
        flow_id: saved.flow_id,
        generation: saved.generation,
        reader_started: false,
        request_id,
      };
    return {
      status: saved.status,
      request_id,
      flow_id: saved.flow_id,
      generation: saved.generation,
      expires_at: saved.expires_at || null,
      qr_generation:
        local?.id === saved.flow_id
          ? local.qrGeneration
          : saved.qr_generation || null,
      login_diagnostics: saved,
      reader_started: false,
      last_verified_at: saved.last_verified_at || null,
      diagnostic_trace:
        local?.id === saved.flow_id
          ? local.diagnostics.snapshot(saved.status)
          : saved.diagnostic_trace || null,
      ...(local?.id === saved.flow_id &&
      saved.status === "waiting_device_confirmation" &&
      this.clock() < Date.parse(saved.expires_at) &&
      local.qr
        ? { qr_uri: local.qr.qr_uri }
        : {}),
    };
  }
  async begin(input = {}) {
    if (this.active && input.replace_flow_id !== this.current.id)
      return this.view(input);
    const previous = this.current;
    const flow = {
      id: crypto.randomUUID(),
      terminal: false,
      phase: "connect",
      deadline: this.clock() + 10000,
      totalDeadline: this.clock() + 120000,
      snapshot: {},
      client: null,
      restored: null,
    };
    flow.diagnostics = new LoginDiagnostics({
      flowId: flow.id,
      request: input.request_id,
      clock: this.clock,
      ...(this.diagnosticSink ? { sink: this.diagnosticSink } : {}),
    });
    this.diagnose(flow, "flow_started", {
      source: "setup",
      remaining_ms: 10000,
      deadline_ms: flow.totalDeadline,
    });
    this.current = flow;
    if (previous?.run) this.waitUntil(previous.run.cancel());
    flow.snapshot = {
      flow_id: flow.id,
      status: "connecting",
      phase: "save_flow",
      persistence_verified: false,
    };
    const initialGuard = this.guard(flow);
    try {
      const initial = await this.bounded(flow, initialGuard, () =>
        this.store.transaction(async (tx) => {
          initialGuard.check();
          const previousGeneration = (await tx.get("login_generation")) || 0;
          if (
            !Number.isSafeInteger(previousGeneration) ||
            previousGeneration < 0 ||
            previousGeneration >= Number.MAX_SAFE_INTEGER
          )
            throw loginError("TelegramQrGenerationInvalid");
          const generation = previousGeneration + 1;
          flow.generation = generation;
          const value = {
            flow_id: flow.id,
            generation,
            status: "connecting",
            phase: "connect",
            checks: 0,
            started_at: new Date(this.clock()).toISOString(),
            deadline_at: new Date(flow.totalDeadline).toISOString(),
            cloud_confirmation_observed: false,
            persistence_verified: false,
            diagnostic_trace: flow.diagnostics.snapshot(),
          };
          await tx.put({
            login_generation: generation,
            login_flow: value,
            login_state: value.status,
            login_diagnostics: value,
            qr_pending_session: null,
            last_error: null,
            login_expires_at: null,
          });
          initialGuard.check();
          return value;
        }),
      );
      flow.snapshot = initial;
      const guard = this.guard(flow);
      const saved = await this.stage(
        flow,
        "load_session",
        () => this.store.get("cloud_session"),
        guard,
      );
      const session = saved
        ? await this.stage(
            flow,
            "decrypt_session",
            () => this.unseal(saved),
            guard,
          )
        : "";
      flow.client = this.createClient(session);
      await this.stage(flow, "connect", () => flow.client.connect(), guard);
      let restoredAuthorization = false;
      if (session) {
        try {
          this.validate(
            await this.stage(
              flow,
              "existing_identity",
              () => flow.client.getMe(),
              guard,
            ),
          );
          restoredAuthorization = true;
        } catch (error) {
          if (safeError(error).reason !== "TelegramLoginRequired") throw error;
        }
      }
      const result = restoredAuthorization
        ? { status: "authorized" }
        : await this.stage(
            flow,
            "export_login_token",
            () =>
              loginToken(flow.client, this.env, (event) => {
                flow.phase = event.phase;
                this.diagnose(flow, event.event || "stage_started", {
                  phase: event.phase,
                  dc: event.dc,
                  elapsed_ms: event.elapsed_ms,
                  error: event.error,
                });
              }),
            guard,
          );
      if (result.status === "authorized") {
        flow.deadline = Math.min(flow.totalDeadline, this.clock() + 20000);
        await this.authorize(flow, this.guard(flow));
        await this.close(flow);
      } else {
        const expires = Date.parse(result.expires_at);
        if (!Number.isFinite(expires) || expires <= this.clock())
          throw loginError("TelegramQrTokenInvalid");
        flow.qrGeneration = 1;
        await this.write(
          flow,
          {
            status: "waiting_device_confirmation",
            phase: "waiting_phone",
            expires_at: result.expires_at,
            qr_generation: flow.qrGeneration,
          },
          {},
          guard,
        );
        flow.qr = result;
        flow.run = keepQrLoginAlive(flow.client, this.env, result, {
          clock: this.clock,
          timer: this.timer,
          cancelTimer: this.cancelTimer,
          deadlineAt: flow.totalDeadline,
          current: () => this.owns(flow),
          observe: (event, g) => this.observe(flow, event, g),
          authorized: (g) => this.authorize(flow, g),
          pending: async (pending, g) => {
            if (pending.qr_uri !== flow.qr?.qr_uri) flow.qrGeneration++;
            await this.write(
              flow,
              {
                expires_at: pending.expires_at,
                qr_generation: flow.qrGeneration,
              },
              {},
              g,
            );
            flow.qr = pending;
          },
          expired: () =>
            this.end(
              flow,
              loginError("TelegramQrExpiredWithoutConfirmation"),
              "login_expired",
            ),
          failed: (error) => this.end(flow, error),
          close: () => this.close(flow),
          diagnose: (event, fields) => this.diagnose(flow, event, fields),
        });
        this.waitUntil(flow.run);
      }
    } catch (error) {
      let terminalTimer;
      try {
        await Promise.race([
          this.end(flow, error),
          new Promise((resolve) => {
            terminalTimer = this.timer(resolve, 2000);
          }),
        ]);
      } finally {
        this.cancelTimer(terminalTimer);
        await this.close(flow);
      }
    }
    return this.view({ request_id: flow.diagnostics.request });
  }
}
