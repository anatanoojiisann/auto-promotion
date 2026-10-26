import { Api } from "teleproto";

const DEFAULTS = {
  requests_per_window: 120,
  window_seconds: 3600,
  scan_rpc_limit: 40,
  messages_per_page: 100,
  groups_per_scan: 10,
  cache_ttl_seconds: 300,
};
export function readPolicy(config = {}, requested = {}) {
  const out = {};
  for (const [key, fallback] of Object.entries(DEFAULTS)) {
    const configured = Number(config[key]);
    const minimum = key === "cache_ttl_seconds" ? 0 : 1;
    const cap =
      Number.isSafeInteger(configured) && configured >= minimum
        ? configured
        : fallback;
    const value = Number(requested[key]);
    out[key] =
      Number.isSafeInteger(value) && value >= minimum
        ? Math.min(cap, value)
        : cap;
  }
  // Callers cannot change the account's durable time bucket to reset its budget.
  const window = Number(config.window_seconds);
  out.window_seconds =
    Number.isSafeInteger(window) && window >= 60
      ? Math.min(86400, window)
      : DEFAULTS.window_seconds;
  out.messages_per_page = Math.min(100, out.messages_per_page);
  out.groups_per_scan = Math.min(100, out.groups_per_scan);
  return out;
}
export class TelegramReadBudgetExceeded extends Error {
  constructor(retryAfter) {
    super("TelegramReadBudgetExceeded");
    this.retryAfter = retryAfter;
  }
}
function plainMessage(m) {
  return {
    id: m.id,
    message: m.message || "",
    date: Number(m.date || 0),
    out: !!m.out,
    fromId: {
      userId: m.fromId?.userId?.toString(),
      channelId: m.fromId?.channelId?.toString(),
    },
    replyTo: { replyToMsgId: m.replyTo?.replyToMsgId },
    sender: { bot: m.sender?.bot },
  };
}
function entityRecord(e) {
  if (e.className !== "Channel" || !e.id || !e.accessHash) return null;
  return {
    id: String(e.id),
    accessHash: String(e.accessHash),
    username: e.username,
    title: e.title || "",
    megagroup: !!e.megagroup,
    broadcast: !!e.broadcast,
    forum: !!e.forum,
  };
}
function restoreEntity(e) {
  return new Api.Channel({
    ...e,
    id: BigInt(e.id),
    accessHash: BigInt(e.accessHash),
    photo: new Api.ChatPhotoEmpty(),
    date: 0,
  });
}
export class TelegramReadPolicy {
  constructor(
    store,
    {
      accountId,
      identity,
      revision = 0,
      policy = {},
      requested = {},
      clock = () => Date.now() / 1000,
    } = {},
  ) {
    this.store = store;
    this.clock = clock;
    this.policy = readPolicy(policy, requested);
    this.account = accountId;
    this.namespace = JSON.stringify([accountId, identity, revision]);
    this.counts = {};
    this.fresh = 0;
    this.hits = 0;
    this.observed = [];
    this.complete = true;
    this.sampleTruncated = false;
    this.scanned = {};
    this.sampleLimits = {};
    this.resultScope = "mandatory_authorization_or_receipt_read";
    this.category = "identity";
    this.freshSamples = new Set();
    this.operationReservations = 0;
    this.cacheSkipped = 0;
  }
  key(kind, value) {
    return "read:" + JSON.stringify([this.namespace, kind, value]);
  }
  async reserve(category) {
    if (this.operationReservations >= this.policy.scan_rpc_limit)
      throw this.budgetError(1);
    // Synchronous operation reservation also fences concurrent SDK invokes
    // while their account transactions are still awaiting storage.
    this.operationReservations++;
    const now = this.clock(),
      seconds = this.policy.window_seconds;
    const key = "read-budget:" + this.account;
    const reserve = async (store) => {
      let row = await store.get(key);
      if (
        row &&
        (!Number.isSafeInteger(row.used) ||
          row.used < 0 ||
          !Number.isSafeInteger(row.limit) ||
          row.limit < 1 ||
          !Number.isFinite(row.until))
      )
        throw new Error("TelegramReadBudgetInvalid");
      if (!row || row.until <= now)
        row = {
          used: 0,
          limit: this.policy.requests_per_window,
          until: (Math.floor(now / seconds) + 1) * seconds,
        };
      row.limit = Math.min(row.limit, this.policy.requests_per_window);
      this.lastBudget = { ...row };
      if (row.used >= row.limit)
        throw this.budgetError(Math.max(1, Math.ceil(row.until - now)));
      row.used++;
      await store.put(key, row); // Reserve durably before invoking, including failed attempts.
      this.lastBudget = { ...row };
    };
    if (typeof this.store.transaction !== "function")
      throw new Error("DurableReadBudgetStoreRequired");
    await this.store.transaction(reserve);
    this.counts[category] = (this.counts[category] || 0) + 1;
  }
  budgetError(retry) {
    const error = new TelegramReadBudgetExceeded(retry);
    error.mutationStarted = (this.counts.mutation || 0) > 0;
    error.readDiagnostics = this.diagnostics(true);
    return error;
  }
  get total() {
    return Object.entries(this.counts).reduce(
      (sum, [category, count]) => (category === "mutation" ? sum : sum + count),
      0,
    );
  }
  instrument(client) {
    this.original = {
      invoke: client.invoke,
      getEntity: client.getEntity,
      getMessages: client.getMessages,
    };
    const read = this;
    client.invoke = async function (request, ...args) {
      const name = request?.className || "unknown";
      // Exact mutation types supported by this executor; no wildcard exemption.
      if (["messages.SendMessage", "channels.JoinChannel"].includes(name)) {
        read.counts.mutation = (read.counts.mutation || 0) + 1;
        return read.original.invoke.call(this, request, ...args);
      }
      const category =
        read.category === "identity"
          ? "identity"
          : /contacts.Search/.test(name)
            ? "directory"
            : /GetFullChannel/.test(name)
              ? "metadata"
              : /GetParticipant/.test(name)
                ? "membership"
                : /GetHistory|GetReplies|Search/.test(name)
                  ? "history"
                  : /ResolveUsername|GetChannels|GetChats/.test(name)
                    ? "entity"
                    : "other";
      await read.reserve(category);
      return read.original.invoke.call(this, request, ...args);
    };
  }
  restore(client) {
    if (this.original) Object.assign(client, this.original);
  }
  async cache(kind, value, fetch) {
    const key = this.key(kind, value),
      cached = await this.store.get(key),
      now = this.clock();
    if (
      cached &&
      now >= cached.at &&
      now - cached.at < this.policy.cache_ttl_seconds
    ) {
      this.hits++;
      this.observed.push(cached.at);
      return structuredClone(cached.value);
    }
    const result = await fetch();
    // Optional samples can exceed Durable Object's per-value size limit.
    // Keep the fresh observation available without pretending it was cached.
    const size = new TextEncoder().encode(JSON.stringify(result)).length;
    if (size <= 96000) await this.store.put(key, { at: now, value: result });
    else this.cacheSkipped++;
    this.fresh++;
    this.observed.push(now);
    return result;
  }
  async sample(client, entity, options, stream) {
    const limit = Math.min(options.limit || 100, this.policy.messages_per_page);
    const scope = [
      String(entity.id),
      stream,
      options.filter?.className || null,
      JSON.stringify({ ...options, limit }, (_, value) =>
        typeof value === "bigint" ? String(value) : value,
      ),
    ];
    const before = this.fresh;
    const rows = await this.cache("sample", scope, async () => {
      const rows = await this.original.getMessages.call(client, entity, {
        ...options,
        limit,
      });
      if (
        !Array.isArray(rows) ||
        rows.length > limit ||
        rows.some(
          (m, i) =>
            !Number.isSafeInteger(m?.id) ||
            m.id <= 0 ||
            !Number.isFinite(Number(m.date)) ||
            (i > 0 && rows[i - 1].id <= m.id),
        )
      )
        throw new Error("TelegramReadMalformedPage");
      return rows.map(plainMessage);
    });
    this.sampleLimits[stream] = limit;
    this.scanned[stream] = rows.length;
    this.sampleTruncated ||= rows.length >= limit;
    if (this.fresh > before)
      for (const message of rows)
        this.freshSamples.add(stream + ":" + message.id);
    return rows;
  }
  diagnostics(incomplete = false) {
    const observed = this.observed.length
      ? Math.min(...this.observed)
      : this.clock();
    return {
      read_state:
        incomplete || !this.complete
          ? "incomplete"
          : this.fresh
            ? "fresh"
            : "cache_hit",
      read_complete: !(incomplete || !this.complete),
      read_observed_at: new Date(observed * 1000).toISOString(),
      read_cursor_next: null,
      rpc_counts: this.counts,
      rpc_count_method: "logical_client_invoke_attempts",
      read_rpc_network_attempts: null,
      read_rpc_retries: null,
      read_budget_scope: "cloud_executor_authority",
      read_policy_effective: { ...this.policy },
      read_budget_state: this.lastBudget || null,
      read_result_scope: this.resultScope,
      read_cursor_scope:
        this.resultScope === "bounded_observation_sample"
          ? "bounded_sample_no_consumption_cursor"
          : "none",
      read_sample_truncated: this.sampleTruncated,
      read_sample_messages: this.scanned,
      read_sample_limits: this.sampleLimits,
      ...(this.incompleteReason
        ? { read_incomplete_reason: this.incompleteReason }
        : {}),
      read_cache_hits: this.hits,
      read_cache_skipped_oversize: this.cacheSkipped,
      read_fresh_samples: this.freshSamples.size,
      read_fresh_sources: this.fresh,
    };
  }
  async execute(client, operation, input, action) {
    const optional = ["discover", "inspect", "read_context"].includes(
      operation,
    );
    this.resultScope = optional
      ? "bounded_observation_sample"
      : "mandatory_authorization_or_receipt_read";
    this.category = "read";
    if (!optional) {
      this.fresh++;
      this.observed.push(this.clock());
    }
    if (optional) {
      client.getEntity = async (target) => {
        const value = await this.cache(
          "entity",
          String(target).toLowerCase(),
          async () => {
            const entity = await this.original.getEntity.call(client, target);
            return (
              entityRecord(entity) || {
                unsupported: {
                  username: entity.username,
                  className: entity.className,
                },
              }
            );
          },
        );
        if (value.unsupported) return value.unsupported;
        return restoreEntity(value);
      };
      const invoke = client.invoke;
      client.invoke = async (request, ...args) =>
        request?.className === "channels.GetFullChannel"
          ? this.cache("metadata", String(request.channel.id), async () => {
              const full = await invoke.call(client, request, ...args);
              return {
                fullChat: {
                  about: full.fullChat.about,
                  participantsCount: full.fullChat.participantsCount,
                  linkedChatId: full.fullChat.linkedChatId?.toString() || null,
                },
              };
            })
          : invoke.call(client, request, ...args);
      client.getMessages = async (entity, options) => {
        if (options.ids)
          return operation === "read_context"
            ? this.cache("source", [String(entity.id), options.ids], () =>
                this.original.getMessages
                  .call(client, entity, options)
                  .then((rows) => rows.map(plainMessage)),
              )
            : this.original.getMessages.call(client, entity, options);
        return this.sample(
          client,
          entity,
          options,
          options.filter
            ? "pins"
            : options.replyTo
              ? "replies:" + options.replyTo
              : "recent",
        );
      };
    }
    try {
      const result =
        operation === "discover"
          ? await this.cache("directory", String(input.query || ""), action)
          : await action();
      const diagnostics = this.diagnostics();
      return {
        ...result,
        ...diagnostics,
        checked_at: diagnostics.read_observed_at,
        ...(this.sampleLimits.pins != null
          ? {
              pinned_truncated: result.pinned?.length >= this.sampleLimits.pins,
            }
          : {}),
        ...(this.sampleLimits.recent != null
          ? {
              recent_sample_truncated:
                result.recent?.length >= this.sampleLimits.recent,
            }
          : {}),
        ...(operation === "read_context"
          ? {
              sample_truncated:
                result.replies?.length >=
                (this.sampleLimits["replies:" + input.message_id] || 50),
            }
          : {}),
      };
    } catch (error) {
      if (error?.message === "TelegramReadMalformedPage") {
        this.incompleteReason = "TelegramReadMalformedPage";
        return {
          status: "incomplete",
          reason: this.incompleteReason,
          ...this.diagnostics(true),
        };
      }
      if (!(error instanceof TelegramReadBudgetExceeded)) throw error;
      return {
        status: "incomplete",
        reason: "TelegramReadBudgetExceeded",
        cooldown_source: "local_read_budget",
        retry_after_seconds: error.retryAfter,
        ...this.diagnostics(true),
      };
    }
  }
}
