import { DurableObject } from "cloudflare:workers";
import {
  createClient,
  readOperation,
  submitOperation,
  loginToken,
  safeError,
  LIBRARY_VERSION,
} from "./telegram-adapter.mjs";
import { ExecutorCore, preExecutionDeferral } from "./executor-core.mjs";
import { authorizeNative } from "./native-authorization.mjs";

function response(value, status = 200) {
  return Response.json(value, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}
export class TelegramExecutor extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.busy = false;
  }
  configured() {
    return !!(
      this.env.TG_API_ID &&
      this.env.TG_API_HASH &&
      this.env.TG_EXPECTED_ID &&
      this.env.TG_SESSION_KEK
    );
  }
  async seal(session) {
    const key = await crypto.subtle.importKey(
      "raw",
      Uint8Array.from(Buffer.from(this.env.TG_SESSION_KEK, "hex")),
      "AES-GCM",
      false,
      ["encrypt"],
    );
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const data = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv },
      key,
      new TextEncoder().encode(session),
    );
    return {
      iv: Buffer.from(iv).toString("base64"),
      data: Buffer.from(data).toString("base64"),
    };
  }
  async session() {
    const s = await this.ctx.storage.get("cloud_session");
    if (!s) return "";
    const key = await crypto.subtle.importKey(
      "raw",
      Uint8Array.from(Buffer.from(this.env.TG_SESSION_KEK, "hex")),
      "AES-GCM",
      false,
      ["decrypt"],
    );
    return new TextDecoder().decode(
      await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: Buffer.from(s.iv, "base64") },
        key,
        Buffer.from(s.data, "base64"),
      ),
    );
  }
  async fetch(request) {
    const input = await request.json(),
      op = input.operation;
    if (op === "status")
      return response({
        installed: true,
        runtime: "cloudflare-workers",
        library: LIBRARY_VERSION,
        api_configured: this.configured(),
        login_state:
          (await this.ctx.storage.get("login_state")) || "not_logged_in",
        native_gate_configured:
          !!this.env.CONTROL_INTERNAL && !!this.env.TG_QUEUE_BRIDGE,
        local_session_copied: false,
        last_error: (await this.ctx.storage.get("last_error")) || null,
        login_expires_at:
          (await this.ctx.storage.get("login_expires_at")) || null,
        supported_reads: [
          "health",
          "discover",
          "inspect",
          "membership_verify",
          "verify",
          "read_context",
        ],
        supported_native_actions: ["join", "reply"],
        supported_content: "plain_text_reply_with_reviewed_context",
        write_requires_native_lease: true,
      });
    if (!this.configured())
      return response(
        {
          status: "waiting_configuration",
          reason: "TelegramApplicationCredentialsRequired",
        },
        409,
      );
    const cooldown = (await this.ctx.storage.get("cooldown_until")) || 0;
    if (cooldown > Date.now() / 1000)
      return response(
        preExecutionDeferral(input, "waiting_cooldown", cooldown),
        409,
      );
    if (this.busy)
      return response(preExecutionDeferral(input, "waiting_account_lock"), 409);
    this.busy = true;
    let discardSession = false;
    let client,
      phase = "create_client";
    try {
      client = createClient(await this.session(), this.env);
      phase = "connect";
      await client.connect();
      if (op === "login_begin" || op === "login_poll") {
        phase = "login_token";
        const result = await loginToken(client, this.env);
        if (result.status === "authorized") {
          const me = await client.getMe();
          if (
            !me ||
            String(me.id) !== this.env.TG_EXPECTED_ID ||
            me.bot ||
            me.restricted ||
            me.deleted
          )
            throw new Error("TelegramIdentityMismatch");
        }
        const saved = client.session.save();
        if (!saved) throw new Error("CloudSessionPersistenceRequired");
        await this.ctx.storage.put("cloud_session", await this.seal(saved));
        await this.ctx.storage.put("login_state", result.status);
        if (result.expires_at)
          await this.ctx.storage.put("login_expires_at", result.expires_at);
        await this.ctx.storage.delete("last_error");
        return response(result);
      }
      phase = "identity_check";
      const me = await client.getMe();
      if (
        !me ||
        String(me.id) !== this.env.TG_EXPECTED_ID ||
        me.bot ||
        me.restricted ||
        me.deleted
      )
        throw new Error("TelegramIdentityMismatch");
      await this.ctx.storage.put("login_state", "authorized");
      await this.ctx.storage.delete("last_error");
      if (op === "execute") {
        phase = "native_execution";
        const core = new ExecutorCore({
          store: this.ctx.storage,
          authorize: (request) => authorizeNative(this.env, request),
          classifyError: safeError,
          submit: (job, checkpoint, revalidate) =>
            submitOperation(client, job, checkpoint, revalidate),
        });
        const outcome = await core.execute(input);
        if (outcome.retry_after_seconds && !outcome.from_durable_receipt)
          await this.ctx.storage.put(
            "cooldown_until",
            Date.now() / 1000 + outcome.retry_after_seconds,
          );
        return response(outcome);
      }
      if (
        ![
          "health",
          "discover",
          "inspect",
          "membership_verify",
          "verify",
          "read_context",
        ].includes(op)
      )
        throw new Error("UnsupportedExecutorOperation");
      phase = "authenticated_read";
      return response(await readOperation(client, op, input));
    } catch (error) {
      const safe = safeError(error);
      await this.ctx.storage.put("last_error", { phase, ...safe });
      if (safe.reason === "TelegramTwoStepLoginRequired")
        await this.ctx.storage.put(
          "login_state",
          "needs_two_step_verification",
        );
      if (safe.reason === "TelegramLoginRequired")
        await this.ctx.storage.put("login_state", "not_logged_in");
      if (safe.reason === "TelegramIdentityMismatch") {
        discardSession = true;
        await this.ctx.storage.delete("cloud_session");
        await this.ctx.storage.put("login_state", "identity_mismatch");
      }
      if (safe.retry_after_seconds)
        await this.ctx.storage.put(
          "cooldown_until",
          Date.now() / 1000 + safe.retry_after_seconds,
        );
      return response({ status: "unavailable", ...safe }, 409);
    } finally {
      if (client) {
        try {
          const s = client.session.save();
          if (s && !discardSession)
            await this.ctx.storage.put("cloud_session", await this.seal(s));
        } catch {}
        try {
          await client.destroy();
        } catch {}
      }
      this.busy = false;
    }
  }
}
export default {
  async fetch(request, env) {
    // workers_dev=false: only an explicitly configured service binding can reach this worker.
    if (request.method !== "POST")
      return response({ error: "MethodNotAllowed" }, 405);
    const input = await request.clone().json();
    if (input.account_id !== env.TG_ACCOUNT_ID)
      return response({ error: "AccountNotAssigned" }, 403);
    return env.TG.get(env.TG.idFromName(env.TG_ACCOUNT_ID)).fetch(request);
  },
};
