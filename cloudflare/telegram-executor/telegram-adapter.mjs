import { TelegramClient, Api } from "teleproto";
import { StringSession } from "teleproto/sessions";
import { Logger } from "teleproto/extensions";

export const LIBRARY_VERSION = "1.230.1";
export function createClient(session, env) {
  return new TelegramClient(
    new StringSession(session || ""),
    Number(env.TG_API_ID),
    env.TG_API_HASH,
    {
      requestRetries: 1,
      connectionRetries: 1,
      reconnectRetries: 0,
      autoReconnect: false,
      retryDelay: 0,
      floodSleepThreshold: 0,
      timeout: 8,
      baseLogger: new Logger("none"),
      deviceModel: "AIgram cloud executor",
      systemVersion: "Cloudflare Workers",
      appVersion: "0.1",
      maxConcurrentDownloads: 1,
      securityChecks: true,
    },
  );
}
export function safeError(error) {
  const code = String(
    error?.errorMessage || error?.name || "TelegramExecutorError",
  );
  const known = new Set([
    "TelegramIdentityMismatch",
    "NativeGateNotConnected",
    "NativeGateRejected",
    "PublicDiscussionRequired",
    "UnsupportedNativeAction",
    "MembershipNotVerified",
    "TargetWriteForbidden",
    "ReplyContextRequired",
    "ReplyContextChanged",
    "MessageIdRequired",
    "CloudSessionPersistenceRequired",
  ]);
  if (known.has(error?.message)) return { reason: error.message };
  if (/PEER_FLOOD|USER_RESTRICTED|USER_BANNED/.test(code))
    return {
      reason: "TelegramRiskRestriction",
      risk_warning: true,
      retry_after_seconds: 86400,
    };
  if (/FLOOD|SLOWMODE/.test(code))
    return {
      reason: "TelegramCooldown",
      rate_limited: true,
      retry_after_seconds: Math.max(60, Number(error.seconds) || 3600),
    };
  if (/PASSWORD_NEEDED/.test(code))
    return { reason: "TelegramTwoStepLoginRequired" };
  if (/AUTH_KEY_UNREGISTERED|SESSION_REVOKED|SESSION_EXPIRED/.test(code))
    return { reason: "TelegramLoginRequired" };
  const message = String(error?.message || "").toLowerCase();
  if (/not implemented|not supported|wasm|webassembly/.test(message))
    return { reason: "CloudflareRuntimeCapabilityMissing" };
  if (/connect|socket|timeout/.test(message))
    return { reason: "TelegramConnectionUnavailable" };
  return {
    reason:
      /^[A-Za-z0-9_]{1,80}$/.test(code) && code !== "Error"
        ? code
        : "TelegramExecutorError",
  };
}
function iso(date) {
  return new Date(Number(date) * 1000).toISOString();
}
function members(participant) {
  return [
    "ChannelParticipant",
    "ChannelParticipantSelf",
    "ChannelParticipantAdmin",
    "ChannelParticipantCreator",
  ].includes(participant?.className);
}
async function observedMember(client, entity) {
  try {
    const p = await client.getParticipant(entity, "me");
    return members(p.participant);
  } catch (e) {
    if (e.errorMessage === "USER_NOT_PARTICIPANT") return false;
    throw e;
  }
}
export async function readOperation(client, operation, input) {
  if (operation === "health")
    return { status: "healthy", identity_verified: true };
  if (operation === "discover") {
    const r = await client.invoke(
      new Api.contacts.Search({
        q: String(input.query).slice(0, 120),
        limit: 30,
      }),
      undefined,
      { maxRetryCount: 0 },
    );
    return {
      candidates: r.chats
        .filter((c) => c.username)
        .map((c) => ({
          target: "@" + c.username,
          title: c.title,
          is_group: !!c.megagroup,
          peer_id: String(c.id),
        })),
    };
  }
  const entity = await client.getEntity(input.target);
  if (!entity.username || (!entity.megagroup && !entity.broadcast))
    return { status: "unavailable", reason: "PublicEntryRequired" };
  if (operation === "read_context") {
    if (!Number.isInteger(input.message_id) || input.message_id < 1)
      throw new Error("MessageIdRequired");
    const source = (
      await client.getMessages(entity, { ids: [input.message_id] })
    )[0];
    const replies = await client.getMessages(entity, {
      replyTo: input.message_id,
      limit: 50,
    });
    const map = (m) => ({
      id: m.id,
      text: (m.message || "").slice(0, 12000),
      date: iso(m.date),
      out: !!m.out,
      author_id: String(m.fromId?.userId || ""),
      author_is_bot: typeof m.sender?.bot === "boolean" ? m.sender.bot : null,
      reply_to: m.replyTo?.replyToMsgId || null,
      url: `https://t.me/${entity.username}/${m.id}`,
    });
    return {
      discussion_id: String(entity.id),
      target: input.target,
      source: source ? map(source) : null,
      replies: replies.map(map),
      sample_truncated: replies.length >= 50,
      checked_at: new Date().toISOString(),
    };
  }
  if (operation === "membership_verify") {
    const verified = await observedMember(client, entity);
    return {
      status: verified ? "joined" : "unknown",
      verified,
      discussion_id: String(entity.id),
      external_reference: "telegram:membership:" + entity.id,
    };
  }
  if (operation === "inspect") {
    const full = await client.invoke(
      new Api.channels.GetFullChannel({ channel: entity }),
      undefined,
      { maxRetryCount: 0 },
    );
    const pins = await client.getMessages(entity, {
      filter: new Api.InputMessagesFilterPinned(),
      limit: 100,
    });
    const recent = await client.getMessages(entity, { limit: 100 });
    const map = (m) => ({
      id: m.id,
      date: iso(m.date),
      text: (m.message || "").slice(0, 8000),
      reply_to: m.replyTo?.replyToMsgId || null,
      author_id: String(m.fromId?.userId || m.fromId?.channelId || ""),
      author_is_bot: typeof m.sender?.bot === "boolean" ? m.sender.bot : null,
      out: !!m.out,
      url: `https://t.me/${entity.username}/${m.id}`,
    });
    return {
      identity_checked: true,
      target: "@" + entity.username,
      discussion_id: String(entity.id),
      title: entity.title,
      is_group: !!entity.megagroup,
      is_channel: !!entity.broadcast,
      forum_topics_unchecked: !!entity.forum,
      about: full.fullChat.about,
      linked_discussion_id: full.fullChat.linkedChatId?.toString() || null,
      members: full.fullChat.participantsCount,
      pinned: pins.map(map),
      pinned_truncated: pins.length >= 100,
      recent: recent.map(map),
      recent_sample_truncated: recent.length >= 100,
      checked_at: new Date().toISOString(),
    };
  }
  if (operation === "verify") {
    const ids = input.message_ids;
    if (!Array.isArray(ids) || ids.length !== 1)
      return { status: "unknown", verified: false };
    const rows = await client.getMessages(entity, { ids });
    const message = rows[0];
    const verified =
      !!message?.out &&
      message.message === input.text &&
      (input.reply_to == null ||
        message.replyTo?.replyToMsgId === input.reply_to);
    return {
      status: verified ? "confirmed" : "unknown",
      verified,
      message_ids: ids,
      discussion_id: String(entity.id),
      external_reference: `telegram:${input.target}:${ids[0]}`,
    };
  }
  throw new Error("UnsupportedReadOperation");
}
export async function submitOperation(client, job, checkpoint, authorize) {
  const item = job.payload;
  const entity = await client.getEntity(item.target);
  if (!entity.username || !entity.megagroup)
    throw new Error("PublicDiscussionRequired");
  if (job.kind === "join") {
    if (await observedMember(client, entity))
      return {
        status: "already_joined",
        verified: true,
        discussion_id: String(entity.id),
        external_reference: "telegram:membership:" + entity.id,
      };
    await authorize();
    await checkpoint({
      status: "unknown",
      verified: false,
      submission_started: true,
    });
    try {
      await client.invoke(
        new Api.channels.JoinChannel({ channel: entity }),
        undefined,
        {
          maxRetryCount: 0,
        },
      );
    } catch (e) {
      if (e.errorMessage === "INVITE_REQUEST_SENT")
        return { status: "pending_admin_approval", verified: false };
      throw e;
    }
    const verified = await observedMember(client, entity);
    return {
      status: verified ? "joined" : "unknown",
      verified,
      discussion_id: String(entity.id),
      external_reference: "telegram:membership:" + entity.id,
    };
  }
  if (
    job.kind !== "send" ||
    !["reply", "send_message"].includes(item.action) ||
    (item.media || []).length
  )
    throw new Error("UnsupportedNativeAction");
  if (!(await observedMember(client, entity)))
    throw new Error("MembershipNotVerified");
  if (entity.defaultBannedRights?.sendMessages)
    throw new Error("TargetWriteForbidden");
  const context = job.workflow_context;
  if (
    !context?.question?.text ||
    context.question.message_id !== item.reply_to ||
    Date.now() - Date.parse(context.reviewed_at) > 900000
  )
    throw new Error("ReplyContextRequired");
  if (String(entity.id) !== context.question.discussion_id)
    throw new Error("ReplyContextChanged");
  const original = (
    await client.getMessages(entity, { ids: [item.reply_to] })
  )[0];
  if (!original || original.out || original.message !== context.question.text)
    throw new Error("ReplyContextChanged");
  if (
    !Number.isFinite(Number(original.date)) ||
    !Number.isFinite(Date.parse(context.question.published_at)) ||
    Math.abs(
      Number(original.date) * 1000 - Date.parse(context.question.published_at),
    ) > 1000
  )
    throw new Error("ReplyContextChanged");
  const replies = await client.getMessages(entity, {
    replyTo: item.reply_to,
    limit: 20,
  });
  if (
    Date.now() - Number(original.date) * 1000 > 259200000 &&
    (!context.discussion_continuing ||
      !replies.some((m) => Date.now() - Number(m.date) * 1000 <= 259200000))
  )
    throw new Error("ReplyContextChanged");
  if (
    replies.some(
      (m) =>
        m.id !== item.reply_to &&
        Number(m.date) * 1000 > Date.parse(context.reviewed_at),
    )
  )
    throw new Error("ReplyContextChanged");
  await authorize();
  await checkpoint({
    status: "unknown",
    verified: false,
    submission_started: true,
  });
  const sent = await client.sendMessage(entity, {
    message: item.text,
    replyTo: item.reply_to,
    parseMode: false,
    linkPreview: false,
  });
  const receipt = {
    status: "unknown",
    verified: false,
    message_ids: sent?.id ? [sent.id] : [],
    external_reference: `telegram:${item.target}:${sent?.id || ""}`,
  };
  await checkpoint(receipt);
  return await readOperation(client, "verify", { ...item, ...receipt });
}

