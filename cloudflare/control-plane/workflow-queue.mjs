// Durable orchestration is separate from once-only native join/send attempts.
import { ControlError, canonical, sha } from "./kernel.mjs";

export const WORK_STAGES = new Set([
  "discover",
  "inspect",
  "research_review",
  "opportunity_review",
  "membership_verify",
  "join",
  "reply_draft",
  "reply_review",
  "send",
  "receipt_verify",
  "follow_up",
  "goal_check",
]);
export function targetSteps(target, nativeJobId = null) {
  if (!/^@[a-zA-Z][a-zA-Z0-9_]{3,31}$/.test(target))
    fail("InvalidWorkflowTarget");
  const prefix = target.toLowerCase().slice(1),
    steps = [];
  const add = (stage, depends_on, input = {}) => {
    const id = prefix + ":" + stage;
    steps.push({
      id,
      stage,
      depends_on: depends_on.map((s) => prefix + ":" + s),
      input: { target, ...input },
    });
  };
  add("inspect", []);
  add("research_review", ["inspect"]);
  add("opportunity_review", ["research_review"]);
  if (!nativeJobId) add("join", ["research_review"]);
  add("membership_verify", nativeJobId ? ["research_review"] : ["join"], {
    native_job_id: nativeJobId,
  });
  add("reply_draft", ["opportunity_review"]);
  add("reply_review", ["reply_draft"]);
  add("send", ["reply_review", "membership_verify"]);
  add("receipt_verify", ["send"]);
  add("follow_up", ["receipt_verify"]);
  return steps;
}
const WRITES = new Set(["join", "send"]);
const SCHEMA = `
CREATE TABLE IF NOT EXISTS workplans(id TEXT PRIMARY KEY,manifest_hash TEXT NOT NULL,definition TEXT NOT NULL,state TEXT NOT NULL,source TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS worksteps(plan_id TEXT NOT NULL,id TEXT NOT NULL,definition TEXT NOT NULL,state TEXT NOT NULL,available_at REAL NOT NULL,owner TEXT,lease_token TEXT,lease_until REAL NOT NULL DEFAULT 0,result TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(plan_id,id));
CREATE INDEX IF NOT EXISTS worksteps_due ON worksteps(plan_id,state,available_at);
CREATE TABLE IF NOT EXISTS workflow_nodes(id TEXT PRIMARY KEY,token_hash TEXT NOT NULL,stages TEXT NOT NULL,runtime TEXT NOT NULL,enabled INTEGER NOT NULL,last_seen REAL NOT NULL DEFAULT 0);
`;
function fail(code) {
  throw new ControlError(code, 400);
}
function safe(value) {
  if (!value || typeof value !== "object") return;
  for (const [key, v] of Object.entries(value)) {
    if (
      /^(api_hash|api_key|password|phone|session_string|cookies?|access_token|refresh_token|credentials?)$/i.test(
        key,
      )
    )
      fail("CredentialInWorkflow");
    safe(v);
  }
}
function validStep(s) {
  if (
    !s ||
    !/^[a-zA-Z0-9_.:-]{1,140}$/.test(s.id || "") ||
    !WORK_STAGES.has(s.stage) ||
    !Array.isArray(s.depends_on) ||
    new Set(s.depends_on).size !== s.depends_on.length ||
    !s.input ||
    typeof s.input !== "object"
  )
    fail("InvalidWorkflowStep");
  if (WRITES.has(s.stage) && !s.input.target) fail("WorkflowTargetRequired");
  safe(s);
}
function validateGraph(steps) {
  const map = new Map(steps.map((s) => [s.id, s]));
  if (map.size !== steps.length) fail("DuplicateWorkflowStep");
  const done = new Set(),
    active = new Set();
  function visit(id) {
    if (!map.has(id)) fail("MissingWorkflowDependency");
    if (active.has(id)) fail("WorkflowCycle");
    if (done.has(id)) return;
    active.add(id);
    for (const parent of map.get(id).depends_on) visit(parent);
    active.delete(id);
    done.add(id);
  }
  for (const s of steps) {
    validStep(s);
    visit(s.id);
  }
}
export class WorkflowQueue {
  constructor(kernel) {
    this.k = kernel;
    if (
      !kernel.one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workplans'",
      )
    )
      kernel.tx(() => kernel.sql.exec(SCHEMA));
    if (
      !kernel.one(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='worksteps_stage_due'",
      )
    )
      kernel.sql.exec(
        "CREATE INDEX worksteps_stage_due ON worksteps(plan_id,state,json_extract(definition,'$.stage'),available_at)",
      );
  }
  async create(input) {
    const { definition: d, source } = input;
    if (
      !d ||
      !/^WF-[A-Z0-9-]{4,70}$/.test(d.id || "") ||
      !d.name ||
      !Array.isArray(d.steps) ||
      !d.steps.length ||
      d.steps.length > 5000 ||
      d.execution_channel !== "cloud" ||
      d.local_fallback_allowed !== false ||
      !Number.isInteger(d.goal) ||
      d.goal < 1 ||
      !source ||
      !Array.isArray(d.account_ids) ||
      !d.account_ids.length ||
      !d.requirements ||
      !Array.isArray(d.native_task_ids) ||
      !Array.isArray(d.native_job_ids)
    )
      fail("InvalidWorkflow");
    safe(d);
    validateGraph(d.steps);
    const hash = await sha(canonical(d));
    return this.k.tx(() => {
      const old = this.k.one(
        "SELECT manifest_hash FROM workplans WHERE id=?",
        d.id,
      );
      if (old && old.manifest_hash !== hash)
        throw new ControlError("FrozenWorkflowChanged");
      if (old) return { id: d.id, manifest_hash: hash, existing: true };
      for (const account of d.account_ids)
        if (!this.k.one("SELECT id FROM accounts WHERE id=?", account))
          fail("UnknownWorkflowAccount");
      this.k.sql.exec(
        "INSERT INTO workplans VALUES(?,?,?,?,?,?,?)",
        d.id,
        hash,
        canonical(d),
        "active",
        source,
        this.k.clock(),
        this.k.clock(),
      );
      for (const s of d.steps) this.put(d.id, s);
      this.k.event("workflow.started", d.id, {
        source,
        manifest_hash: hash,
        step_count: d.steps.length,
      });
      return { id: d.id, manifest_hash: hash, existing: false };
    });
  }
  put(id, s) {
    this.k.sql.exec(
      "INSERT INTO worksteps(plan_id,id,definition,state,available_at) VALUES(?,?,?,'pending',?)",
      id,
      s.id,
      canonical(s),
      s.available_at || this.k.clock(),
    );
  }
  addDiscoveredTargets(plan, targets) {
    if (!Array.isArray(targets) || targets.length > 100)
      fail("InvalidDiscoveredTargets");
    const added = [];
    for (const raw of new Set(targets)) {
      const target = typeof raw === "string" ? "@" + raw.replace(/^@/, "") : "";
      const steps = targetSteps(target);
      if (
        this.k.one(
          "SELECT id FROM worksteps WHERE plan_id=? AND id=?",
          plan.id,
          steps[0].id,
        )
      )
        continue;
      if (
        this.k.one("SELECT COUNT(*) n FROM worksteps WHERE plan_id=?", plan.id)
          .n +
          steps.length >
        5000
      )
        throw new ControlError("WorkflowCapacityReached");
      for (const step of steps) this.put(plan.id, step);
      added.push(target);
    }
    if (added.length)
      this.k.event("workflow.targets_added", plan.id, { targets: added });
    return added;
  }
  plan(id) {
    const row = this.k.one("SELECT * FROM workplans WHERE id=?", id);
    if (!row) throw new ControlError("WorkflowNotFound", 404);
    return { ...row, definition: JSON.parse(row.definition) };
  }
  changeState(id, state, source) {
    const p = this.plan(id);
    if (!["active", "paused", "cancelled"].includes(state) || !source)
      fail("InvalidWorkflowStateChange");
    if (["cancelled", "completed"].includes(p.state))
      throw new ControlError("WorkflowAlreadyEnded");
    this.k.sql.exec(
      "UPDATE workplans SET state=?,updated_at=? WHERE id=?",
      state,
      this.k.clock(),
      id,
    );
    this.k.event("workflow.state_changed", id, { state, source });
    return { id, state };
  }
  node(id, tokenHash) {
    const n = this.k.one("SELECT * FROM workflow_nodes WHERE id=?", id);
    if (!n || !n.enabled || n.token_hash !== tokenHash || n.runtime !== "cloud")
      throw new ControlError("CloudWorkflowNodeRequired", 403);
    return { ...n, stages: JSON.parse(n.stages) };
  }
  registerNode(n) {
    if (
      !n ||
      !/^[a-zA-Z0-9_.-]{3,80}$/.test(n.id || "") ||
      !/^[a-f0-9]{64}$/.test(n.token_hash || "") ||
      n.runtime !== "cloud" ||
      !Array.isArray(n.stages) ||
      !n.stages.length ||
      n.stages.some((s) => !WORK_STAGES.has(s) || WRITES.has(s))
    )
      fail("InvalidCloudWorkflowNode");
    const old = this.k.one(
      "SELECT token_hash FROM workflow_nodes WHERE id=?",
      n.id,
    );
    if (old && old.token_hash !== n.token_hash)
      throw new ControlError("WorkflowNodeCredentialChanged");
    this.k.sql.exec(
      "INSERT INTO workflow_nodes VALUES(?,?,?,?,1,0) ON CONFLICT(id) DO UPDATE SET stages=excluded.stages,enabled=1",
      n.id,
      n.token_hash,
      canonical(n.stages),
      "cloud",
    );
    return { registered: true, id: n.id, runtime: "cloud" };
  }
  dependencies(plan, step) {
    return step.depends_on.map((id) =>
      this.k.one(
        "SELECT state,result FROM worksteps WHERE plan_id=? AND id=?",
        plan,
        id,
      ),
    );
  }
  dependenciesReady(step, rows) {
    return rows.every(
      (r, i) =>
        r?.state === "completed" ||
        (step.stage === "membership_verify" &&
          step.depends_on[i].endsWith(":join") &&
          ["unknown", "pending_admin_approval"].includes(r?.state)),
    );
  }
  status(id, { offset = 0, limit = 100 } = {}) {
    if (
      !Number.isInteger(offset) ||
      offset < 0 ||
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > 500
    )
      fail("InvalidWorkflowPage");
    const p = this.plan(id),
      now = this.k.clock();
    const nodes = this.k
      .rows(
        "SELECT id,stages,last_seen FROM workflow_nodes WHERE enabled=1 AND runtime='cloud'",
      )
      .map((n) => ({ ...n, stages: JSON.parse(n.stages) }));
    const rows = this.k.rows(
      "SELECT * FROM worksteps WHERE plan_id=? ORDER BY rowid",
      id,
    );
    const byId = new Map(rows.map((r) => [r.id, r])),
      stopped = this.k.settings().global_stop;
    const counts = {},
      stages = {};
    const visible = rows.map((row) => {
      const d = JSON.parse(row.definition),
        deps = d.depends_on.map((key) => byId.get(key));
      let state = row.state;
      if (state === "leased" && row.lease_until <= now) state = "pending";
      if (state === "pending") {
        if (p.state !== "active" || stopped) state = "paused";
        else if (deps.some((r) => r?.state === "excluded"))
          state = "excluded_by_dependency";
        else if (!this.dependenciesReady(d, deps)) state = "waiting_dependency";
        else if (row.available_at > now) state = "waiting_schedule";
        else if (WRITES.has(d.stage)) state = "waiting_native_dispatch";
        else if (
          !nodes.some(
            (n) => n.stages.includes(d.stage) && n.last_seen > now - 300,
          )
        )
          state = "waiting_cloud_executor";
        else state = "ready";
      }
      counts[state] = (counts[state] || 0) + 1;
      stages[d.stage] ||= {};
      stages[d.stage][state] = (stages[d.stage][state] || 0) + 1;
      return {
        id: row.id,
        stage: d.stage,
        target: d.input.target || null,
        state,
        available_at: row.available_at,
        depends_on: d.depends_on,
        result: JSON.parse(row.result),
      };
    });
    // A completed orchestration stage is not a completed 100-community goal.
    const completedGroups = new Set(
      rows
        .filter(
          (r) =>
            JSON.parse(r.definition).stage === "receipt_verify" &&
            r.state === "completed",
        )
        .map((r) => JSON.parse(r.result))
        .filter(
          (r) =>
            r.verified === true &&
            r.native_state === "confirmed" &&
            r.discussion_id,
        )
        .map((r) => r.discussion_id),
    ).size;
    const nativeIds = new Set(p.definition.native_job_ids);
    if (
      this.k.one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_native_links'",
      )
    )
      for (const r of this.k.rows(
        "SELECT job_id FROM workflow_native_links WHERE plan_id=?",
        id,
      ))
        nativeIds.add(r.job_id);
    const nativeJobs = [];
    for (const key of nativeIds) {
      const j = this.k.one(
        "SELECT id,kind,state,account_id,payload,result,attempt_id FROM jobs WHERE id=?",
        key,
      );
      if (!j || !p.definition.account_ids.includes(j.account_id)) continue;
      const data = JSON.parse(j.payload),
        r = JSON.parse(j.result);
      nativeJobs.push({
        id: j.id,
        kind: j.kind,
        task_id: data.task_id,
        target: data.payload?.target,
        state: j.state,
        attempted: !!j.attempt_id,
        verified: r.verified === true,
        external_reference: r.external_reference || null,
        message_ids: r.message_ids || [],
        discussion_id: r.discussion_id || null,
      });
    }
    return {
      id,
      name: p.definition.name,
      state: p.state,
      manifest_hash: p.manifest_hash,
      goal: p.definition.goal,
      goal_completed: completedGroups >= p.definition.goal,
      verified_interacted_groups: completedGroups,
      native_jobs: nativeJobs,
      execution_channel: "cloud",
      local_fallback_allowed: false,
      counts,
      stages,
      total: visible.length,
      nodes,
      steps: visible.slice(offset, offset + limit),
      next_offset: offset + limit < visible.length ? offset + limit : null,
      source: p.source,
      created_at: p.created_at,
      updated_at: p.updated_at,
    };
  }
  claim(node, id) {
    return this.k.tx(() => {
      const p = this.plan(id),
        now = this.k.clock();
      if (p.state !== "active" || this.k.settings().global_stop)
        return { step: null };
      this.k.sql.exec(
        "UPDATE workflow_nodes SET last_seen=? WHERE id=?",
        now,
        node.id,
      );
      this.k.sql.exec(
        "UPDATE worksteps SET state='pending',owner=NULL,lease_token=NULL,lease_until=0 WHERE plan_id=? AND state='leased' AND lease_until<=?",
        id,
        now,
      );
      for (const row of this.k.rows(
        "SELECT * FROM worksteps WHERE plan_id=? AND state='pending' AND json_extract(definition,'$.stage') IN (" +
          node.stages.map(() => "?").join(",") +
          ") AND available_at<=? ORDER BY available_at,rowid",
        id,
        ...node.stages,
        now,
      )) {
        const s = JSON.parse(row.definition);
        if (
          WRITES.has(s.stage) ||
          !node.stages.includes(s.stage) ||
          !this.dependenciesReady(s, this.dependencies(id, s))
        )
          continue;
        const token = this.k.uuid();
        this.k.sql.exec(
          "UPDATE worksteps SET state='leased',owner=?,lease_token=?,lease_until=? WHERE plan_id=? AND id=?",
          node.id,
          token,
          now + 180,
          id,
          row.id,
        );
        return {
          step: {
            ...s,
            plan_id: id,
            lease_token: token,
            lease_until: now + 180,
            requirements: p.definition.requirements,
            product_facts: p.definition.product_facts,
            parent_results: s.depends_on.map((d) => ({
              id: d,
              result: JSON.parse(
                this.k.one(
                  "SELECT result FROM worksteps WHERE plan_id=? AND id=?",
                  id,
                  d,
                ).result,
              ),
            })),
          },
        };
      }
      return { step: null };
    });
  }
  finish(node, input) {
    return this.k.tx(() => {
      const p = this.plan(input.plan_id),
        r = this.k.one(
          "SELECT * FROM worksteps WHERE plan_id=? AND id=?",
          input.plan_id,
          input.id,
        );
      if (
        !r ||
        r.state !== "leased" ||
        r.owner !== node.id ||
        r.lease_token !== input.lease_token ||
        r.lease_until <= this.k.clock()
      )
        throw new ControlError("WorkflowLeaseExpired");
      const s = JSON.parse(r.definition),
        result = input.result;
      if (!result || !["pass", "exclude", "defer"].includes(result.verdict))
        fail("WorkflowVerdictRequired");
      safe(result);
      if (result.verdict === "pass") this.checkEvidence(s, result, p);
      if (result.verdict !== "pass" && !result.reason)
        fail("WorkflowReasonRequired");
      let state =
        result.verdict === "defer"
          ? "pending"
          : result.verdict === "exclude"
            ? "excluded"
            : "completed";
      let next =
        result.verdict === "defer"
          ? Math.max(
              this.k.clock() + 60,
              Number(input.retry_at) || this.k.clock() + 1800,
            )
          : this.k.clock();
      if (s.stage === "discover" && result.verdict === "pass") {
        this.addDiscoveredTargets(p, result.targets);
        state = "pending";
        next =
          this.k.clock() +
          Math.max(1800, p.definition.discovery_interval_seconds || 3600);
      }
      this.k.sql.exec(
        "UPDATE worksteps SET state=?,available_at=?,owner=NULL,lease_token=NULL,lease_until=0,result=? WHERE plan_id=? AND id=?",
        state,
        next,
        canonical(result),
        input.plan_id,
        input.id,
      );
      if (s.stage === "goal_check" && result.verdict === "pass")
        this.k.sql.exec(
          "UPDATE workplans SET state='completed' WHERE id=?",
          input.plan_id,
        );
      this.k.sql.exec(
        "UPDATE workplans SET updated_at=? WHERE id=?",
        this.k.clock(),
        input.plan_id,
      );
      this.k.event("workflow.step_result", input.plan_id, {
        id: input.id,
        stage: s.stage,
        state,
      });
      return { recorded: true, state };
    });
  }
  checkEvidence(s, r, p) {
    if (!Array.isArray(r.evidence) || !r.evidence.length)
      fail("WorkflowEvidenceRequired");
    const facts = r.facts || {};
    const required =
      {
        inspect: ["identity_checked", "rules_read", "recent_context_read"],
        research_review: [
          "identity_verified",
          "discussion_entry",
          "topic_allowed",
          "free_real_identity",
          "recent_human_discussion",
          "rules_read",
          "deduplicated_discussion",
        ],
        opportunity_review: [
          "automation_allowed",
          "question_unresolved",
          "context_read",
          "reply_target_verified",
        ],
        reply_review: [
          "automation_allowed",
          "question_unresolved",
          "context_read",
          "language_matches",
          "nonduplicate",
          "claims_verified",
        ],
      }[s.stage] || [];
    if (required.some((key) => facts[key] !== true))
      fail("WorkflowQualificationIncomplete");
    if (["opportunity_review", "reply_review"].includes(s.stage)) {
      const q = r.question;
      if (
        !q?.message_id ||
        !q.discussion_id ||
        !Number.isFinite(Date.parse(q.published_at)) ||
        (this.k.clock() - Date.parse(q.published_at) / 1000 > 259200 &&
          facts.discussion_continuing !== true)
      )
        fail("WorkflowQuestionStale");
    }
    if (s.stage === "reply_review") {
      if (!r.text || typeof r.text !== "string") fail("WorkflowReplyRequired");
      const links = r.text.match(/https?:\/\/\S+/g) || [];
      const mentions = /aigram/i.test(r.text);
      if (links.length > 1) fail("WorkflowTooManyLinks");
      if (
        mentions &&
        (p.definition.product_facts?.recommendations_enabled !== true ||
          facts.relationship_disclosed !== true ||
          facts.recommendation_allowed !== true)
      )
        fail("WorkflowProductNotVerified");
      if (
        links.length &&
        (facts.links_allowed !== true ||
          !p.definition.product_facts?.links_enabled ||
          !links[0].startsWith(p.definition.product_facts.official_store_url))
      )
        fail("WorkflowLinkNotVerified");
    }
    if (["membership_verify", "receipt_verify"].includes(s.stage)) {
      const j = this.k.one("SELECT * FROM jobs WHERE id=?", r.native_job_id);
      const linked =
        j &&
        this.k.one(
          "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_native_links'",
        ) &&
        this.k.one(
          "SELECT 1 FROM workflow_native_links WHERE plan_id=? AND job_id=? AND task_id=?",
          p.id,
          j.id,
          j.task_id,
        );
      const target = j && JSON.parse(j.payload)?.payload?.target;
      const receipt = j && JSON.parse(j.result || "{}");
      const membership =
        s.stage === "membership_verify"
          ? receipt?.external_reference?.match(
              /^telegram:membership:(\d+)$/,
            )?.[1]
          : JSON.parse(
              this.k.one(
                "SELECT result FROM worksteps WHERE plan_id=? AND id=?",
                p.id,
                s.input.target.toLowerCase().slice(1) + ":membership_verify",
              )?.result || "{}",
            ).discussion_id;
      if (
        !j ||
        j.kind !== (s.stage === "membership_verify" ? "join" : "send") ||
        !p.definition.account_ids.includes(j.account_id) ||
        (!linked &&
          (!p.definition.native_task_ids.includes(j.task_id) ||
            !p.definition.native_job_ids.includes(j.id))) ||
        (s.stage === "membership_verify"
          ? !["joined", "already_joined"].includes(j.state)
          : j.state !== "confirmed") ||
        r.verified !== true ||
        r.native_state !== j.state ||
        !r.discussion_id ||
        !target ||
        target.toLowerCase() !== s.input.target.toLowerCase() ||
        !membership ||
        r.discussion_id !== membership ||
        receipt?.verified !== true
      )
        fail("NativeWorkflowReceiptRequired");
    }
    if (
      s.stage === "goal_check" &&
      this.status(p.id).verified_interacted_groups < p.definition.goal
    )
      fail("WorkflowGoalNotReached");
  }
}
