import { ControlError, canonical, sha } from "./kernel.mjs";
import { controlProof } from "./offline-permit.mjs";
import { normalizeTelegramTarget } from "./telegram-target.mjs";
const fail = (code, status = 403) => {
  throw new ControlError(code, status);
};
const id = (v) => typeof v === "string" && /^[a-zA-Z0-9_-]{16,80}$/.test(v);
export class DeviceControl {
  constructor(k) {
    this.k = k;
    k.sql
      .exec(`CREATE TABLE IF NOT EXISTS control_devices(id TEXT PRIMARY KEY,store_id TEXT NOT NULL UNIQUE,token_hash TEXT NOT NULL,scope TEXT NOT NULL,expires_at REAL NOT NULL,enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS device_tasks(plan_id TEXT PRIMARY KEY,device_id TEXT NOT NULL,request_id TEXT NOT NULL,request_hash TEXT NOT NULL,task_id TEXT NOT NULL UNIQUE,job_id TEXT NOT NULL UNIQUE,UNIQUE(device_id,request_id));`);
  }
  register(b) {
    if (
      !id(b.node_id) ||
      !id(b.store_id) ||
      !/^([a-f0-9]{64})$/.test(b.token_hash || "") ||
      !Number.isFinite(b.expires_at) ||
      b.expires_at <= this.k.clock() ||
      b.expires_at > this.k.clock() + 86400 * 30
    )
      fail("InvalidDeviceRegistration", 400);
    const s = b.scope;
    if (
      !s ||
      !Array.isArray(s.account_ids) ||
      s.account_ids.length !== 1 ||
      !Array.isArray(s.targets) ||
      s.targets.length !== 1 ||
      s.action !== "send_text" ||
      s.max_messages !== 1
    )
      fail("InvalidDeviceScope", 400);
    if (normalizeTelegramTarget(s.targets[0]) !== s.targets[0])
      fail("CanonicalTargetRequired", 400);
    const a = this.k.one(
      "SELECT platform FROM accounts WHERE id=?",
      s.account_ids[0],
    );
    if (a?.platform !== "telegram") fail("UnknownDeviceAccount", 400);
    const scope = canonical(s);
    return this.k.tx(() => {
      if (this.k.one("SELECT id FROM nodes WHERE id=?", b.node_id))
        fail("ExistingNodeProtected", 409);
      const old = this.k.one(
        "SELECT * FROM control_devices WHERE id=? OR store_id=?",
        b.node_id,
        b.store_id,
      );
      if (old) {
        if (
          old.id !== b.node_id ||
          old.store_id !== b.store_id ||
          old.token_hash !== b.token_hash ||
          old.scope !== scope ||
          old.expires_at !== b.expires_at ||
          !old.enabled
        )
          fail("DeviceRegistrationConflict", 409);
        return { registered: true, existing: true, node_id: old.id };
      }
      this.k.sql.exec(
        "INSERT INTO control_devices VALUES(?,?,?,?,?,1)",
        b.node_id,
        b.store_id,
        b.token_hash,
        scope,
        b.expires_at,
      );
      return { registered: true, existing: false, node_id: b.node_id };
    });
  }
  revoke(node) {
    const row = this.k.one("SELECT id FROM control_devices WHERE id=?", node);
    if (!row) fail("DeviceNotFound", 404);
    this.k.tx(() => {
      this.k.sql.exec("UPDATE control_devices SET enabled=0 WHERE id=?", node);
      this.k.sql.exec(
        "UPDATE tasks SET state='cancelled' WHERE id IN (SELECT task_id FROM device_tasks WHERE device_id=?)",
        node,
      );
      this.k.sql.exec(
        "UPDATE workplans SET state='paused' WHERE id IN (SELECT plan_id FROM device_tasks WHERE device_id=?)",
        node,
      );
      this.k.sql.exec(
        "UPDATE jobs SET state='cancelled' WHERE task_id IN (SELECT task_id FROM device_tasks WHERE device_id=?) AND state='pending' AND attempt_id IS NULL",
        node,
      );
    });
    return { revoked: true, node_id: node };
  }
  async authenticate(request, path, b) {
    const node = request.headers.get("X-Control-Node");
    const d = this.k.one("SELECT * FROM control_devices WHERE id=?", node);
    if (
      !d?.enabled ||
      d.expires_at <= this.k.clock() ||
      d.store_id !== b.store_id ||
      d.token_hash !== request.headers.get("X-Control-Node-Hash")
    )
      fail("DeviceUnauthorized");
    const signed = canonical({
      protocol: 2,
      method: "POST",
      path,
      node_id: node,
      body: b,
    });
    if (
      request.headers.get("X-Local-Control-Proof") !==
      (await controlProof(signed, d.token_hash))
    )
      fail("DeviceProofRequired");
    const current = this.k.one(
      "SELECT * FROM control_devices WHERE id=?",
      node,
    );
    if (
      !current?.enabled ||
      current.expires_at <= this.k.clock() ||
      canonical(current) !== canonical(d)
    )
      fail("DeviceUnauthorized");
    return { ...d, scope: JSON.parse(d.scope) };
  }
  owned(d, plan) {
    const row = this.k.one(
      "SELECT * FROM device_tasks WHERE plan_id=? AND device_id=?",
      plan,
      d.id,
    );
    if (!row) fail("DeviceTaskNotOwned");
    return row;
  }
  status(d, plan) {
    const row = this.owned(d, plan);
    const j = this.k.one(
      "SELECT state,attempt_id,result FROM jobs WHERE id=?",
      row.job_id,
    );
    const result = JSON.parse(j.result || "{}");
    return {
      plan_id: plan,
      task_id: row.task_id,
      job_id: row.job_id,
      state: j.state,
      attempted: !!j.attempt_id,
      verified: result.verified === true,
      message_ids: Array.isArray(result.message_ids) ? result.message_ids : [],
    };
  }
  cancel(d, plan) {
    const row = this.owned(d, plan);
    this.k.tx(() => {
      this.k.sql.exec(
        "UPDATE tasks SET state='cancelled' WHERE id=?",
        row.task_id,
      );
      this.k.sql.exec("UPDATE workplans SET state='paused' WHERE id=?", plan);
      this.k.sql.exec(
        "UPDATE jobs SET state='cancelled' WHERE id=? AND state='pending' AND attempt_id IS NULL",
        row.job_id,
      );
    });
    return this.status(d, plan);
  }
  async prepare(d, b, resolvedTarget = null) {
    if (
      !id(b.request_id) ||
      b.account_id !== d.scope.account_ids[0] ||
      normalizeTelegramTarget(b.target) !== b.target ||
      !d.scope.targets.includes(b.target) ||
      typeof b.text !== "string" ||
      !b.text.trim() ||
      b.text.length > 4096 ||
      !Number.isFinite(b.expires_at) ||
      b.expires_at <= this.k.clock() ||
      b.expires_at > Math.min(d.expires_at, this.k.clock() + 3600)
    )
      fail("InvalidExplicitText", 400);
    // Numeric peer IDs are frozen before execution. Username aliases must first
    // be resolved by an authorized target-inspection workflow, not trusted here.
    const target = b.target.startsWith("-") ? b.target : resolvedTarget;
    if (
      typeof target !== "string" ||
      !target.startsWith("-") ||
      normalizeTelegramTarget(target) !== target
    )
      fail("ResolvedGroupTargetRequired", 400);
    const hash = await sha(
      canonical({
        account_id: b.account_id,
        target,
        requested_target: b.target,
        text: b.text,
        expires_at: b.expires_at,
      }),
    );
    const plan =
      "WF-DEVICE-" +
      (await sha(d.id + ":" + b.request_id)).slice(0, 32).toUpperCase();
    const task = "device-task-" + plan,
      job = "send:" + task;
    const payload = {
      action: "send_text",
      target,
      text: b.text,
      reply_to: null,
      media: [],
      platform: "telegram",
      account_id: b.account_id,
    };
    const policy = {
      execution_channel: "cloud",
      starts_at: new Date(this.k.clock() * 1000).toISOString(),
      ends_at: new Date(b.expires_at * 1000).toISOString(),
      min_interval: 60,
      daily_limit: 1,
      max_joins: 0,
      target_allowlist: [target],
    };
    const t = {
      id: task,
      version: hash,
      manifest_hash: hash,
      state: "paused",
      phase: "ready",
      account_ids: [b.account_id],
      policy,
    };
    const j = {
      id: job,
      kind: "send",
      reference_id: job,
      task_id: task,
      account_id: b.account_id,
      pool: "telegram.mtproto",
      version: hash,
      action: "send_text",
      payload,
    };
    const definition = {
      id: plan,
      mode: "explicit_text",
      device_id: d.id,
      account_ids: [b.account_id],
      native_task_ids: [task],
      native_job_ids: [job],
      goal: 1,
      execution_channel: "cloud",
      local_fallback_allowed: false,
      requirements: {},
      steps: [
        {
          id: "explicit:send",
          stage: "send",
          depends_on: [],
          input: {
            ...payload,
            grant_target: b.target,
            job_id: job,
            manifest_hash: hash,
          },
        },
      ],
    };
    return this.k.tx(() => {
      const grant = this.k.one(
        "SELECT * FROM control_devices WHERE id=?",
        d.id,
      );
      if (
        !grant?.enabled ||
        grant.expires_at <= this.k.clock() ||
        grant.expires_at !== d.expires_at ||
        grant.scope !== canonical(d.scope)
      )
        fail("DeviceUnauthorized");
      const old = this.k.one(
        "SELECT * FROM device_tasks WHERE device_id=? AND request_id=?",
        d.id,
        b.request_id,
      );
      if (old) {
        if (old.request_hash !== hash) fail("DeviceRequestChanged", 409);
        return { plan_id: old.plan_id, existing: true };
      }
      // One durable send for this grant, across retries, new request IDs and restarts.
      if (
        this.k.one("SELECT plan_id FROM device_tasks WHERE device_id=?", d.id)
      )
        fail("DeviceMessageBudgetUsed", 409);
      this.k.sql.exec(
        "INSERT INTO tasks VALUES(?,?,?,?,?)",
        task,
        "paused",
        "ready",
        hash,
        canonical(t),
      );
      this.k.sql.exec(
        "INSERT INTO jobs(id,kind,reference_id,task_id,account_id,pool,version,payload,state,available_at,expires_at,result) VALUES(?,?,?,?,?,?,?,?,'pending',?,?,'{}')",
        job,
        "send",
        job,
        task,
        b.account_id,
        "telegram.mtproto",
        hash,
        canonical(j),
        this.k.clock(),
        b.expires_at,
      );
      this.k.sql.exec(
        "INSERT INTO workplans VALUES(?,?,?,?,?,?,?)",
        plan,
        hash,
        canonical(definition),
        "paused",
        "device_explicit_draft",
        this.k.clock(),
        this.k.clock(),
      );
      this.k.sql.exec(
        "INSERT INTO worksteps(plan_id,id,definition,state,available_at) VALUES(?,?,?,'pending',?)",
        plan,
        "explicit:send",
        canonical(definition.steps[0]),
        this.k.clock(),
      );
      this.k.sql.exec(
        "INSERT INTO device_tasks VALUES(?,?,?,?,?,?)",
        plan,
        d.id,
        b.request_id,
        hash,
        task,
        job,
      );
      return {
        plan_id: plan,
        task_id: task,
        job_id: job,
        existing: false,
        state: "draft",
      };
    });
  }
}
