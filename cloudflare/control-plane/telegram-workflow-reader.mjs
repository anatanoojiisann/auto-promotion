import { ControlError, sha } from "./kernel.mjs";
import { verifiedCooldownStart } from "./telegram-executor-bridge.mjs";
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
    const columns = new Set(this.k.rows("PRAGMA table_info(telegram_reader_runtime)").map(c => c.name));
    for (const [name, type] of Object.entries({
      rpc_counts: "TEXT NOT NULL DEFAULT '{}'", last_read_state: "TEXT",
      last_read_observed_at: "TEXT", cooldown_source: "TEXT",
    })) if (!columns.has(name)) this.k.sql.exec(`ALTER TABLE telegram_reader_runtime ADD COLUMN ${name} ${type}`);
  }
  readPolicy() {
    let configured;
    try { configured = JSON.parse(this.env.TG_READ_POLICY_JSON || "{}"); }
    catch { throw new ControlError("InvalidTelegramReadPolicy", 400); }
    const defaults = { requests_per_window: 120, window_seconds: 3600, scan_rpc_limit: 40,
      messages_per_page: 100, groups_per_scan: 10, cache_ttl_seconds: 300 };
    const result = Object.fromEntries(Object.entries(defaults).map(([key, fallback]) =>
      [key, Number.isSafeInteger(configured[key]) && configured[key] >= (key === "cache_ttl_seconds" ? 0 : 1) ? configured[key] : fallback]));
    result.groups_per_scan = Math.min(100, result.groups_per_scan);
    result.messages_per_page = Math.min(100, result.messages_per_page);
    result.window_seconds = result.window_seconds >= 60 ? Math.min(86400, result.window_seconds) : defaults.window_seconds;
    return result;
  }
  async rpc(operation, input = {}, remaining = null) {
    const policy = this.readPolicy();
    if (remaining != null) policy.scan_rpc_limit = Math.min(policy.scan_rpc_limit, remaining);
    const r = await this.env.TELEGRAM_EXECUTOR.fetch(
      "https://telegram-executor/internal",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...input,
          read_policy: policy,
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
    const deferred = await verifiedCooldownStart(this.k, this.env);
    if (!deferred) {
      const health = await this.rpc("health");
      if (!health.ok || health.result.identity_verified !== true)
        throw new ControlError("ExecutorLoginNotVerified", 409);
    }
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
      "INSERT INTO telegram_reader_runtime(plan_id,enabled,next_at,state) VALUES(?,1,?,?) ON CONFLICT(plan_id) DO UPDATE SET enabled=1,next_at=excluded.next_at,state=excluded.state",
      id,
      deferred?.not_before || 0,
      deferred ? "waiting_cooldown" : "queued",
    );
    this.k.event("workflow.telegram_reader_started", id, {
      source,
      account_id: this.env.TELEGRAM_EXECUTOR_ACCOUNT,
    });
    return {
      started: true,
      scope: "authenticated_telegram_reads",
      node_id: NODE,
      ...(deferred || {}),
      waiting_cooldown: !!deferred,
    };
  }
  snapshot(id) {
    return (
      this.k.one(
        "SELECT plan_id,enabled,next_at,runs,reads,reads AS executor_read_operations,last_run,state,rpc_counts,last_read_state,last_read_observed_at,cooldown_source FROM telegram_reader_runtime WHERE plan_id=?",
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
    const out = []; let claimed = 0;
    const policy = this.readPolicy(), limit = policy.groups_per_scan;
    let remaining = policy.scan_rpc_limit;
    for (const rt of this.k.rows(
      "SELECT * FROM telegram_reader_runtime WHERE enabled=1 ORDER BY last_run,next_at,plan_id",
    )) {
      if (claimed >= limit || remaining <= 0) break;
      const now = this.k.clock();
      if (
        rt.next_at > now ||
        rt.lease_until > now ||
        this.k.settings().global_stop ||
        this.q.plan(rt.plan_id).state !== "active"
      )
        continue;
      this.k.sql.exec(
        "UPDATE telegram_reader_runtime SET lease_until=?,next_at=?,last_run=?,state='running' WHERE plan_id=?",
        now + 150,
        now + 900,
        now,
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
      claimed++;
      try {
        let reply, nativeJob, receiptSource, reconcileReceipt;
        if (work.stage === "goal_check")
          reply = {
            ok: true,
            result: {
              verified_interacted_groups: this.q.status(rt.plan_id, {
                limit: 1,
              }).verified_interacted_groups,
              rpc_counts: {},
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
              remaining,
            );
        } else reply = await this.rpc(work.stage, work.input, remaining);
        const r = reply.result;
        const countsKnown = r.rpc_counts && typeof r.rpc_counts === "object" && !Array.isArray(r.rpc_counts) &&
          Object.values(r.rpc_counts).every(value => Number.isSafeInteger(value) && value >= 0);
        if (countsKnown) remaining = Math.max(0, remaining - Object.entries(r.rpc_counts)
          .reduce((sum, [category, count]) => category === "mutation" ? sum : sum + count, 0));
        else remaining = 0; // Unknown counts never become zero-cost work for later plans.
        let result;
        if (!reply.ok || r.read_complete === false) {
          result = {
            verdict: "defer",
            reason: r.read_incomplete_reason || r.reason || r.status || "TelegramReadUnavailable",
            evidence: [],
          };
          if (r.retry_after_seconds && (r.rate_limited || r.risk_warning))
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
            /https?:\/\//.test(rulesText) && /rules|锌褉邪胁懈谢/i.test(rulesText);
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
                account_id: this.env.TELEGRAM_EXECUTOR_ACCOUNT,
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
        result.read_diagnostics = Object.fromEntries([
          "read_state", "read_complete", "read_observed_at", "read_cursor_next", "rpc_counts",
          "rpc_count_method", "read_rpc_network_attempts", "read_rpc_retries", "read_budget_scope", "cooldown_source",
          "read_result_scope", "read_cursor_scope", "read_sample_truncated", "read_sample_messages",
          "read_sample_limits", "read_cache_hits", "read_fresh_samples", "read_fresh_sources",
          "read_policy_effective", "read_budget_state",
          "read_cache_skipped_oversize",
        ].filter(key => r[key] !== undefined).map(key => [key, r[key]]));
        if (!countsKnown) Object.assign(result.read_diagnostics, { rpc_counts: null, rpc_count_method: "unavailable" });
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
          "UPDATE telegram_reader_runtime SET lease_until=0,runs=runs+1,reads=reads+1,last_run=?,state=CASE WHEN enabled=1 THEN 'waiting_schedule' ELSE 'stopped' END,rpc_counts=?,last_read_state=?,last_read_observed_at=?,cooldown_source=? WHERE plan_id=?",
          this.k.clock(),
          JSON.stringify(countsKnown ? r.rpc_counts : null), r.read_state || null,
          r.read_observed_at || null, r.cooldown_source || null,
          rt.plan_id,
        );
        out.push({
          plan_id: rt.plan_id,
          stage: work.stage,
          verdict: result.verdict,
        });
      } catch {
        remaining = 0; // The executor may have consumed the entire remaining budget before response loss.
        this.k.sql.exec(
          "UPDATE telegram_reader_runtime SET lease_until=0,runs=runs+1,state='waiting_retry',rpc_counts='null',last_read_state='incomplete',last_read_observed_at=NULL,cooldown_source=NULL WHERE plan_id=?",
          rt.plan_id,
        );
      }
    }
    return { reads: out };
  }
}
