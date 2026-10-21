import { ControlError, canonical, sha } from "./kernel.mjs";
import {
  pipelineParent,
  ensureGeneratedTaskSchema,
} from "./telegram-task-builder.mjs";
import {
  REVIEW_STAGES,
  validatePreparedReply,
  verifyObservation,
  researchReview,
  opportunityReview,
  exactDraft,
  replyReview,
} from "./telegram-reply-policy.mjs";

const NODE = "cloudflare-prepared-reply-reviewer";
const permanent = new Set([
  "AutomationForbiddenByRules",
  "PreparedResearchNotQualified",
  "QuestionAlreadyResolved",
  "QuestionTooOld",
  "DuplicatePreparedReply",
  "ProductRecommendationNotAccepted",
  "PreparedLinkNotAllowed",
  "AiDisclosureRequired",
]);

export class TelegramPreparedReplies {
  constructor(queue, reader, env) {
    this.q = queue;
    this.k = queue.k;
    this.reader = reader;
    this.env = env;
    ensureGeneratedTaskSchema(this.k);
    this.k.sql.exec(
      "CREATE TABLE IF NOT EXISTS telegram_reply_runtime(plan_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,reference_task_id TEXT NOT NULL,reference_version TEXT NOT NULL,source TEXT NOT NULL,enabled INTEGER NOT NULL,next_at REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,runs INTEGER NOT NULL DEFAULT 0,last_run REAL NOT NULL DEFAULT 0,state TEXT NOT NULL)",
    );
    this.k.sql.exec(
      "CREATE TABLE IF NOT EXISTS telegram_prepared_replies(id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,target TEXT NOT NULL,question_id INTEGER NOT NULL,text TEXT NOT NULL,review TEXT NOT NULL,review_hash TEXT NOT NULL,source TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(plan_id,target))",
    );
  }
  async start(body) {
    const plan = this.q.plan(body.plan_id);
    if (
      !this.env.TG_QUEUE_BRIDGE ||
      !body.source ||
      plan.state !== "active" ||
      !plan.definition.account_ids.includes(
        this.env.TELEGRAM_EXECUTOR_ACCOUNT,
      ) ||
      !plan.definition.native_task_ids.includes(body.reference_task_id)
    )
      throw new ControlError("ExplicitPipelineStartRequired", 400);
    const row = this.k.one(
      "SELECT * FROM tasks WHERE id=?",
      body.reference_task_id,
    );
    const runtime = {
      reference_task_id: body.reference_task_id,
      reference_version: row?.version,
      account_id: this.env.TELEGRAM_EXECUTOR_ACCOUNT,
    };
    pipelineParent(this.k, plan, runtime);
    const tokenHash = await sha("prepared-review:" + this.env.TG_QUEUE_BRIDGE);
    this.k.tx(() => {
      pipelineParent(this.k, this.q.plan(plan.id), runtime);
      const prior = this.k.one(
        "SELECT * FROM telegram_reply_runtime WHERE plan_id=?",
        plan.id,
      );
      if (
        prior &&
        (prior.reference_task_id !== runtime.reference_task_id ||
          prior.reference_version !== runtime.reference_version)
      )
        throw new ControlError("FrozenPipelineReferenceChanged");
      this.q.registerNode({
        id: NODE,
        token_hash: tokenHash,
        runtime: "cloud",
        stages: REVIEW_STAGES,
      });
      this.k.sql.exec(
        "INSERT INTO telegram_reply_runtime(plan_id,account_id,reference_task_id,reference_version,source,enabled,state) VALUES(?,?,?,?,?,1,'queued') ON CONFLICT(plan_id) DO UPDATE SET enabled=1,next_at=0,state='queued'",
        plan.id,
        runtime.account_id,
        runtime.reference_task_id,
        runtime.reference_version,
        body.source,
      );
      this.k.event("workflow.prepared_reply_pipeline_started", plan.id, {
        source: body.source,
        reference_task_id: runtime.reference_task_id,
      });
    });
    return {
      started: true,
      model_service_required: false,
      exact_text_only: true,
      ...this.snapshot(plan.id),
    };
  }
  stop(body) {
    if (!body.source)
      throw new ControlError("ExplicitWorkflowStopRequired", 400);
    this.k.sql.exec(
      "UPDATE telegram_reply_runtime SET enabled=0,state='stopped' WHERE plan_id=?",
      body.plan_id,
    );
    this.k.event("workflow.prepared_reply_pipeline_stopped", body.plan_id, {
      source: body.source,
    });
    return { stopped: true };
  }
  async prepare(body) {
    if (!body.source)
      throw new ControlError("PreparedReplySourceRequired", 400);
    const p = this.q.plan(body.plan_id),
      input = validatePreparedReply(body);
    if (
      p.state !== "active" ||
      !p.definition.account_ids.includes(this.env.TELEGRAM_EXECUTOR_ACCOUNT)
    )
      throw new ControlError("ExecutorPlanNotActive");
    const stages = this.k
      .rows(
        "SELECT definition FROM worksteps WHERE plan_id=? AND lower(json_extract(definition,'$.input.target'))=?",
        p.id,
        input.target,
      )
      .map((r) => JSON.parse(r.definition));
    if (
      !REVIEW_STAGES.every((stage) => stages.some((s) => s.stage === stage)) ||
      !stages.some((s) => s.stage === "send") ||
      (
        p.definition.requirements.historical_attempted_targets_not_replayed ||
        []
      ).some((t) => t.toLowerCase() === input.target)
    )
      throw new ControlError("TargetNotInInteractionWorkflow");
    const runtime = this.k.one(
      "SELECT * FROM telegram_reply_runtime WHERE plan_id=?",
      p.id,
    );
    if (!runtime) throw new ControlError("ReplyPipelineNotConfigured");
    const parent = pipelineParent(this.k, p, runtime);
    if (
      !(parent.policy.target_allowlist || []).some(
        (t) => t.toLowerCase() === input.target,
      )
    )
      throw new ControlError("GeneratedTargetOutsideFrozenPolicy");
    const id =
      "prepared:" +
      (await sha(
        canonical({
          plan_hash: p.manifest_hash,
          target: input.target,
          question_id: input.question_message_id,
          text: input.text,
        }),
      ));
    const reviewHash = await sha(canonical(input.review));
    return this.k.tx(() => {
      const old = this.k.one(
        "SELECT * FROM telegram_prepared_replies WHERE plan_id=? AND target=?",
        p.id,
        input.target,
      );
      if (old && old.id !== id)
        throw new ControlError("FrozenPreparedReplyChanged");
      if (old && old.review_hash !== reviewHash)
        throw new ControlError("PreparedReviewNeedsExplicitReconfirmation");
      if (!old) {
        this.k.sql.exec(
          "INSERT INTO telegram_prepared_replies VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          id,
          p.id,
          input.target,
          input.question_message_id,
          input.text,
          canonical(input.review),
          reviewHash,
          body.source,
          "queued",
          this.k.clock(),
          this.k.clock(),
        );
        this.k.event("workflow.exact_reply_prepared", p.id, {
          packet_id: id,
          target: input.target,
          question_message_id: input.question_message_id,
          review_hash: reviewHash,
          source: body.source,
        });
        this.k.sql.exec(
          "UPDATE telegram_reply_runtime SET next_at=0 WHERE plan_id=?",
          p.id,
        );
      }
      return {
        prepared: true,
        existing: !!old,
        packet_id: id,
        review_hash: reviewHash,
        text: input.text,
        target: input.target,
        question_message_id: input.question_message_id,
        automatically_publish_when_qualified: !!runtime.enabled,
      };
    });
  }
  async reconfirm(body) {
    if (!body.source)
      throw new ControlError("PreparedReplySourceRequired", 400);
    const packet = this.k.one(
      "SELECT * FROM telegram_prepared_replies WHERE plan_id=? AND id=?",
      body.plan_id,
      body.packet_id,
    );
    if (!packet || packet.review_hash !== body.expected_review_hash)
      throw new ControlError("PreparedReviewVersionChanged");
    const plan = this.q.plan(body.plan_id);
    const runtime = this.k.one(
      "SELECT * FROM telegram_reply_runtime WHERE plan_id=?",
      body.plan_id,
    );
    if (plan.state !== "active" || !runtime)
      throw new ControlError("ExecutorPlanNotActive");
    pipelineParent(this.k, plan, runtime);
    const input = validatePreparedReply({
      target: packet.target,
      question_message_id: packet.question_id,
      text: packet.text,
      review: body.review,
    });
    const hash = await sha(canonical(input.review));
    return this.k.tx(() => {
      if (
        this.k.one(
          "SELECT review_hash FROM telegram_prepared_replies WHERE id=?",
          packet.id,
        )?.review_hash !== body.expected_review_hash
      )
        throw new ControlError("PreparedReviewVersionChanged");
      if (
        this.k.one(
          "SELECT 1 FROM jobs j JOIN telegram_generated_tasks g ON g.job_id=j.id WHERE g.packet_id=? AND j.kind='send' AND (j.attempt_id IS NOT NULL OR j.state<>'pending')",
          packet.id,
        )
      )
        throw new ControlError("NativeAttemptCannotReplay");
      const rows = this.k
        .rows(
          "SELECT id,state,definition FROM worksteps WHERE plan_id=? AND lower(json_extract(definition,'$.input.target'))=?",
          packet.plan_id,
          packet.target,
        )
        .filter((r) => REVIEW_STAGES.includes(JSON.parse(r.definition).stage));
      if (rows.some((r) => r.state === "leased"))
        throw new ControlError("PreparedReviewBusy");
      for (const r of rows)
        this.k.sql.exec(
          "UPDATE worksteps SET state='pending',available_at=?,result='{}' WHERE plan_id=? AND id=?",
          this.k.clock(),
          packet.plan_id,
          r.id,
        );
      this.k.sql.exec(
        "UPDATE telegram_prepared_replies SET review=?,review_hash=?,state='queued',updated_at=? WHERE id=?",
        canonical(input.review),
        hash,
        this.k.clock(),
        packet.id,
      );
      this.k.sql.exec(
        "UPDATE telegram_reply_runtime SET next_at=0 WHERE plan_id=?",
        packet.plan_id,
      );
      this.k.event("workflow.exact_reply_reconfirmed", packet.plan_id, {
        packet_id: packet.id,
        review_hash: hash,
        source: body.source,
      });
      return {
        reconfirmed: true,
        packet_id: packet.id,
        review_hash: hash,
        text_unchanged: true,
      };
    });
  }
  snapshot(id) {
    const runtime = this.k.one(
      "SELECT plan_id,enabled,next_at,runs,last_run,state,reference_task_id FROM telegram_reply_runtime WHERE plan_id=?",
      id,
    );
    if (!runtime)
      return {
        configured: false,
        model_service_required: false,
        prepared_replies: 0,
      };
    return {
      ...runtime,
      configured: true,
      model_service_required: false,
      exact_text_only: true,
      prepared_replies: this.k.one(
        "SELECT COUNT(*) n FROM telegram_prepared_replies WHERE plan_id=?",
        id,
      ).n,
      generated_tasks:
        this.k.one(
          "SELECT COUNT(*) n FROM telegram_generated_tasks WHERE plan_id=?",
          id,
        )?.n || 0,
    };
  }
  observation(planId, packet) {
    const row = this.k.one(
      "SELECT state,result FROM worksteps WHERE plan_id=? AND id=?",
      planId,
      packet.target.slice(1) + ":inspect",
    );
    if (row?.state !== "completed")
      throw new ControlError("AuthenticatedObservationRequired");
    return verifyObservation(
      JSON.parse(row.result),
      packet,
      this.env.TELEGRAM_EXECUTOR_ACCOUNT,
      this.k.clock(),
    );
  }
  async context(packet) {
    const response = await this.reader.rpc("read_context", {
      target: packet.target,
      message_id: packet.question_message_id,
    });
    if (!response.ok) {
      const seconds = response.result.retry_after_seconds;
      if (Number.isFinite(seconds) && seconds >= 1 && seconds <= 86400)
        this.k.sql.exec(
          "UPDATE accounts SET cooldown_until=MAX(cooldown_until,?) WHERE identity=(SELECT identity FROM accounts WHERE id=?)",
          this.k.clock() + seconds,
          this.env.TELEGRAM_EXECUTOR_ACCOUNT,
        );
      throw new ControlError(
        response.result.reason || "TelegramContextUnavailable",
      );
    }
    const context = response.result;
    for (const message of [context.source, ...(context.replies || [])].filter(
      Boolean,
    )) {
      if (message.author_id) message.author_hash = await sha(message.author_id);
      delete message.author_id;
    }
    return context;
  }
  async evaluate(work, packet) {
    let observation = this.observation(work.plan_id, packet);
    const now = this.k.clock();
    if (work.stage === "research_review") {
      const duplicate = this.k.one(
        "SELECT id FROM worksteps WHERE plan_id=? AND state='completed' AND json_extract(definition,'$.stage')='research_review' AND json_extract(result,'$.discussion_id')=? AND id<>?",
        work.plan_id,
        observation.discussion_id,
        work.id,
      );
      if (duplicate) throw new ControlError("DuplicateDiscussionEntry");
      return researchReview(observation, packet, now);
    }
    if (work.stage === "opportunity_review")
      return opportunityReview(
        await this.context(packet),
        observation,
        packet,
        this.k.clock(),
      );
    if (work.stage === "reply_draft") return exactDraft(packet);
    const opportunity = JSON.parse(
      this.k.one(
        "SELECT result FROM worksteps WHERE plan_id=? AND id=?",
        work.plan_id,
        packet.target.slice(1) + ":opportunity_review",
      )?.result || "{}",
    );
    const prior = this.k
      .rows(
        "SELECT result FROM worksteps WHERE state='completed' AND json_extract(definition,'$.stage')='reply_review' AND NOT(plan_id=? AND id=?)",
        work.plan_id,
        work.id,
      )
      .map((r) => JSON.parse(r.result).text)
      .filter(Boolean);
    const product = this.q.plan(work.plan_id).definition.product_facts;
    // Re-read rules before final review. Stored editorial assertions must still
    // cite the current rules, and new bans or incomplete external rules hold it.
    const refreshed = await this.reader.rpc("inspect", {
      target: packet.target,
    });
    if (!refreshed.ok)
      throw new ControlError(
        refreshed.result.reason || "TelegramRulesRefreshRequired",
      );
    const fresh = refreshed.result;
    for (const m of [...(fresh.pinned || []), ...(fresh.recent || [])]) {
      if (m.author_id) m.author_hash = await sha(m.author_id);
      delete m.author_id;
    }
    observation = verifyObservation(
      {
        verdict: "pass",
        facts: { rules_read: true },
        evidence: [
          {
            transport: "authenticated_mtproto",
            account_id: this.env.TELEGRAM_EXECUTOR_ACCOUNT,
            observation: fresh,
          },
        ],
      },
      packet,
      this.env.TELEGRAM_EXECUTOR_ACCOUNT,
      this.k.clock(),
    );
    researchReview(observation, packet, this.k.clock());
    return replyReview(
      await this.context(packet),
      observation,
      packet,
      opportunity,
      product,
      this.k.clock(),
      prior,
    );
  }
  async tick() {
    let completed = 0;
    for (const runtime of this.k.rows(
      "SELECT * FROM telegram_reply_runtime WHERE enabled=1 AND next_at<=? AND lease_until<=?",
      this.k.clock(),
      this.k.clock(),
    )) {
      const now = this.k.clock(),
        plan = this.q.plan(runtime.plan_id);
      if (plan.state !== "active" || this.k.settings().global_stop) continue;
      let state = "waiting_prepared_content",
        next = now + 900;
      this.k.sql.exec(
        "UPDATE telegram_reply_runtime SET lease_until=? WHERE plan_id=?",
        now + 150,
        plan.id,
      );
      try {
        pipelineParent(this.k, plan, runtime);
        const account = this.k.one(
          "SELECT cooldown_until,disabled FROM accounts WHERE id=?",
          runtime.account_id,
        );
        if (!account || account.disabled)
          throw new ControlError("ExecutionStopped");
        if (account.cooldown_until > now) {
          next = account.cooldown_until;
          throw new ControlError("waiting_cooldown");
        }
        const packets = this.k.rows(
          "SELECT * FROM telegram_prepared_replies WHERE plan_id=? AND state<>'excluded' ORDER BY created_at",
          plan.id,
        );
        if (packets.length) {
          const status = await this.reader.rpc("status");
          if (
            !status.ok ||
            status.result.login_state !== "authorized" ||
            !status.result.last_verified_at
          )
            throw new ControlError("waiting_cloud_login");
          const node = { id: NODE, stages: REVIEW_STAGES };
          let processed = 0;
          for (const raw of packets.slice(0, 4)) {
            const packet = {
              id: raw.id,
              target: raw.target,
              text: raw.text,
              question_message_id: raw.question_id,
              review: JSON.parse(raw.review),
            };
            for (let count = 0; count < 4; count++) {
              if (processed >= 4) break;
              if (
                this.k.settings().global_stop ||
                this.q.plan(plan.id).state !== "active"
              )
                break;
              const work = this.q.claim(node, plan.id, {
                target: packet.target,
              }).step;
              if (!work) break;
              processed++;
              let result;
              try {
                result = await this.evaluate(work, packet);
                const latest = this.k.one(
                  "SELECT review_hash FROM telegram_prepared_replies WHERE id=?",
                  raw.id,
                );
                if (latest?.review_hash !== raw.review_hash)
                  throw new ControlError("PreparedReviewVersionChanged");
              } catch (error) {
                const reason =
                  error instanceof ControlError
                    ? error.message
                    : "PreparedReviewUnavailable";
                result = {
                  verdict:
                    permanent.has(reason) ||
                    reason === "DuplicateDiscussionEntry"
                      ? "exclude"
                      : "defer",
                  reason,
                  evidence: [],
                };
              }
              this.q.finish(node, {
                plan_id: plan.id,
                id: work.id,
                lease_token: work.lease_token,
                result,
                retry_at: now + 900,
              });
              this.k.sql.exec(
                "UPDATE telegram_prepared_replies SET state=?,updated_at=? WHERE id=?",
                result.verdict === "exclude"
                  ? "excluded"
                  : result.verdict === "pass" && work.stage === "reply_review"
                    ? "reviewed"
                    : result.reason || "reviewing",
                this.k.clock(),
                raw.id,
              );
              state =
                result.verdict === "pass"
                  ? "reviewed_or_waiting_dependency"
                  : result.reason;
              if (result.verdict !== "pass") break;
              completed++;
            }
          }
        }
      } catch (error) {
        state =
          error instanceof ControlError
            ? error.message
            : "ReplyPipelineUnavailable";
      } finally {
        this.k.sql.exec(
          "UPDATE telegram_reply_runtime SET lease_until=0,next_at=?,runs=runs+1,last_run=?,state=? WHERE plan_id=?",
          next,
          this.k.clock(),
          state,
          plan.id,
        );
      }
    }
    return { completed };
  }
}
