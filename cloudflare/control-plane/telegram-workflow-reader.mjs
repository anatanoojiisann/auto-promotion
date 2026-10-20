import { ControlError, sha } from "./kernel.mjs";
const NODE = "cloudflare-telegram-reader";
export class TelegramWorkflowReader {
  constructor(queue, env) {
    this.q = queue;
    this.k = queue.k;
    this.env = env;
    if (
      !this.k.one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_reader_runtime'",
      )
    )
      this.k.sql.exec(
        "CREATE TABLE telegram_reader_runtime(plan_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,next_at REAL NOT NULL,lease_until REAL NOT NULL DEFAULT 0,runs INTEGER NOT NULL DEFAULT 0,reads INTEGER NOT NULL DEFAULT 0,last_run REAL NOT NULL DEFAULT 0,state TEXT NOT NULL)",
      );
  }
  async rpc(operation, input = {}) {
    const r = await this.env.TELEGRAM_EXECUTOR.fetch(
      "https://telegram-executor/internal",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...input,
          operation,
          account_id: this.env.TELEGRAM_EXECUTOR_ACCOUNT,
        }),
      },
    );
    return { ok: r.ok, result: await r.json() };
  }
  async start(id, source) {
    const p = this.q.plan(id);
    if (
      p.state !== "active" ||
      !p.definition.account_ids.includes(this.env.TELEGRAM_EXECUTOR_ACCOUNT) ||
      !source ||
      !this.env.TG_QUEUE_BRIDGE
    )
      throw new ControlError("TelegramReaderSetupIncomplete", 409);
    const health = await this.rpc("health");
    if (!health.ok || health.result.identity_verified !== true)
      throw new ControlError("ExecutorLoginNotVerified", 409);
    this.q.registerNode({
      id: NODE,
      token_hash: await sha("reader:" + this.env.TG_QUEUE_BRIDGE),
      runtime: "cloud",
      stages: [
        "discover",
        "inspect",
        "membership_verify",
        "receipt_verify",
        "follow_up",
        "goal_check",
      ],
    });
    this.k.tx(() => {
      for (const jobId of p.definition.native_job_ids) {
        const j = this.k.one("SELECT * FROM jobs WHERE id=?", jobId);
        if (
          !j ||
          j.kind !== "join" ||
          !j.attempt_id ||
          j.account_id !== this.env.TELEGRAM_EXECUTOR_ACCOUNT
        )
          continue;
        const id = "prior-receipt:" + jobId;
        if (
          !this.k.one(
            "SELECT id FROM worksteps WHERE plan_id=? AND id=?",
            p.id,
            id,
          )
        )
          this.q.put(p.id, {
            id,
            stage: "membership_verify",
            depends_on: [],
            input: {
              target: JSON.parse(j.payload).payload.target,
              native_job_id: j.id,
              prior_attempt_must_not_repeat: true,
            },
          });
      }
    });
    this.k.sql.exec(
      "INSERT INTO telegram_reader_runtime(plan_id,enabled,next_at,state) VALUES(?,1,0,'queued') ON CONFLICT(plan_id) DO UPDATE SET enabled=1",
      id,
    );
    this.k.event("workflow.telegram_reader_started", id, {
      source,
      account_id: this.env.TELEGRAM_EXECUTOR_ACCOUNT,
    });
    return {
      started: true,
      scope: "authenticated_telegram_reads",
      node_id: NODE,
    };
  }
  snapshot(id) {
    return (
      this.k.one(
        "SELECT plan_id,enabled,next_at,runs,reads,last_run,state FROM telegram_reader_runtime WHERE plan_id=?",
        id,
      ) || null
    );
  }
  stop(id, source) {
    if (!source) throw new ControlError("ExplicitWorkflowStopRequired", 400);
    this.k.sql.exec(
      "UPDATE telegram_reader_runtime SET enabled=0,state='stopped' WHERE plan_id=?",
      id,
    );
    return { stopped: true };
  }
  async tick() {
    const out = [];
    for (const rt of this.k.rows(
      "SELECT * FROM telegram_reader_runtime WHERE enabled=1",
    )) {
      const now = this.k.clock();
      if (
        rt.next_at > now ||
        rt.lease_until > now ||
        this.k.settings().global_stop ||
        this.q.plan(rt.plan_id).state !== "active"
      )
        continue;
      this.k.sql.exec(
        "UPDATE telegram_reader_runtime SET lease_until=?,next_at=?,state='running' WHERE plan_id=?",
        now + 150,
        now + 900,
        rt.plan_id,
      );
      const account = this.k.one(
        "SELECT cooldown_until,disabled FROM accounts WHERE id=?",
        this.env.TELEGRAM_EXECUTOR_ACCOUNT,
      );
      if (!account || account.disabled || account.cooldown_until > now) {
        this.k.sql.exec(
          "UPDATE telegram_reader_runtime SET lease_until=0,state='waiting_account' WHERE plan_id=?",
          rt.plan_id,
        );
        continue;
      }
      const stages = [
        ["discover"],
        ["inspect"],
        ["membership_verify", "inspect"],
        ["receipt_verify", "follow_up", "inspect"],
        ["goal_check", "inspect"],
      ][rt.runs % 5];
      const work = this.q.claim({ id: NODE, stages }, rt.plan_id).step;
      if (!work) {
        this.k.sql.exec(
          "UPDATE telegram_reader_runtime SET lease_until=0,state='waiting_schedule',runs=runs+1 WHERE plan_id=?",
          rt.plan_id,
        );
        continue;
      }
      try {
        let reply, nativeJob, receiptSource, reconcileReceipt;
        if (work.stage === "goal_check")
          reply = {
            ok: true,
            result: {
              verified_interacted_groups: this.q.status(rt.plan_id, {
                limit: 1,
              }).verified_interacted_groups,
            },
          };
        else if (["receipt_verify", "follow_up"].includes(work.stage)) {
          receiptSource = this.q.receiptSource(rt.plan_id, work);
          nativeJob = receiptSource?.job;
          const stored = receiptSource?.receipt,
            payload = receiptSource?.payload;
          if (
            !receiptSource ||
            nativeJob.account_id !== this.env.TELEGRAM_EXECUTOR_ACCOUNT
          )
            reply = {
              ok: false,
              result: { reason: "durable_send_receipt_required" },
            };
          else
            reply = await this.rpc(
              work.stage === "receipt_verify" ? "verify" : "read_context",
              {
                ...payload,
                message_ids: stored.message_ids,
                message_id: stored.message_ids[0],
              },
            );
        } else reply = await this.rpc(work.stage, work.input);
        const r = reply.result;
        let result;
        if (!reply.ok) {
          result = {
            verdict: "defer",
            reason: r.reason || r.status || "TelegramReadUnavailable",
            evidence: [],
          };
          if (r.retry_after_seconds)
            this.k.sql.exec(
              "UPDATE accounts SET cooldown_until=MAX(cooldown_until,?) WHERE identity=(SELECT identity FROM accounts WHERE id=?)",
              now + r.retry_after_seconds,
              this.env.TELEGRAM_EXECUTOR_ACCOUNT,
            );
        } else if (work.stage === "goal_check") {
          const achieved =
            r.verified_interacted_groups >=
            this.q.plan(rt.plan_id).definition.goal;
          result = {
            verdict: achieved ? "pass" : "defer",
            ...(achieved
              ? {}
              : { reason: "qualified_interaction_goal_not_reached" }),
            evidence: [
              {
                checked_at: new Date().toISOString(),
                verified_interacted_groups: r.verified_interacted_groups,
              },
            ],
          };
        } else if (work.stage === "receipt_verify") {
          if (
            r.verified === true &&
            receiptSource &&
            JSON.stringify(r.message_ids) ===
              JSON.stringify(receiptSource.receipt.message_ids) &&
            r.external_reference === receiptSource.receipt.external_reference &&
            r.discussion_id ===
              JSON.parse(
                this.k.one(
                  "SELECT result FROM worksteps WHERE plan_id=? AND id=?",
                  rt.plan_id,
                  work.input.target.toLowerCase().slice(1) +
                    ":membership_verify",
                )?.result || "{}",
              ).discussion_id
          ) {
            reconcileReceipt = () => {
              const current = this.q.receiptSource(rt.plan_id, work);
              if (
                !current ||
                current.job.attempt_id !== nativeJob.attempt_id ||
                current.job.version !== nativeJob.version ||
                JSON.stringify(current.receipt.message_ids) !==
                  JSON.stringify(receiptSource.receipt.message_ids)
              )
                throw new ControlError("NativeReceiptChanged", 409);
              const checked = { ...current.receipt, ...r, status: "confirmed" };
              this.k.sql.exec(
                "UPDATE jobs SET state='confirmed',result=? WHERE id=?",
                JSON.stringify(checked),
                nativeJob.id,
              );
              this.k.sql.exec(
                "UPDATE worksteps SET state='completed',result=? WHERE plan_id=? AND id=? AND state IN ('unknown','completed')",
                JSON.stringify({
                  ...checked,
                  verdict: "pass",
                  native_job_id: nativeJob.id,
                }),
                rt.plan_id,
                current.sendId,
              );
            };
            result = {
              verdict: "pass",
              verified: true,
              native_state: "confirmed",
              native_job_id: nativeJob.id,
              discussion_id: r.discussion_id,
              evidence: [{ checked_at: new Date().toISOString(), receipt: r }],
            };
          } else
            result = {
              verdict: "defer",
              reason: "message_not_conclusively_verified",
              evidence: [{ checked_at: new Date().toISOString(), receipt: r }],
            };
        } else if (work.stage === "follow_up") {
          result = {
            verdict: "defer",
            reason: r.replies?.length
              ? "follow_up_context_needs_evaluation"
              : "waiting_for_voluntary_reply",
            evidence: [{ checked_at: new Date().toISOString(), context: r }],
            follow_up_may_need_reply: !!r.replies?.length,
          };
        } else if (work.stage === "discover") {
          result = {
            verdict: "pass",
            targets: (r.candidates || []).map((c) => c.target),
            evidence: [
              {
                checked_at: new Date().toISOString(),
                transport: "authenticated_mtproto",
                query: work.input.query,
                candidates: r.candidates || [],
              },
            ],
          };
        } else if (work.stage === "inspect") {
          for (const m of [...r.pinned, ...r.recent]) {
            m.author_hash = await sha(m.author_id || "unknown");
            delete m.author_id;
          }
          if (new TextEncoder().encode(JSON.stringify(r)).length > 900000) {
            r.pinned = r.pinned.slice(0, 20);
            r.recent = r.recent.slice(0, 50);
            for (const m of [...r.pinned, ...r.recent])
              m.text = m.text.slice(0, 2000);
            r.pinned_truncated = true;
            r.storage_sample_truncated = true;
          }
          const rulesText =
            (r.about || "") + "\n" + r.pinned.map((m) => m.text).join("\n");
          const linkedRules =
            /https?:\/\//.test(rulesText) && /rules|правил/i.test(rulesText);
          const completeRules =
            !r.pinned_truncated &&
            !r.forum_topics_unchecked &&
            !/[!/]rules\b/i.test(r.about || "") &&
            !linkedRules;
          result = {
            verdict: completeRules ? "pass" : "defer",
            ...(completeRules ? {} : { reason: "rules_need_further_reading" }),
            facts: {
              identity_checked: !!r.identity_checked,
              rules_read: completeRules,
              recent_context_read: true,
            },
            evidence: [
              {
                url: "https://t.me/" + work.input.target.slice(1),
                checked_at: r.checked_at,
                transport: "authenticated_mtproto",
                observation: r,
              },
            ],
          };
        } else {
          const plan = this.q.plan(rt.plan_id);
          const linked =
            this.k.one(
              "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_native_links'",
            ) &&
            this.k.one(
              "SELECT job_id FROM workflow_native_links WHERE plan_id=? AND step_id=?",
              rt.plan_id,
              work.input.target.toLowerCase().slice(1) + ":join",
            );
          const jobId = work.input.native_job_id || linked?.job_id;
          const j = jobId && this.k.one("SELECT * FROM jobs WHERE id=?", jobId);
          if (
            r.verified === true &&
            j?.attempt_id &&
            [
              "unknown",
              "pending_admin_approval",
              "joined",
              "already_joined",
            ].includes(j.state)
          ) {
            const checked = {
              status: "joined",
              verified: true,
              discussion_id: r.discussion_id,
              external_reference: r.external_reference,
            };
            this.k.sql.exec(
              "UPDATE jobs SET state='joined',result=? WHERE id=? AND state<>'leased'",
              JSON.stringify(checked),
              j.id,
            );
            if (linked)
              this.k.sql.exec(
                "UPDATE worksteps SET state='completed',result=? WHERE plan_id=? AND id=?",
                JSON.stringify(checked),
                rt.plan_id,
                work.input.target.toLowerCase().slice(1) + ":join",
              );
            result = {
              verdict: "pass",
              verified: true,
              native_state: "joined",
              native_job_id: j.id,
              discussion_id: r.discussion_id,
              evidence: [
                { checked_at: new Date().toISOString(), membership: r },
              ],
            };
          } else
            result = {
              verdict: "defer",
              reason: "membership_not_conclusively_verified",
              evidence: [
                { checked_at: new Date().toISOString(), membership: r },
              ],
            };
        }
        this.q.finish(
          { id: NODE },
          {
            plan_id: rt.plan_id,
            id: work.id,
            lease_token: work.lease_token,
            result,
            retry_at: this.k.clock() + 3600,
          },
          reconcileReceipt,
        );
        this.k.sql.exec(
          "UPDATE telegram_reader_runtime SET lease_until=0,runs=runs+1,reads=reads+1,last_run=?,state=CASE WHEN enabled=1 THEN 'waiting_schedule' ELSE 'stopped' END WHERE plan_id=?",
          this.k.clock(),
          rt.plan_id,
        );
        out.push({
          plan_id: rt.plan_id,
          stage: work.stage,
          verdict: result.verdict,
        });
      } catch {
        this.k.sql.exec(
          "UPDATE telegram_reader_runtime SET lease_until=0,runs=runs+1,state='waiting_retry' WHERE plan_id=?",
          rt.plan_id,
        );
      }
    }
    return { reads: out };
  }
}
