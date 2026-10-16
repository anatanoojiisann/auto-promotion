// All mutations run synchronously inside one workspace's durable SQLite transaction.
export const PLATFORMS = new Set([
  "telegram",
  "x",
  "reddit",
  "instagram",
  "discord",
  "tiktok",
]);
export const POOLS = new Set([
  "telegram.mtproto",
  "x.playwright",
  "x.edge",
  "x.tabbit",
  "x.browser_use",
  "x.manual",
  "reddit.manual",
  "instagram.graph",
  "instagram.manual",
  "discord.bot",
  "tiktok.organic",
  ...Array.from(PLATFORMS, (p) => p + ".manual"),
  ...Array.from(PLATFORMS, (p) => "simulation." + p),
]);
const TERMINAL = new Set([
  "confirmed",
  "scheduled",
  "failed_known",
  "unknown",
  "manual_attention",
  "missed",
  "cancelled",
  "joined",
  "already_joined",
  "pending_admin_approval",
]);
const SCHEMA = `
 CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
 INSERT OR IGNORE INTO config VALUES(1,'{"global_stop":true,"live_enabled":false,"max_workers":6,"global_limit":{"daily":100,"hourly":20},"platform_workers":{},"pool_workers":{}}');
 CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,identity TEXT NOT NULL,platform TEXT NOT NULL,payload TEXT NOT NULL,next_at REAL NOT NULL DEFAULT 0,cooldown_until REAL NOT NULL DEFAULT 0,disabled INTEGER NOT NULL DEFAULT 0);
 CREATE INDEX IF NOT EXISTS account_identity ON accounts(identity);
 CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,state TEXT NOT NULL,phase TEXT NOT NULL,version TEXT NOT NULL,payload TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,reference_id TEXT NOT NULL,task_id TEXT NOT NULL,account_id TEXT NOT NULL,pool TEXT NOT NULL,version TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,available_at REAL NOT NULL,expires_at REAL NOT NULL,owner TEXT,lease_token TEXT,lease_until REAL NOT NULL DEFAULT 0,attempt_id TEXT,result TEXT NOT NULL DEFAULT '{}');
 CREATE INDEX IF NOT EXISTS jobs_due ON jobs(pool,state,available_at);
 CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner,state,lease_until);
 CREATE INDEX IF NOT EXISTS jobs_task ON jobs(task_id,kind,state);
 CREATE TABLE IF NOT EXISTS task_pacing(task_id TEXT PRIMARY KEY,next_at REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS snapshot_cursor(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,token_hash TEXT NOT NULL,pools TEXT NOT NULL,accounts TEXT NOT NULL,enabled INTEGER NOT NULL,last_seen REAL NOT NULL DEFAULT 0);
 CREATE TABLE IF NOT EXISTS quota_events(attempt_id TEXT NOT NULL,scope TEXT NOT NULL,scope_key TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(attempt_id,scope,scope_key));
 CREATE INDEX IF NOT EXISTS quota_window ON quota_events(scope,scope_key,at);
 CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,kind TEXT NOT NULL,job_id TEXT,details TEXT NOT NULL);
`;
export class ControlError extends Error {
  constructor(code, status = 409) {
    super(code);
    this.status = status;
  }
}
export function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object")
    return (
      "{" +
      Object.keys(value)
        .sort()
        .map((k) => JSON.stringify(k) + ":" + canonical(value[k]))
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
export async function sha(value) {
  return [
    ...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
    ),
  ]
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
}
function safePayload(value) {
  if (value && typeof value === "object")
    for (const [k, v] of Object.entries(value)) {
      if (
        /^(credentials?|cookies?|session_string|api_hash|api_key|access_token|refresh_token|operator_token|node_token|phone|password)$/i.test(
          k,
        )
      )
        throw new ControlError("CredentialInSnapshot", 400);
      safePayload(v);
    }
}
export class ControlKernel {
  constructor(
    storage,
    { clock = () => Date.now() / 1000, uuid = () => crypto.randomUUID() } = {},
  ) {
    this.storage = storage;
    this.sql = storage.sql;
    this.clock = clock;
    this.uuid = uuid;
    this.sql.exec(SCHEMA);
  }
  rows(q, ...args) {
    return this.sql.exec(q, ...args).toArray();
  }
  one(q, ...args) {
    return this.rows(q, ...args)[0];
  }
  tx(fn) {
    return this.storage.transactionSync(fn);
  }
  event(kind, id, details = {}) {
    this.sql.exec(
      "INSERT INTO audit(at,kind,job_id,details) VALUES(?,?,?,?)",
      this.clock(),
      kind,
      id,
      canonical(details),
    );
  }
  settings() {
    return JSON.parse(
      this.one("SELECT payload FROM config WHERE id=1").payload,
    );
  }
  configure(input) {
    const prior = this.settings();
    const next = { ...prior, ...input };
    if (
      typeof next.global_stop !== "boolean" ||
      typeof next.live_enabled !== "boolean" ||
      !Number.isInteger(next.max_workers) ||
      next.max_workers < 1 ||
      next.max_workers > 50
    )
      throw new ControlError("InvalidSettings", 400);
    if (
      !next.global_limit ||
      ![next.global_limit.daily, next.global_limit.hourly].every(
        (n) => Number.isInteger(n) && n >= 1,
      )
    )
      throw new ControlError("InvalidQuota", 400);
    for (const [key, n] of Object.entries(next.pool_workers || {}))
      if (!POOLS.has(key) || !Number.isInteger(n) || n < 1 || n > 50)
        throw new ControlError("InvalidPool", 400);
    for (const [key, n] of Object.entries(next.platform_workers || {}))
      if (!PLATFORMS.has(key) || !Number.isInteger(n) || n < 1 || n > 50)
        throw new ControlError("InvalidPlatform", 400);
    for (const [key, limit] of Object.entries(next.platform_limits || {}))
      if (
        !PLATFORMS.has(key) ||
        !limit ||
        ![limit.daily, limit.hourly].every((n) => Number.isInteger(n) && n >= 1)
      )
        throw new ControlError("InvalidQuota", 400);
    return this.tx(() => {
      this.sql.exec("UPDATE config SET payload=? WHERE id=1", canonical(next));
      this.event("settings_changed", null, {
        global_stop: next.global_stop,
        live_enabled: next.live_enabled,
      });
      return next;
    });
  }
  registerNode({ id, token_hash, pools, account_ids }) {
    if (
      !/^[a-zA-Z0-9_.-]{1,100}$/.test(id) ||
      !/^[a-f0-9]{64}$/.test(token_hash) ||
      !Array.isArray(pools) ||
      !pools.length ||
      pools.some((p) => !POOLS.has(p)) ||
      !Array.isArray(account_ids) ||
      !account_ids.length
    )
      throw new ControlError("InvalidNode", 400);
    return this.tx(() => {
      this.sql.exec(
        "INSERT INTO nodes VALUES(?,?,?,?,1,0) ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash,pools=excluded.pools,accounts=excluded.accounts,enabled=1",
        id,
        token_hash,
        canonical(pools),
        canonical(account_ids),
      );
      this.event("node_registered", null, { node_id: id, pools });
      return { id, pools, account_ids };
    });
  }
  node(id, tokenHash) {
    const n = this.one("SELECT * FROM nodes WHERE id=?", id);
    if (!n || !n.enabled || n.token_hash !== tokenHash)
      throw new ControlError("NodeUnauthorized", 401);
    this.sql.exec("UPDATE nodes SET last_seen=? WHERE id=?", this.clock(), id);
    return {
      ...n,
      pools: JSON.parse(n.pools),
      accounts: JSON.parse(n.accounts),
    };
  }
  sync(snapshot) {
    safePayload(snapshot);
    if (
      !Array.isArray(snapshot.accounts) ||
      !Array.isArray(snapshot.tasks) ||
      !Array.isArray(snapshot.jobs) ||
      snapshot.jobs.length > 1000
    )
      throw new ControlError("InvalidSnapshot", 400);
    return this.tx(() => {
      const cursor = this.one(
        "SELECT revision FROM snapshot_cursor WHERE id=1",
      );
      if (snapshot.revision !== undefined) {
        if (!Number.isSafeInteger(snapshot.revision) || snapshot.revision < 1)
          throw new ControlError("InvalidSnapshotRevision", 400);
        if (cursor && snapshot.revision < cursor.revision)
          return { stale: true };
        this.sql.exec(
          "INSERT INTO snapshot_cursor VALUES(1,?) ON CONFLICT(id) DO UPDATE SET revision=MAX(revision,excluded.revision)",
          snapshot.revision,
        );
      } else if (cursor)
        throw new ControlError("SnapshotRevisionRequired", 400);
      for (const a of snapshot.accounts) {
        if (!PLATFORMS.has(a.platform) || !a.id || !a.identity || !a.limits)
          throw new ControlError("InvalidAccount", 400);
        const prior = this.one(
          "SELECT identity FROM accounts WHERE id=?",
          a.id,
        );
        if (prior && prior.identity !== a.identity)
          throw new ControlError("AccountIdentityChanged");
        this.sql.exec(
          "INSERT INTO accounts VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,next_at=MAX(accounts.next_at,excluded.next_at),cooldown_until=MAX(accounts.cooldown_until,excluded.cooldown_until),disabled=MAX(accounts.disabled,excluded.disabled)",
          a.id,
          a.identity,
          a.platform,
          canonical(a),
          a.next_at || 0,
          a.cooldown_until || 0,
          a.disabled ? 1 : 0,
        );
      }
      for (const t of snapshot.tasks) {
        if (!t.id || !t.policy || !t.version || !Array.isArray(t.account_ids))
          throw new ControlError("InvalidTask", 400);
        const old = this.one("SELECT * FROM tasks WHERE id=?", t.id);
        const frozen = old && JSON.parse(old.payload).manifest_hash;
        if (frozen && (frozen !== t.manifest_hash || old.version !== t.version))
          throw new ControlError("FrozenTaskChanged");
        if (
          old &&
          old.version !== t.version &&
          ["active", "completed"].includes(old.state)
        )
          throw new ControlError("FrozenTaskChanged");
        this.sql.exec(
          "INSERT INTO tasks VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,phase=excluded.phase,version=excluded.version,payload=excluded.payload",
          t.id,
          t.state,
          t.phase,
          t.version,
          canonical(t),
        );
      }
      for (const j of snapshot.jobs) {
        if (
          !j.id ||
          !POOLS.has(j.pool) ||
          !["send", "join"].includes(j.kind) ||
          !j.version ||
          !Number.isFinite(j.available_at) ||
          !Number.isFinite(j.expires_at) ||
          !this.one("SELECT id FROM tasks WHERE id=?", j.task_id) ||
          !this.one("SELECT id FROM accounts WHERE id=?", j.account_id)
        )
          throw new ControlError("InvalidJob", 400);
        const prior = this.one("SELECT * FROM jobs WHERE id=?", j.id);
        if (
          prior &&
          (prior.version !== j.version ||
            prior.account_id !== j.account_id ||
            prior.task_id !== j.task_id ||
            prior.reference_id !== j.reference_id ||
            prior.pool !== j.pool)
        )
          throw new ControlError("FrozenJobChanged");
        const state = TERMINAL.has(j.state)
          ? j.state
          : j.attempt_id
            ? "unknown"
            : "pending";
        if (!prior)
          this.sql.exec(
            "INSERT INTO jobs(id,kind,reference_id,task_id,account_id,pool,version,payload,state,available_at,expires_at,attempt_id,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            j.id,
            j.kind,
            j.reference_id,
            j.task_id,
            j.account_id,
            j.pool,
            j.version,
            canonical(j),
            state,
            j.available_at,
            j.expires_at,
            j.attempt_id || null,
            canonical(j.result || {}),
          );
        else if (
          (prior.state === "pending" && TERMINAL.has(j.state)) ||
          (["unknown", "scheduled", "pending_admin_approval"].includes(
            prior.state,
          ) &&
            j.attempt_id &&
            j.attempt_id === prior.attempt_id &&
            ["confirmed", "scheduled", "joined", "already_joined"].includes(
              j.state,
            ) &&
            j.result?.verified &&
            j.result?.external_reference)
        )
          this.sql.exec(
            "UPDATE jobs SET state=?,attempt_id=COALESCE(attempt_id,?),result=? WHERE id=?",
            state,
            j.attempt_id || null,
            canonical(j.result || {}),
            j.id,
          );
      }
      for (const e of snapshot.quota_events || [])
        this.sql.exec(
          "INSERT OR IGNORE INTO quota_events VALUES(?,?,?,?)",
          e.attempt_id,
          e.scope,
          e.key,
          e.at,
        );
      return { tasks: snapshot.tasks.length, jobs: snapshot.jobs.length };
    });
  }
  sweep() {
    return this.tx(() => {
      const now = this.clock();
      for (const j of this.rows(
        "SELECT id FROM jobs WHERE state='leased' AND lease_until<=?",
        now,
      )) {
        this.sql.exec(
          "UPDATE jobs SET state='unknown',result=? WHERE id=?",
          canonical({ reason: "executor_lease_expired", verified: false }),
          j.id,
        );
        this.event("job_unknown", j.id, { reason: "executor_lease_expired" });
      }
      this.sql.exec(
        "UPDATE jobs SET state='missed',result=? WHERE state='pending' AND expires_at<=?",
        canonical({ reason: "delivery_window_elapsed" }),
        now,
      );
    });
  }
  claim(node, pool, expectedId = null) {
    if (!node.pools.includes(pool))
      throw new ControlError("PoolNotAssigned", 403);
    this.sweep();
    return this.tx(() => {
      const config = this.settings(),
        now = this.clock();
      if (config.global_stop) return null;
      const active = this.rows(
        "SELECT j.*,a.identity,a.platform FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE j.state='leased' AND j.lease_until>?",
        now,
      );
      if (
        active.length >= config.max_workers ||
        active.filter((j) => j.pool === pool).length >=
          (config.pool_workers?.[pool] || 1)
      )
        return null;
      const rows = this.rows(
        "SELECT j.*,a.identity,a.platform,a.next_at,a.cooldown_until,a.disabled,a.payload account_payload,t.payload task_payload FROM jobs j JOIN accounts a ON a.id=j.account_id JOIN tasks t ON t.id=j.task_id WHERE j.pool=? AND j.state='pending' AND j.available_at<=? AND j.expires_at>? AND t.state='active' AND (? IS NULL OR j.id=?) ORDER BY a.next_at,j.available_at,COALESCE(json_extract(j.payload,'$.position'),0),j.id LIMIT 128",
        pool,
        now,
        now,
        expectedId,
        expectedId,
      );
      for (const j of rows) {
        const data = JSON.parse(j.payload),
          task = JSON.parse(j.task_payload),
          account = JSON.parse(j.account_payload);
        const identityState = this.one(
          "SELECT MAX(next_at) next_at,MAX(cooldown_until) cooldown_until,MAX(disabled) disabled FROM accounts WHERE identity=?",
          j.identity,
        );
        if (
          !node.accounts.includes(j.account_id) ||
          identityState.disabled ||
          identityState.cooldown_until > now ||
          identityState.next_at > now ||
          j.attempt_id
        )
          continue;
        const manual =
          data.payload?.execution_mode === "manual" ||
          data.action === "manual_comment";
        if (account.mode === "live" && !config.live_enabled && !manual)
          continue;
        if (j.kind === "send" && task.phase !== "ready") continue;
        if (
          task.policy.pacing_scope !== "account" &&
          (this.one(
            "SELECT next_at FROM task_pacing WHERE task_id=?",
            j.task_id,
          )?.next_at || 0) > now
        )
          continue;
        if (j.kind === "send") {
          const earlier = this.rows(
            "SELECT account_id,state,payload FROM jobs WHERE task_id=? AND kind='send' AND id<>?",
            j.task_id,
            j.id,
          );
          if (
            earlier.some(
              (e) =>
                (task.policy.ordering_mode !== "independent_accounts" ||
                  e.account_id === j.account_id) &&
                Number(JSON.parse(e.payload).position ?? -1) <
                  Number(data.position ?? -1) &&
                !TERMINAL.has(e.state),
            )
          )
            continue;
        }
        if (
          now < Date.parse(task.policy.starts_at) / 1000 ||
          now >= Date.parse(task.policy.ends_at) / 1000
        )
          continue;
        if (
          active.some(
            (a) =>
              a.identity === j.identity ||
              (a.owner === node.id &&
                data.resource &&
                JSON.parse(a.payload).resource === data.resource),
          )
        )
          continue;
        if (
          active.filter((a) => a.platform === j.platform).length >=
          (config.platform_workers?.[j.platform] || config.max_workers)
        )
          continue;
        const token = this.uuid(),
          until = now + 180;
        this.sql.exec(
          "UPDATE jobs SET state='leased',owner=?,lease_token=?,lease_until=? WHERE id=?",
          node.id,
          token,
          until,
          j.id,
        );
        this.event("job_leased", j.id, { node_id: node.id, pool });
        return { ...data, lease_token: token, lease_until: until };
      }
      return null;
    });
  }
  leased(node, id, token, { late = false } = {}) {
    const j = this.one("SELECT * FROM jobs WHERE id=?", id);
    if (
      !j ||
      j.owner !== node.id ||
      j.lease_token !== token ||
      !node.accounts.includes(j.account_id) ||
      !node.pools.includes(j.pool)
    )
      throw new ControlError("InvalidLease", 403);
    if (!late && (j.state !== "leased" || j.lease_until <= this.clock()))
      throw new ControlError("LeaseExpired");
    return j;
  }
  renew(node, id, token) {
    return this.tx(() => {
      this.leased(node, id, token);
      this.sql.exec(
        "UPDATE jobs SET lease_until=? WHERE id=?",
        this.clock() + 180,
        id,
      );
      return { renewed: true };
    });
  }
  authorize(node, id, token, attemptId) {
    return this.tx(() => {
      const j = this.leased(node, id, token),
        now = this.clock(),
        config = this.settings();
      const a = this.one("SELECT * FROM accounts WHERE id=?", j.account_id),
        account = JSON.parse(a.payload),
        task = JSON.parse(
          this.one("SELECT payload FROM tasks WHERE id=?", j.task_id).payload,
        ),
        data = JSON.parse(j.payload);
      if (
        data.payload?.execution_mode === "manual" ||
        data.action === "manual_comment"
      )
        throw new ControlError("ManualActionCannotPublish", 403);
      const identityState = this.one(
        "SELECT MAX(cooldown_until) cooldown_until,MAX(disabled) disabled FROM accounts WHERE identity=?",
        a.identity,
      );
      if (
        config.global_stop ||
        task.state !== "active" ||
        (account.mode === "live" && !config.live_enabled) ||
        identityState.disabled ||
        identityState.cooldown_until > now
      )
        throw new ControlError("ExecutionStopped");
      if (
        account.mode === "live" &&
        !(account.allowed_actions || []).includes(data.action)
      )
        throw new ControlError("ActionNotEnabled", 403);
      if (j.expires_at <= now || now >= Date.parse(task.policy.ends_at) / 1000)
        throw new ControlError("ExecutionWindowElapsed");
      if (!attemptId || typeof attemptId !== "string")
        throw new ControlError("AttemptRequired", 400);
      if (j.attempt_id) {
        if (j.attempt_id !== attemptId)
          throw new ControlError("AttemptAlreadyRecorded");
        return { allowed: true };
      }
      if (
        task.policy.pacing_scope !== "account" &&
        (this.one("SELECT next_at FROM task_pacing WHERE task_id=?", j.task_id)
          ?.next_at || 0) > now
      )
        throw new ControlError("PacingWait");
      if (j.kind === "join") {
        if (
          this.one(
            "SELECT COUNT(*) n FROM jobs WHERE task_id=? AND kind='join' AND attempt_id IS NOT NULL",
            j.task_id,
          ).n >= task.policy.max_joins
        )
          throw new ControlError("JoinLimitReached");
        if (
          this.one(
            "SELECT j.id FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE j.kind='join' AND a.identity=? AND j.attempt_id IS NOT NULL AND lower(json_extract(j.payload,'$.payload.target'))=lower(?) LIMIT 1",
            a.identity,
            data.payload?.target,
          )
        )
          throw new ControlError("JoinAlreadyAttempted");
      }
      const scopes = [...(data.scopes || [])];
      scopes.push({
        scope: "platform",
        key: canonical([account.mode, a.platform]),
        limit: config.platform_limits?.[a.platform] || {
          daily: 100000,
          hourly: 10000,
        },
      });
      scopes.push({
        scope: "cloud_global",
        key: account.mode,
        limit: config.global_limit,
      });
      scopes.push({
        scope: "cloud_account",
        key: a.identity,
        limit: account.limits,
      });
      scopes.push({
        scope: "cloud_task",
        key: j.task_id,
        limit: {
          daily: task.policy.daily_limit,
          hourly: 100000,
        },
      });
      const day = Math.floor(now / 86400) * 86400;
      for (const s of scopes) {
        const daily = this.one(
          "SELECT COUNT(*) n FROM quota_events WHERE scope=? AND scope_key=? AND at>=? AND attempt_id<>?",
          s.scope,
          s.key,
          day,
          attemptId,
        ).n;
        const hourly = this.one(
          "SELECT COUNT(*) n FROM quota_events WHERE scope=? AND scope_key=? AND at>? AND attempt_id<>?",
          s.scope,
          s.key,
          now - 3600,
          attemptId,
        ).n;
        if (daily >= s.limit.daily || hourly >= s.limit.hourly)
          throw new ControlError("QuotaExceeded");
      }
      this.sql.exec(
        "UPDATE jobs SET attempt_id=? WHERE id=? AND attempt_id IS NULL",
        attemptId,
        id,
      );
      for (const s of scopes)
        this.sql.exec(
          "INSERT OR IGNORE INTO quota_events VALUES(?,?,?,?)",
          attemptId,
          s.scope,
          s.key,
          now,
        );
      this.sql.exec(
        "UPDATE accounts SET next_at=MAX(next_at,?) WHERE identity=?",
        now +
          Math.max(account.min_interval || 0, task.policy.min_interval || 0),
        a.identity,
      );
      if (task.policy.pacing_scope !== "account")
        this.sql.exec(
          "INSERT INTO task_pacing VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)",
          j.task_id,
          now + (task.policy.min_interval || 0),
        );
      this.event("attempt_registered", id, { attempt_id: attemptId });
      return { allowed: true };
    });
  }
  complete(node, id, token, { state, result = {}, attempt_id = null }) {
    if (!TERMINAL.has(state) && state !== "pending")
      throw new ControlError("InvalidOutcome", 400);
    safePayload(result);
    return this.tx(() => {
      const j = this.leased(node, id, token, { late: true });
      if (
        TERMINAL.has(j.state) &&
        !["unknown", "scheduled", "pending_admin_approval"].includes(j.state)
      )
        return { state: j.state };
      if (j.attempt_id && attempt_id !== j.attempt_id)
        throw new ControlError("AttemptMismatch");
      if (
        state === "pending" &&
        (j.attempt_id || j.state === "unknown" || attempt_id)
      )
        throw new ControlError("AttemptCannotReplay");
      if (
        ["confirmed", "scheduled", "joined", "already_joined"].includes(
          state,
        ) &&
        (!result.verified || !result.external_reference)
      )
        throw new ControlError("VerifiedReceiptRequired");
      if (
        ["confirmed", "scheduled", "joined", "already_joined"].includes(
          state,
        ) &&
        !j.attempt_id
      )
        throw new ControlError("AttemptNotAuthorized");
      if (
        state === "scheduled" &&
        (!result.scheduled ||
          !result.active ||
          !Array.isArray(result.message_ids) ||
          !result.message_ids.length ||
          result.message_ids.some(
            (id) =>
              !(result.lookup_message_ids || [])
                .map(String)
                .includes(String(id)),
          ))
      )
        throw new ControlError("VerifiedScheduleRequired");
      const normalized = { ...result, status: state };
      this.sql.exec(
        "UPDATE jobs SET state=?,result=?,attempt_id=COALESCE(attempt_id,?),available_at=?,lease_until=0 WHERE id=?",
        state,
        canonical(normalized),
        attempt_id,
        Math.max(this.clock() + 5, Number(result.next_at) || 0),
        id,
      );
      if (state === "pending")
        this.sql.exec(
          "UPDATE jobs SET owner=NULL,lease_token=NULL WHERE id=?",
          id,
        );
      if (result.rate_limited || result.risk_warning)
        this.sql.exec(
          "UPDATE accounts SET cooldown_until=MAX(cooldown_until,?),disabled=MAX(disabled,?) WHERE identity=(SELECT identity FROM accounts WHERE id=?)",
          this.clock() +
            Math.max(1, Number(result.retry_after_seconds) || 3600),
          result.risk_warning ? 1 : 0,
          j.account_id,
        );
      this.event("job_" + state, id, { verified: !!result.verified });
      return { state };
    });
  }
  status() {
    this.sweep();
    return {
      settings: this.settings(),
      jobs: this.rows(
        "SELECT pool,state,COUNT(*) count,MIN(available_at) oldest FROM jobs GROUP BY pool,state",
      ),
      nodes: this.rows("SELECT id,pools,accounts,enabled,last_seen FROM nodes"),
      unknown: this.rows(
        "SELECT id,task_id,reference_id,pool,attempt_id,result FROM jobs WHERE state='unknown' LIMIT 100",
      ),
    };
  }
}
