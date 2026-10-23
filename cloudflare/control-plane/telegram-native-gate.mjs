import { ControlError, sha } from "./kernel.mjs";
import { checkGeneratedTask } from "./telegram-task-builder.mjs";
import { verifiedCooldownStart } from "./telegram-executor-bridge.mjs";
export const TELEGRAM_NODE = "cloudflare-telegram";
export async function signNativeGate(secret, raw) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return btoa(
    String.fromCharCode(
      ...new Uint8Array(
        await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(raw)),
      ),
    ),
  );
}
export async function verifyNativeGate(secret, raw, signature) {
  if (!secret || !signature) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["verify"],
    );
    return await crypto.subtle.verify(
      "HMAC",
      key,
      Uint8Array.from(atob(signature), (c) => c.charCodeAt(0)),
      new TextEncoder().encode(raw),
    );
  } catch {
    return false;
  }
}
export class TelegramNativeGate {
  constructor(workflows, env) {
    this.q = workflows;
    this.k = workflows.k;
    this.env = env;
    if (
      !this.k.one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_native_links'",
      )
    )
      this.k.sql.exec(
        "CREATE TABLE workflow_native_links(plan_id TEXT NOT NULL,step_id TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE,task_id TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(plan_id,step_id))",
      );
    if (
      !this.k.one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_activation_runtime'",
      )
    )
      this.k.sql.exec(
        "CREATE TABLE telegram_activation_runtime(plan_id TEXT PRIMARY KEY,source TEXT NOT NULL,enabled INTEGER NOT NULL,next_at REAL NOT NULL,state TEXT NOT NULL,last_error TEXT)",
      );
  }
  plan(id) {
    const p = this.q.plan(id);
    if (
      p.state !== "active" ||
      !p.definition.account_ids.includes(this.env.TELEGRAM_EXECUTOR_ACCOUNT)
    )
      throw new ControlError("ExecutorPlanNotActive", 403);
    return p;
  }
  async activate(body) {
    const p = this.plan(body.plan_id);
    if (!body.source || !this.env.TG_QUEUE_BRIDGE)
      throw new ControlError("ExecutorSetupIncomplete", 409);
    if (this.k.settings().global_stop)
      throw new ControlError("ExecutionStopped");
    const account = this.env.TELEGRAM_EXECUTOR_ACCOUNT;
    const deferred = await verifiedCooldownStart(this.k, this.env);
    if (deferred) {
      this.k.sql.exec(
        "INSERT INTO telegram_activation_runtime VALUES(?,?,1,?,'waiting_cooldown',NULL) ON CONFLICT(plan_id) DO UPDATE SET enabled=1,next_at=excluded.next_at,state=excluded.state,last_error=NULL",
        p.id,
        body.source,
        deferred.not_before,
      );
      this.k.event("workflow.telegram_activation_queued", p.id, {
        source: body.source,
        ...deferred,
      });
      return {
        activated: false,
        queued: true,
        account_id: account,
        ...deferred,
      };
    }
    const linkedTasks = this.k
      .rows(
        "SELECT DISTINCT task_id FROM workflow_native_links WHERE plan_id=?",
        p.id,
      )
      .map((r) => r.task_id);
    const allowed = new Set([...p.definition.native_task_ids, ...linkedTasks]);
    const other = this.k
      .rows(
        "SELECT j.id,j.task_id FROM jobs j JOIN tasks t ON t.id=j.task_id JOIN accounts a ON a.id=j.account_id WHERE a.identity=(SELECT identity FROM accounts WHERE id=?) AND t.state='active' AND j.state IN ('pending','leased') AND j.expires_at>?",
        account,
        this.k.clock(),
      )
      .filter((j) => !allowed.has(j.task_id));
    if (other.length)
      throw new ControlError("ExecutorAccountHasOtherPendingTasks", 409);
    const r = await this.env.TELEGRAM_EXECUTOR.fetch(
      "https://telegram-executor/internal",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: account, operation: "health" }),
      },
    );
    const health = await r.json();
    if (!r.ok || health.identity_verified !== true)
      throw new ControlError("ExecutorLoginNotVerified", 409);
    const reference = this.k.one(
      "SELECT payload FROM tasks WHERE id=?",
      p.definition.native_task_ids[0],
    );
    if (!reference) throw new ControlError("ExecutorReferenceTaskMissing");
    const task = JSON.parse(reference.payload),
      until = Date.parse(task.policy.ends_at) / 1000;
    const prior = this.k.accountChannel(account);
    this.k.registerNode({
      id: TELEGRAM_NODE,
      token_hash: await sha(this.env.TG_QUEUE_BRIDGE),
      pools: ["telegram.mtproto"],
      account_ids: [account],
    });
    this.k.setAccountChannel({
      account_id: account,
      task_id: task.id,
      node_id: TELEGRAM_NODE,
      channel: "cloud",
      until,
      generation: crypto.randomUUID().replaceAll("-", ""),
      expected_generation: prior?.generation || null,
    });
    this.k.event("workflow.telegram_executor_activated", p.id, {
      account_id: account,
      node_id: TELEGRAM_NODE,
      source: body.source,
    });
    this.k.sql.exec(
      "UPDATE telegram_activation_runtime SET enabled=0,state='active',last_error=NULL WHERE plan_id=?",
      p.id,
    );
    return { activated: true, node_id: TELEGRAM_NODE, account_id: account };
  }
  activationSnapshot(id) {
    return (
      this.k.one(
        "SELECT plan_id,enabled,next_at,state,last_error FROM telegram_activation_runtime WHERE plan_id=?",
        id,
      ) || null
    );
  }
  async resumeActivations() {
    for (const row of this.k.rows(
      "SELECT a.* FROM telegram_activation_runtime a JOIN workplans p ON p.id=a.plan_id WHERE a.enabled=1 AND a.next_at<=? AND p.state='active' ORDER BY a.next_at LIMIT 2",
      this.k.clock(),
    )) {
      try {
        await this.activate({ plan_id: row.plan_id, source: row.source });
      } catch (error) {
        this.k.sql.exec(
          "UPDATE telegram_activation_runtime SET next_at=?,state='waiting_configuration',last_error=? WHERE plan_id=? AND enabled=1",
          this.k.clock() + 900,
          error instanceof ControlError
            ? error.message
            : "ExecutorActivationUnavailable",
          row.plan_id,
        );
      }
    }
  }
  checkLink(planId, stepId, jobId) {
    const p = this.plan(planId),
      row = this.k.one(
        "SELECT * FROM worksteps WHERE plan_id=? AND id=?",
        planId,
        stepId,
      );
    if (!row) throw new ControlError("WorkflowStepMissing", 404);
    const step = JSON.parse(row.definition),
      j = this.k.one("SELECT * FROM jobs WHERE id=?", jobId);
    if (
      !j ||
      !["join", "send"].includes(step.stage) ||
      j.kind !== step.stage ||
      j.account_id !== this.env.TELEGRAM_EXECUTOR_ACCOUNT
    )
      throw new ControlError("NativeWorkflowScopeMismatch", 403);
    if (this.q.dependencies(planId, step).some((d) => d?.state !== "completed"))
      throw new ControlError("WorkflowQualificationIncomplete", 409);
    if (j.kind === "join") {
      const reviewId = step.depends_on.find((id) =>
        id.endsWith(":research_review"),
      );
      const review = JSON.parse(
        this.k.one(
          "SELECT result FROM worksteps WHERE plan_id=? AND id=?",
          planId,
          reviewId,
        )?.result || "{}",
      );
      if (review.facts?.automation_allowed !== true)
        throw new ControlError("AutomatedJoinNotVerified", 403);
    }
    const data = JSON.parse(j.payload),
      task = JSON.parse(
        this.k.one("SELECT payload FROM tasks WHERE id=?", j.task_id)
          ?.payload || "{}",
      );
    if (
      task.state !== "active" ||
      task.policy?.execution_channel !== "cloud" ||
      data.payload?.target?.toLowerCase() !== step.input.target.toLowerCase()
    )
      throw new ControlError("NativeWorkflowScopeMismatch", 403);
    checkGeneratedTask(this.k, p, j, task);
    if (j.kind === "send") {
      const reviewId = step.depends_on.find((id) =>
        id.endsWith(":reply_review"),
      );
      const review = JSON.parse(
        this.k.one(
          "SELECT result FROM worksteps WHERE plan_id=? AND id=?",
          planId,
          reviewId,
        )?.result || "{}",
      );
      this.q.checkEvidence({ stage: "reply_review" }, review, p);
      if (
        !review.question?.text ||
        !Number.isFinite(Date.parse(review.reviewed_at)) ||
        this.k.clock() - Date.parse(review.reviewed_at) / 1000 > 900 ||
        Date.parse(review.reviewed_at) / 1000 > this.k.clock() + 60
      )
        throw new ControlError("NativeReplyContextNeedsRefresh", 409);
      if (
        data.payload.text !== review.text ||
        data.payload.reply_to !== review.question.message_id ||
        !task.manifest_hash
      )
        throw new ControlError("NativeReplyDoesNotMatchReview", 403);
      const membership = JSON.parse(
        this.k.one(
          "SELECT result FROM worksteps WHERE plan_id=? AND id=?",
          planId,
          step.input.target.toLowerCase().slice(1) + ":membership_verify",
        )?.result || "{}",
      );
      if (review.question.discussion_id !== membership.discussion_id)
        throw new ControlError("NativeQuestionDiscussionMismatch", 403);
    }
    return { p, step, j, data };
  }
  link(body) {
    if (!body.source)
      throw new ControlError("ExplicitNativeDispatchRequired", 400);
    const { j } = this.checkLink(body.plan_id, body.step_id, body.job_id);
    if (j.state !== "pending" || j.attempt_id)
      throw new ControlError("NativeAttemptCannotReplay", 409);
    if (
      j.kind === "join" &&
      this.k.one(
        "SELECT old.id FROM jobs old JOIN accounts a ON a.id=old.account_id WHERE old.kind='join' AND (old.attempt_id IS NOT NULL OR old.state IN ('unknown','pending_admin_approval')) AND old.id<>? AND lower(json_extract(old.payload,'$.payload.target'))=? AND a.identity=(SELECT identity FROM accounts WHERE id=?) LIMIT 1",
        j.id,
        JSON.parse(j.payload).payload.target.toLowerCase(),
        j.account_id,
      )
    )
      throw new ControlError("TelegramTargetAlreadyAttempted", 409);
    const old = this.k.one(
      "SELECT * FROM workflow_native_links WHERE plan_id=? AND step_id=?",
      body.plan_id,
      body.step_id,
    );
    if (old && old.job_id !== j.id)
      throw new ControlError("FrozenNativeLinkChanged", 409);
    if (!old)
      this.k.sql.exec(
        "INSERT INTO workflow_native_links VALUES(?,?,?,?,?)",
        body.plan_id,
        body.step_id,
        j.id,
        j.task_id,
        this.k.clock(),
      );
    return { linked: true, job_id: j.id };
  }
  async authorize(body) {
    const link = this.k.one(
      "SELECT * FROM workflow_native_links WHERE job_id=?",
      body.job_id,
    );
    if (!link) throw new ControlError("NativeWorkflowLinkRequired", 403);
    const { j, data } = this.checkLink(link.plan_id, link.step_id, link.job_id);
    if (body.account_id !== j.account_id || body.version !== j.version)
      throw new ControlError("CloudManifestMismatch", 403);
    const node = this.k.node(
      TELEGRAM_NODE,
      await sha(this.env.TG_QUEUE_BRIDGE),
    );
    const granted = this.k.authorize(
      node,
      j.id,
      body.lease_token,
      body.attempt_id,
    );
    let job = data;
    if (j.kind === "send") {
      const reviewId = this.checkLink(
        link.plan_id,
        link.step_id,
        link.job_id,
      ).step.depends_on.find((id) => id.endsWith(":reply_review"));
      const review = JSON.parse(
        this.k.one(
          "SELECT result FROM worksteps WHERE plan_id=? AND id=?",
          link.plan_id,
          reviewId,
        ).result,
      );
      job = {
        ...data,
        workflow_context: {
          question: review.question,
          reviewed_at: review.reviewed_at,
          discussion_continuing: review.facts?.discussion_continuing === true,
        },
      };
    }
    return { ...granted, job, lease_until: j.lease_until };
  }
  async dispatch(body) {
    const link = this.k.one(
      "SELECT * FROM workflow_native_links WHERE plan_id=? AND step_id=?",
      body.plan_id,
      body.step_id,
    );
    if (!link) throw new ControlError("NativeWorkflowLinkRequired", 403);
    const { j, data } = this.checkLink(link.plan_id, link.step_id, link.job_id);
    if (
      JSON.parse(
        this.k.one("SELECT payload FROM tasks WHERE id=?", j.task_id).payload,
      ).workflow_provenance
    ) {
      const statusResponse = await this.env.TELEGRAM_EXECUTOR.fetch(
        "https://telegram-executor/internal",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            account_id: data.account_id,
            operation: "status",
          }),
        },
      );
      const status = await statusResponse.json();
      if (
        !statusResponse.ok ||
        status.login_state !== "authorized" ||
        !status.last_verified_at
      )
        return { submitted: false, reason: "waiting_cloud_login" };
    }
    const node = this.k.node(
      TELEGRAM_NODE,
      await sha(this.env.TG_QUEUE_BRIDGE),
    );
    const lease = this.k.claim(node, "telegram.mtproto", j.id);
    if (!lease) return { submitted: false, reason: "waiting_native_queue" };
    const attempt = "cloud-executor-" + crypto.randomUUID();
    let result;
    try {
      const response = await this.env.TELEGRAM_EXECUTOR.fetch(
        "https://telegram-executor/internal",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            operation: "execute",
            account_id: j.account_id,
            job_id: j.id,
            version: j.version,
            attempt_id: attempt,
            lease_token: lease.lease_token,
          }),
        },
      );
      result = await response.json();
      const current = this.k.one(
        "SELECT state,attempt_id,owner,lease_token,lease_until FROM jobs WHERE id=?",
        j.id,
      );
      if (
        response.status === 409 &&
        ["waiting_cooldown", "waiting_account_lock"].includes(result.status) &&
        result.phase === "pre_execution" &&
        result.native_authorized === false &&
        result.submission_started === false &&
        result.job_id === j.id &&
        result.account_id === j.account_id &&
        result.job_version === j.version &&
        result.attempt_id === attempt &&
        current?.state === "leased" &&
        !current.attempt_id &&
        current.owner === node.id &&
        current.lease_token === lease.lease_token &&
        current.lease_until > this.k.clock() &&
        Number.isFinite(result.retry_after_seconds) &&
        result.retry_after_seconds >= 1 &&
        result.retry_after_seconds <= 86400
      ) {
        const retryAt = this.k.clock() + result.retry_after_seconds;
        this.k.complete(node, j.id, lease.lease_token, {
          state: "pending",
          result: { reason: result.status, next_at: retryAt },
        });
        this.k.sql.exec(
          "UPDATE worksteps SET available_at=?,result=? WHERE plan_id=? AND id=? AND state='pending'",
          retryAt,
          JSON.stringify({ verdict: "defer", reason: result.status }),
          link.plan_id,
          link.step_id,
        );
        return {
          submitted: false,
          job_id: j.id,
          reason: result.status,
          retry_at: retryAt,
        };
      }
      if (!response.ok)
        result = {
          status: "unknown",
          verified: false,
          reason: "ExecutorOutcomeUnconfirmed",
        };
    } catch {
      result = {
        status: "unknown",
        verified: false,
        reason: "ExecutorOutcomeUnconfirmed",
      };
    }
    if (
      ![
        "joined",
        "already_joined",
        "confirmed",
        "pending_admin_approval",
        "failed_known",
        "unknown",
      ].includes(result.status)
    )
      result = { status: "unknown", verified: false };
    const current = this.k.one("SELECT attempt_id FROM jobs WHERE id=?", j.id);
    this.k.complete(node, j.id, lease.lease_token, {
      state: result.status,
      result,
      attempt_id: current.attempt_id,
    });
    const stepState = ["joined", "already_joined", "confirmed"].includes(
      result.status,
    )
      ? "completed"
      : result.status;
    this.k.sql.exec(
      "UPDATE worksteps SET state=?,result=? WHERE plan_id=? AND id=?",
      stepState,
      JSON.stringify({
        ...result,
        verdict: stepState === "completed" ? "pass" : "defer",
        native_job_id: j.id,
      }),
      link.plan_id,
      link.step_id,
    );
    this.k.event("workflow.native_outcome", link.plan_id, {
      job_id: j.id,
      state: result.status,
    });
    this.k.sql.exec(
      "UPDATE workplans SET updated_at=? WHERE id=?",
      this.k.clock(),
      link.plan_id,
    );
    return { submitted: true, job_id: j.id, result };
  }
  async tick() {
    if (!this.env.TG_QUEUE_BRIDGE || this.k.settings().global_stop)
      return { submitted: 0 };
    await this.resumeActivations();
    const rows = this.k.rows(
      "SELECT l.plan_id,l.step_id,l.job_id FROM workflow_native_links l JOIN jobs j ON j.id=l.job_id JOIN workplans p ON p.id=l.plan_id JOIN worksteps w ON w.plan_id=l.plan_id AND w.id=l.step_id WHERE p.state='active' AND j.state='pending' AND j.available_at<=? AND j.expires_at>? ORDER BY CASE WHEN json_extract(w.result,'$.dispatch_blocked') THEN 1 ELSE 0 END,j.available_at,l.created_at LIMIT 16",
      this.k.clock(),
      this.k.clock(),
    );
    let count = 0;
    for (const row of rows) {
      try {
        const r = await this.dispatch(row);
        if (r.submitted) count++;
        if (count) break; // Bound writes as before; scan past blocked candidates.
      } catch (error) {
        const reason =
          error instanceof ControlError
            ? error.message
            : "NativeDispatchUnavailable";
        const retryAt = this.k.clock() + 1800;
        this.k.tx(() => {
          this.k.sql.exec(
            "UPDATE jobs SET available_at=MAX(available_at,?) WHERE id=? AND state='pending' AND attempt_id IS NULL",
            retryAt,
            row.job_id,
          );
          this.k.sql.exec(
            "UPDATE worksteps SET available_at=?,result=? WHERE plan_id=? AND id=? AND state='pending'",
            retryAt,
            JSON.stringify({
              verdict: "defer",
              reason,
              dispatch_blocked: true,
              requires_review: reason === "NativeReplyContextNeedsRefresh",
            }),
            row.plan_id,
            row.step_id,
          );
          if (reason === "NativeReplyContextNeedsRefresh") {
            const send = JSON.parse(
              this.k.one(
                "SELECT definition FROM worksteps WHERE plan_id=? AND id=?",
                row.plan_id,
                row.step_id,
              ).definition,
            );
            const reviewId = send.depends_on.find((id) =>
              id.endsWith(":reply_review"),
            );
            this.k.sql.exec(
              "UPDATE worksteps SET state='pending',available_at=? WHERE plan_id=? AND id=? AND state='completed'",
              this.k.clock(),
              row.plan_id,
              reviewId,
            );
          }
          this.k.event("workflow.native_blocked", row.plan_id, {
            step_id: row.step_id,
            reason,
            retry_at: retryAt,
          });
        });
      }
    }
    return { submitted: count };
  }
}
