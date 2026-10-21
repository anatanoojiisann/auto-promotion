import { ControlError, canonical, sha } from "./kernel.mjs";

export function ensureGeneratedTaskSchema(k) {
  k.sql.exec(
    "CREATE TABLE IF NOT EXISTS telegram_generated_tasks(plan_id TEXT NOT NULL,step_id TEXT NOT NULL,packet_id TEXT NOT NULL,task_id TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE,manifest_hash TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(plan_id,step_id))",
  );
}

export function pipelineParent(k, plan, runtime) {
  const row = k.one(
    "SELECT * FROM tasks WHERE id=?",
    runtime.reference_task_id,
  );
  const task = row && JSON.parse(row.payload);
  if (
    !task ||
    row.version !== runtime.reference_version ||
    row.state !== "active" ||
    task.state !== "active" ||
    !plan.definition.native_task_ids.includes(task.id) ||
    task.policy?.execution_channel !== "cloud" ||
    !task.account_ids.includes(runtime.account_id)
  )
    throw new ControlError("PipelineReferenceTaskNotActive");
  const policy = task.policy;
  if (
    k.one("SELECT platform FROM accounts WHERE id=?", runtime.account_id)
      ?.platform !== "telegram"
  )
    throw new ControlError("PipelineTelegramAccountRequired");
  if (
    !Number.isFinite(Date.parse(policy.starts_at)) ||
    !Number.isFinite(Date.parse(policy.ends_at)) ||
    !Number.isInteger(policy.daily_limit) ||
    policy.daily_limit < 1 ||
    !Number.isInteger(policy.max_joins) ||
    policy.max_joins < 0 ||
    !Number.isFinite(policy.min_interval) ||
    policy.min_interval < 0
  )
    throw new ControlError("PipelineReferencePolicyInvalid");
  return task;
}

// Called again by the native gate before each authorization, including the
// final authorization inside the Telegram adapter. Child tasks share parent
// limits rather than obtaining a fresh quota for each generated reply.
export function checkGeneratedTask(k, plan, job, task) {
  if (!task.workflow_provenance) return;
  const p = task.workflow_provenance;
  const runtime = k.one(
    "SELECT * FROM telegram_reply_runtime WHERE plan_id=?",
    plan.id,
  );
  const generated = k.one(
    "SELECT * FROM telegram_generated_tasks WHERE job_id=?",
    job.id,
  );
  if (
    !runtime?.enabled ||
    !generated ||
    generated.plan_id !== plan.id ||
    generated.task_id !== task.id ||
    p.plan_id !== plan.id ||
    p.plan_hash !== plan.manifest_hash ||
    p.packet_id !== generated.packet_id
  )
    throw new ControlError("GeneratedTaskAuthorizationMissing");
  const parent = pipelineParent(k, plan, runtime);
  if (
    p.parent_task_id !== parent.id ||
    canonical(task.policy) !== canonical(parent.policy)
  )
    throw new ControlError("GeneratedTaskPolicyChanged");
  const payload = JSON.parse(job.payload).payload;
  if (
    !(parent.policy.target_allowlist || []).some(
      (t) => t.toLowerCase() === payload.target.toLowerCase(),
    )
  )
    throw new ControlError("GeneratedTargetOutsideFrozenPolicy");
  const family = k.rows(
    "SELECT j.id,j.kind,j.attempt_at,j.attempt_id FROM jobs j JOIN tasks t ON t.id=j.task_id WHERE j.task_id=? OR json_extract(t.payload,'$.workflow_provenance.parent_task_id')=?",
    parent.id,
    parent.id,
  );
  const other = family.filter((j) => j.id !== job.id && j.attempt_id);
  if (
    !job.attempt_id &&
    job.kind === "join" &&
    other.filter((j) => j.kind === "join").length >= parent.policy.max_joins
  )
    throw new ControlError("JoinLimitReached");
  if (
    !job.attempt_id &&
    parent.policy.pacing_scope !== "account" &&
    other.some(
      (j) => Number(j.attempt_at) + parent.policy.min_interval > k.clock(),
    )
  )
    throw new ControlError("PacingWait");
}