export async function loginToken(client, env, observe = async () => {}) {
  async function step(phase, action, dc) {
    const started = Date.now();
    await observe({ phase, dc });
    try {
      const result = await action();
      await observe({
        phase,
        dc,
        event: "stage_completed",
        elapsed_ms: Date.now() - started,
      });
      return result;
    } catch (error) {
      try {
        await observe({
          phase,
          dc,
          event: "stage_failed",
          elapsed_ms: Date.now() - started,
          error,
        });
      } catch {}
      throw error;
    }
  }
  let result = await step(
    "export_login_token",
    () =>
      client.invoke(
        new Api.auth.ExportLoginToken({
          apiId: Number(env.TG_API_ID),
          apiHash: env.TG_API_HASH,
          exceptIds: [],
        }),
        undefined,
        { maxRetryCount: 0 },
      ),
    client.session?.dcId,
  );
  if (result instanceof Api.auth.LoginTokenMigrateTo) {
    await step(
      "migrate_login",
      () => client._switchDC(result.dcId),
      result.dcId,
    );
    result = await step(
      "import_login_token",
      () =>
        client.invoke(
          new Api.auth.ImportLoginToken({ token: result.token }),
          undefined,
          { maxRetryCount: 0 },
        ),
      result.dcId,
    );
  }
  await observe({
    phase:
      result instanceof Api.auth.LoginTokenSuccess
        ? "login_token_authorized"
        : "login_token_pending",
  });
  if (result instanceof Api.auth.LoginTokenSuccess)
    return { status: "authorized" };
  if (result instanceof Api.auth.LoginToken)
    return {
      status: "waiting_device_confirmation",
      qr_uri:
        "tg://login?token=" + Buffer.from(result.token).toString("base64url"),
      expires_at: iso(result.expires),
    };
  throw new Error("TelegramLoginUnavailable");
}
