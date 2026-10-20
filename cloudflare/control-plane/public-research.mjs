// This worker can read public web pages only. It has no Telegram session or write adapter.
import { ControlError, sha } from "./kernel.mjs";

const NODE = "cloudflare-public-web";
const RESERVED = new Set([
  "share",
  "joinchat",
  "addstickers",
  "addemoji",
  "addtheme",
  "proxy",
  "socks",
  "login",
  "confirmphone",
  "setlanguage",
  "boost",
  "giftcode",
]);
function text(html) {
  return html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(
      /&(?:amp|quot|lt|gt|nbsp);/g,
      (m) =>
        ({
          "&amp;": "&",
          "&quot;": '"',
          "&lt;": "<",
          "&gt;": ">",
          "&nbsp;": " ",
        })[m],
    )
    .replace(/\s+/g, " ")
    .trim();
}
export function telegramTargets(html) {
  const candidates = [
    ...html.matchAll(
      /https?:\/\/(?:t\.me|telegram\.me)\/(?:s\/)?([A-Za-z][A-Za-z0-9_]{3,31})(?=[/\s?"'<>#&]|$)/gi,
    ),
  ].map((m) => "@" + m[1]);
  for (const m of html.matchAll(/[?&](?:amp;)?u=(a1[A-Za-z0-9_-]+)/g)) {
    try {
      const decoded = atob(m[1].slice(2).replace(/-/g, "+").replace(/_/g, "/"));
      candidates.push(...telegramTargets(decoded));
    } catch {}
  }
  return [
    ...new Set(
      candidates
        .filter((s) => !RESERVED.has(s.slice(1).toLowerCase()))
        .map((s) => s.toLowerCase()),
    ),
  ].slice(0, 30);
}
async function readPage(url, fetcher) {
  const response = await fetcher(url, {
    headers: { "User-Agent": "AutoPromotionPublicResearch/1.0" },
    redirect: "follow",
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) {
    await response.body?.cancel().catch(() => {});
    return { status: response.status, html: "", truncated: false };
  }
  const reader = response.body?.getReader();
  if (!reader) return { status: response.status, html: "", truncated: false };
  const decoder = new TextDecoder();
  let html = "",
    bytes = 0,
    truncated = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > 262144) {
        truncated = true;
        break;
      }
      html += decoder.decode(value, { stream: true });
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  return { status: response.status, html: html + decoder.decode(), truncated };
}
export class PublicResearch {
  constructor(queue, { fetcher = fetch } = {}) {
    this.q = queue;
    this.k = queue.k;
    this.fetcher = fetcher;
    if (
      !this.k.one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='public_workflow_runtime'",
      )
    )
      this.k.tx(() =>
        this.k.sql.exec(
          `CREATE TABLE public_workflow_runtime(plan_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,next_at REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,last_run REAL NOT NULL DEFAULT 0,runs INTEGER NOT NULL DEFAULT 0,reads INTEGER NOT NULL DEFAULT 0,read_successes INTEGER NOT NULL DEFAULT 0,day TEXT NOT NULL DEFAULT '',daily_runs INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,source TEXT NOT NULL)`,
        ),
      );
  }
  async start(id, source, secret) {
    if (!source) throw new ControlError("ExplicitWorkflowStartRequired", 400);
    const plan = this.q.plan(id);
    if (plan.state !== "active") throw new ControlError("WorkflowNotActive");
    this.q.registerNode({
      id: NODE,
      token_hash: await sha("public-web-only:" + secret),
      runtime: "cloud",
      stages: ["discover", "inspect"],
    });
    this.k.sql.exec(
      "INSERT INTO public_workflow_runtime(plan_id,enabled,state,source) VALUES(?,1,'queued',?) ON CONFLICT(plan_id) DO UPDATE SET enabled=1,source=excluded.source",
      id,
      source,
    );
    this.k.event("workflow.public_research_started", id, {
      source,
      scope: "public_web_only",
      paid_services: false,
      telegram_actions: false,
    });
    return {
      started: true,
      plan_id: id,
      scope: "public_web_only",
      telegram_actions: false,
    };
  }
  snapshot(id) {
    return (
      this.k.one(
        "SELECT plan_id,enabled,next_at,last_run,runs,reads,read_successes,daily_runs,state FROM public_workflow_runtime WHERE plan_id=?",
        id,
      ) || null
    );
  }
  stop(id, source) {
    if (!source) throw new ControlError("ExplicitWorkflowStopRequired", 400);
    this.q.plan(id);
    this.k.sql.exec(
      "UPDATE public_workflow_runtime SET enabled=0,state='stopped' WHERE plan_id=?",
      id,
    );
    this.k.event("workflow.public_research_stopped", id, { source });
    return { stopped: true, plan_id: id };
  }
  async tick() {
    const summaries = [];
    for (const rt of this.k.rows(
      "SELECT * FROM public_workflow_runtime WHERE enabled=1",
    )) {
      const now = this.k.clock(),
        day = new Date(now * 1000).toISOString().slice(0, 10);
      if (
        rt.next_at > now ||
        rt.lease_until > now ||
        this.q.plan(rt.plan_id).state !== "active"
      )
        continue;
      if (this.k.settings().global_stop) {
        summaries.push({ id: rt.plan_id, reason: "global_stop" });
        continue;
      }
      if (rt.day === day && rt.daily_runs >= 96) {
        summaries.push({ id: rt.plan_id, reason: "daily_public_read_budget" });
        continue;
      }
      const locked = this.k.tx(() => {
        const row = this.k.one(
          "SELECT lease_until FROM public_workflow_runtime WHERE plan_id=?",
          rt.plan_id,
        );
        if (row.lease_until > now) return false;
        this.k.sql.exec(
          "UPDATE public_workflow_runtime SET lease_until=?,next_at=?,state='running' WHERE plan_id=?",
          now + 150,
          now + 900,
          rt.plan_id,
        );
        return true;
      });
      if (!locked) continue;
      let reads = 0,
        successes = 0;
      try {
        // One search and two profile reads per run; never consume review or write stages.
        for (const stage of ["discover", "inspect", "inspect"]) {
          if (
            this.k.settings().global_stop ||
            !this.snapshot(rt.plan_id)?.enabled
          )
            break;
          const work = this.q.claim(
            { id: NODE, stages: [stage] },
            rt.plan_id,
          ).step;
          if (!work) continue;
          let result;
          try {
            const url =
              stage === "discover"
                ? "https://www.bing.com/search?q=" +
                  encodeURIComponent("site:t.me " + work.input.query)
                : "https://t.me/" + work.input.target.slice(1);
            const page = await readPage(url, this.fetcher);
            reads++;
            const evidence = [
              {
                url,
                http_status: page.status,
                checked_at: new Date(this.k.clock() * 1000).toISOString(),
                truncated: page.truncated,
                sha256: await sha(page.html),
                public_excerpt: text(page.html).slice(0, 4000),
                source_is_untrusted: true,
              },
            ];
            const blocked =
              page.status !== 200 ||
              /captcha|verify you are human|unusual traffic/i.test(
                text(page.html).slice(0, 3000),
              );
            if (!blocked) successes++;
            if (blocked)
              result = {
                verdict: "defer",
                reason: "public_source_unavailable_or_challenge",
                evidence,
              };
            else if (stage === "discover") {
              const targets = telegramTargets(page.html);
              result = targets.length
                ? { verdict: "pass", targets, evidence }
                : {
                    verdict: "defer",
                    reason: "no_verifiable_new_public_entries",
                    evidence,
                  };
            } else
              result = {
                verdict: "defer",
                reason:
                  "public_profile_read; authenticated_pins_and_recent_group_context_still_required",
                evidence,
                partial: {
                  public_profile_read: true,
                  rules_fully_verified: false,
                  recent_discussion_verified: false,
                },
                facts: {
                  identity_checked: false,
                  rules_read: false,
                  recent_context_read: false,
                },
              };
          } catch {
            result = {
              verdict: "defer",
              reason: "public_read_unavailable",
              evidence: [],
            };
          }
          this.q.finish(
            { id: NODE },
            {
              plan_id: rt.plan_id,
              id: work.id,
              lease_token: work.lease_token,
              result,
              retry_at: this.k.clock() + (stage === "inspect" ? 21600 : 3600),
            },
          );
        }
        this.k.sql.exec(
          "UPDATE public_workflow_runtime SET lease_until=0,last_run=?,runs=runs+1,reads=reads+?,read_successes=read_successes+?,day=?,daily_runs=CASE WHEN day=? THEN daily_runs+1 ELSE 1 END,state=CASE WHEN enabled=1 THEN 'waiting_schedule' ELSE 'stopped' END WHERE plan_id=?",
          this.k.clock(),
          reads,
          successes,
          day,
          day,
          rt.plan_id,
        );
        summaries.push({
          id: rt.plan_id,
          public_reads: reads,
          successful_public_reads: successes,
          telegram_actions: 0,
        });
      } catch {
        this.k.sql.exec(
          "UPDATE public_workflow_runtime SET lease_until=0,state='waiting_retry' WHERE plan_id=?",
          rt.plan_id,
        );
        summaries.push({ id: rt.plan_id, reason: "public_research_deferred" });
      }
    }
    return { runs: summaries };
  }
}