export class TelegramTaskBuilder {
  constructor(queue, gate, env) {
    this.q = queue;
    this.k = queue.k;
    this.gate = gate;
    this.env = env;
    ensureGeneratedTaskSchema(this.k);
  }
  async build(planId, stepId) {
    const plan = this.gate.plan(planId);
    const runtime = this.k.one(
      "SELECT * FROM telegram_reply_runtime WHERE plan_id=?",
      planId,
    );
    if (!runtime?.enabled) throw new ControlError("ReplyPipelineNotStarted");
    const parent = pipelineParent(this.k, plan, runtime);
    const row = this.k.one(
      "SELECT * FROM worksteps WHERE plan_id=? AND id=?",
      planId,
      stepId,
    );
    const step = row && JSON.parse(row.definition);
    if (!step || !["join", "send"].includes(step.stage))
      throw new ControlError("NativeBuildStepRequired");
    const previous = this.k.one(
      "SELECT * FROM telegram_generated_tasks WHERE plan_id=? AND step_id=?",
      planId,
      stepId,
    );
    if (previous) return { created: false, ...previous };
    if (
      row.state !== "pending" ||
      this.q.dependencies(planId, step).some((d) => d?.state !== "completed")
    )
      throw new ControlError("WorkflowQualificationIncomplete");
    const target = step.input.target.toLowerCase();
    if (
      !(parent.policy.target_allowlist || []).some(
        (t) => t.toLowerCase() === target,
      ) ||
      (
        plan.definition.requirements
          .historical_attempted_targets_not_replayed || []
      ).some((t) => t.toLowerCase() === target)
    )
      throw new ControlError("GeneratedTargetOutsideFrozenPolicy");
    const packet = this.k.one(
      "SELECT * FROM telegram_prepared_replies WHERE plan_id=? AND target=?",
      planId,
      target,
    );
    if (!packet) throw new ControlError("PreparedReplyRequired");
    const kind = step.stage,
      prefix = target.slice(1);
    const read = (stage) =>
      this.k.one(
        "SELECT state,result FROM worksteps WHERE plan_id=? AND id=?",
        planId,
        prefix + ":" + stage,
      );
    const researchRow = read("research_review"),
      opportunityRow = read("opportunity_review"),
      reviewRow = read("reply_review");
    const research = JSON.parse(researchRow?.result || "{}"),
      opportunity = JSON.parse(opportunityRow?.result || "{}");
    const review = JSON.parse(reviewRow?.result || "{}");
    // A join needs an actual opportunity, not just a broadly relevant group.
    if (
      researchRow?.state !== "completed" ||
      opportunityRow?.state !== "completed" ||
      research.facts?.automation_allowed !== true ||
      !opportunity.question?.message_id
    )
      throw new ControlError("QualifiedInteractionOpportunityRequired");
    this.q.checkEvidence({ stage: "research_review" }, research, plan);
    this.q.checkEvidence({ stage: "opportunity_review" }, opportunity, plan);
    if (reviewRow?.state !== "completed" || review.text !== packet.text)
      throw new ControlError("PreparedReplyDoesNotMatchReview");
    this.q.checkEvidence({ stage: "reply_review" }, review, plan);
    const snapshot = canonical([
      row,
      researchRow,
      opportunityRow,
      reviewRow,
      parent,
      packet,
    ]);
    const account = this.k.one(
      "SELECT * FROM accounts WHERE id=?",
      runtime.account_id,
    );
    if (!account || account.disabled || this.k.settings().global_stop)
      throw new ControlError("ExecutionStopped");
    const now = this.k.clock();
    const available = Math.max(
      now,
      row.available_at,
      account.cooldown_until,
      Date.parse(parent.policy.starts_at) / 1000,
      Date.parse(
        plan.definition.requirements.platform_execution_not_before ||
          parent.policy.starts_at,
      ) / 1000,
    );
    const expires = Date.parse(parent.policy.ends_at) / 1000;
    if (available >= expires) throw new ControlError("ExecutionWindowElapsed");
    const payload = {
      target,
      action: kind === "join" ? "join" : "reply",
      media: [],
      ...(kind === "send"
        ? { text: packet.text, reply_to: review.question.message_id }
        : {}),
    };
    const provenance = {
      plan_id: planId,
      plan_hash: plan.manifest_hash,
      packet_id: packet.id,
      parent_task_id: parent.id,
      parent_version: runtime.reference_version,
      step_id: stepId,
    };
    const manifest = {
      schema: 1,
      account_id: runtime.account_id,
      account_snapshot: JSON.parse(account.payload),
      policy: parent.policy,
      payload,
      provenance,
      available_at: available,
      expires_at: expires,
    };
    const hash = await sha(canonical(manifest));
    const taskId = "AP-CLOUD-" + hash.slice(0, 24).toUpperCase(),
      jobId = kind + ":workflow:" + hash;
    const task = {
      id: taskId,
      name: "Prepared Telegram " + kind + " " + target,
      state: "active",
      phase: "ready",
      account_ids: [runtime.account_id],
      policy: parent.policy,
      version: hash,
      manifest,
      manifest_hash: hash,
      workflow_provenance: provenance,
    };
    const scopes = [
      {
        scope: "cloud_task",
        key: parent.id,
        limit: { daily: parent.policy.daily_limit, hourly: 100000 },
      },
    ];
    if (kind === "send") {
      scopes.push({
        scope: "telegram_workflow_target",
        key: canonical([parent.id, review.question.discussion_id]),
        limit: { daily: parent.policy.target_daily_limit || 1, hourly: 100000 },
      });
      if (!review.question.author_hash)
        throw new ControlError("QuestionAuthorEvidenceRequired");
      scopes.push({
        scope: "telegram_workflow_user",
        key: canonical([parent.id, review.question.author_hash]),
        limit: { daily: parent.policy.user_daily_limit || 1, hourly: 100000 },
      });
    }
    const job = {
      id: jobId,
      kind,
      reference_id: packet.id + ":" + kind,
      task_id: taskId,
      account_id: runtime.account_id,
      pool: "telegram.mtproto",
      version: hash,
      payload,
      action: payload.action,
      scopes,
      position: 0,
      state: "not_attempted",
      available_at: available,
      expires_at: expires,
    };
    return this.k.tx(() => {
      const existing = this.k.one(
        "SELECT * FROM telegram_generated_tasks WHERE plan_id=? AND step_id=?",
        planId,
        stepId,
      );
      if (existing) return { created: false, ...existing };
      const currentPlan = this.gate.plan(planId);
      const currentRuntime = this.k.one(
        "SELECT * FROM telegram_reply_runtime WHERE plan_id=?",
        planId,
      );
      if (!currentRuntime?.enabled)
        throw new ControlError("ReplyPipelineNotStarted");
      const currentParent = pipelineParent(this.k, currentPlan, currentRuntime);
      const current = this.k.one(
        "SELECT * FROM worksteps WHERE plan_id=? AND id=?",
        planId,
        stepId,
      );
      const currentPacket = this.k.one(
        "SELECT * FROM telegram_prepared_replies WHERE id=?",
        packet.id,
      );
      if (
        snapshot !==
        canonical([
          current,
          read("research_review"),
          read("opportunity_review"),
          read("reply_review"),
          currentParent,
          currentPacket,
        ])
      )
        throw new ControlError("NativeBuildEvidenceChanged");
      const duplicate = this.k.one(
        "SELECT j.id FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE a.identity=? AND j.kind=? AND lower(json_extract(j.payload,'$.payload.target'))=? AND (j.attempt_id IS NOT NULL OR j.state IN ('pending','leased','unknown','pending_admin_approval','confirmed','joined','already_joined')) AND (?='join' OR json_extract(j.payload,'$.payload.reply_to')=?) LIMIT 1",
        account.identity,
        kind,
        target,
        kind,
        payload.reply_to || 0,
      );
      if (duplicate)
        throw new ControlError("TelegramIntentAlreadyQueuedOrAttempted");
      this.k.sql.exec(
        "INSERT INTO tasks VALUES(?,?,?,?,?)",
        taskId,
        task.state,
        task.phase,
        hash,
        canonical(task),
      );
      this.k.sql.exec(
        "INSERT INTO jobs(id,kind,reference_id,task_id,account_id,pool,version,payload,state,available_at,expires_at) VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
        jobId,
        kind,
        job.reference_id,
        taskId,
        runtime.account_id,
        job.pool,
        hash,
        canonical(job),
        available,
        expires,
      );
      this.k.sql.exec(
        "INSERT INTO telegram_generated_tasks VALUES(?,?,?,?,?,?,?)",
        planId,
        stepId,
        packet.id,
        taskId,
        jobId,
        hash,
        now,
      );
      this.gate.link({
        plan_id: planId,
        step_id: stepId,
        job_id: jobId,
        source: runtime.source,
      });
      this.k.event("workflow.prepared_native_task_created", planId, {
        task_id: taskId,
        job_id: jobId,
        parent_task_id: parent.id,
        source: runtime.source,
        manifest_hash: hash,
        packet_id: packet.id,
      });
      return {
        created: true,
        task_id: taskId,
        job_id: jobId,
        manifest_hash: hash,
      };
    });
  }
  async tick() {
    let created = 0;
    const rows = this.k.rows(
      "SELECT w.plan_id,w.id FROM worksteps w JOIN telegram_reply_runtime r ON r.plan_id=w.plan_id JOIN workplans p ON p.id=w.plan_id JOIN telegram_prepared_replies x ON x.plan_id=w.plan_id AND x.target=lower(json_extract(w.definition,'$.input.target')) WHERE r.enabled=1 AND p.state='active' AND x.state<>'excluded' AND w.state='pending' AND json_extract(w.definition,'$.stage') IN ('join','send') AND w.available_at<=? AND NOT EXISTS(SELECT 1 FROM telegram_generated_tasks g WHERE g.plan_id=w.plan_id AND g.step_id=w.id) ORDER BY w.available_at,w.rowid LIMIT 100",
      this.k.clock(),
    );
    for (const row of rows) {
      if (this.k.settings().global_stop) break;
      try {
        const result = await this.build(row.plan_id, row.id);
        if (result.created) created++;
      } catch (error) {
        // Dependency waits are ordinary; only prepared targets get a visible reason.
        const step = JSON.parse(
          this.k.one(
            "SELECT definition FROM worksteps WHERE plan_id=? AND id=?",
            row.plan_id,
            row.id,
          ).definition,
        );
        if (
          error instanceof ControlError &&
          error.message === "NativeReplyContextNeedsRefresh"
        )
          this.k.sql.exec(
            "UPDATE worksteps SET state='pending',available_at=? WHERE plan_id=? AND id=? AND state='completed'",
            this.k.clock(),
            row.plan_id,
            step.input.target.toLowerCase().slice(1) + ":reply_review",
          );
        if (
          this.k.one(
            "SELECT id FROM telegram_prepared_replies WHERE plan_id=? AND target=?",
            row.plan_id,
            step.input.target.toLowerCase(),
          )
        )
          this.k.sql.exec(
            "UPDATE worksteps SET result=? WHERE plan_id=? AND id=? AND state='pending'",
            canonical({
              verdict: "defer",
              reason:
                error instanceof ControlError
                  ? error.message
                  : "NativeBuildUnavailable",
            }),
            row.plan_id,
            row.id,
          );
      }
      if (created >= 2) break;
    }
    return { created };
  }
}
