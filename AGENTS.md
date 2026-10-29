# Explicit task execution

On 2026-10-26 the user explicitly approved: "启用 TikTok 真实外发，仅执行明确启动的任务".
Setting `AP_TIKTOK_OWNER=auto-promotion` and adding the registered TikTok desktop
account/pool to the existing executor node are authorized for this commissioning.
This is not task-start authorization: recipients, exact text and explicit task
start remain required. Global live/stop settings, account stops and quotas must
not be changed. Do not ask again for this already granted commissioning approval.

On 2026-10-26 the user explicitly requested TikTok private-message collection and
proactive messaging. Enabled `tiktok_desktop` accounts additionally support
`send_direct_message` and `reply_direct_message`. Private inbox observations stay
in the local DM tables, outside the shared content pool and notification bodies.
Use the same explicit task start, queue, one-use guard and independent native
message-ID receipt. Missing send controls, restricted recipients, message requests
requiring acceptance and uncertain outcomes never authorize a send or a retry.
Collection and enabling task capability never start a task. Do not enable global
live writes or clear global/account stops. See docs/tiktok-private-messages.md.

TikTok web development was explicitly requested on 2026-10-26. Registered
`tiktok_desktop` accounts may execute exact text comments/replies through their
existing browser only after the corresponding normal task is explicitly started.
Use `tiktok_browser_begin`, a fresh `tiktok_browser_guard` immediately before the
single click, and independently observed `tiktok_browser_result` receipts.
Unknown results never resend. A reply requires independently verified parent
comment ID and author; missing DOM IDs remain unknown. The background adapter
never submits. Do not enable live switches or copy browser credentials.
See docs/tiktok-browser.md. The original TikTok API retains its owned-content rules.

Facebook / Instagram browser integration was explicitly requested on 2026-10-26.
Registered Meta desktop accounts may execute exact text comments and replies
through their EXISTING Tabbit / Chrome / Edge connection. This new scoped route
does not change legacy manual-comment items. Use the normal explicit start,
persistent queue, meta_browser_begin, the one-use meta_browser_guard immediately
before clicking, and independent receipt tools. See docs/meta-browser-integration.md.
Never submit from the background adapter, replay unknown outcomes, restart
imported history, or copy browser credentials.

A user may start Telegram, Reddit, Instagram, X or Discord work by giving Codex
an explicit instruction to send, reply, interact or join for a specified task,
or by clicking the corresponding Start button in the local client. Do not ask
for approval phrases or route the user through discovery/outbound approval stages.
This replaces the former two-stage approval workflow at the user's request.

Drafting, editing, importing, planning and previewing are local preparation and
must never start external writes. Use the MCP `start_task` or `start_joins` tools
only when the human explicitly requests that execution. Generic acknowledgments
without a clear execution instruction do not start a task.

## Task scope and execution

- Keep a persistent `task_id`, account/policy snapshot and exact immutable outbound
  manifest with hashes. Starting sending automatically freezes the manifest.
- Record each explicit start, its source, task phase and manifest hash in the audit
  log. Keep historical approval records as read-only evidence; never create new ones.
- Candidate discovery and local item editing need no approval. Only explicitly
  started tasks may join or send. Qualified Telegram joins may run within the task
  policy; tasks waiting for membership automatically continue after verification.
- Preserve candidate qualification, target boundaries, media verification, account
  health, cooldowns, global stops, quotas, pacing and delivery windows.
- Do not enable live writes or release a global stop merely to start a task.
- Keep platform execution support unchanged. A start instruction does not bypass
  platform permissions or turn a manual action into browser automation.
- Each outbound item and Telegram target may be attempted once. Resume only durable
  `not_attempted` items. An outcome after uncertain submission is `unknown`; do not
  retry it. Reconcile through read-only lookups.
- Record Telegram administrator review as `pending_admin_approval`, not joined;
  do not retry it. FloodWait, risk warnings and restrictions persist cooldowns.
- Verify platform results and save external references. Telegram schedules require
  `scheduled: true`, `active: true`, `verified: true` and a lookup containing every
  returned message ID. Local pause/cancel does not retract accepted schedules.
- Changes to frozen content, accounts, targets, media, ordering or schedule require
  a new task. No new approval process is introduced.

## Feedback and privacy

Feedback and receipt verification are read-only. New reply content enters a new
prepared task and needs an explicit start instruction or client button click.
Never expose API hashes, Session Strings, phone numbers, login codes, bearer
tokens, cookies or credential database contents.

## Persistent execution pools

The production sender uses persistent jobs and Cloudflare authorization. Explicit
starts queue work; account and platform processes execute it asynchronously. Do
not bypass the queue with direct adapter calls. Read `/api/execution/pools` for
pool health and pending receipts; a parent heartbeat alone does not prove a pool
is working. See `docs/cloudflare-execution.md`.

The user requested cloud-first dual-channel execution, then explicitly required
the local channel to work independently without server initialization. A
`cloud_then_local` policy with a durable `local_channel_authority` may consume
the SAME local persistent queue for the explicitly scoped accounts. An explicit
local-today handoff or `CloudWriteQuotaExceeded` can select that channel; no cloud
checkpoint or server request is required. Keep all local stops, quotas, leases,
manual boundaries and once-only outcomes. Do not unset `cloud_control_required`
or use direct/legacy calls. Server-side fencing prevents parallel online claims
during today's handoff. Reconcile local receipts and quotas before returning to
cloud. See `docs/execution-channels.md`.

An MCP connection opened before an upgrade may still have older code in memory.
The database blocks attempts without the owning pool lease. If that older client
reports `PersistentJobLeaseRequired` or a missing pool function after a start,
read the task and queue status before doing anything else; the explicitly started
task can already be queued. Refresh that MCP connection, and never create a new
item or repeat an uncertain send to work around the error.


## Execution diagnosis and channel intent (2026-10-28)

- Before preparing or continuing work, identify the user's requested channel, scoped
  accounts/tasks, current authorization and expiry, actual execution node, and the next
  permitted action. Persist a human instruction reference in `execution_channel_source`
  when known. Do not invent a reference or infer an instruction from an audit source label.
- An explicit online/local instruction must use `cloud`/`local`; never leave it as
  `inherit`. Intentional following of the default remains `inherit` and must be described
  as dynamic. Never silently rewrite a form's selected channel to satisfy platform support.
- Cloud authorization/queue management and the physical browser location are separate.
  A browser on this PC does not prove independent-local authorization. Report both.
- Local-only authorization belongs to its recorded accounts/tasks and deadline. Never
  expand it to another account, reinterpret expiry as online permission, or bypass a
  cloud ownership fence. If independent local proof is missing, explain the missing proof.
- Explicit online failures wait online; only an authorized quota fallback may switch an
  inherited route. Network, login, verification and read-budget errors are not write-quota
  failures. Cloud read exhaustion does not itself authorize local sending.
- Distinguish waiting for schedule/pacing, item/target dependencies, account restrictions,
  channel ownership, execution-pool failure and receipt verification. Name the specific
  blocked task/item/account and next retry time; do not call future schedules deadlocks.
- Execution completion is distinct from delivery success. Completed tasks may still have
  unknown, platform-scheduled or manual items. Never convert them to success or resend.
  `published` dependencies require verified publication; use `finished` only when the human
  intended execution completion. A permanent dependency failure or deadline ends the wait.
- Judge pre-submit failures from durable no-submit proof; after uncertain submission use
  read-only reconciliation. Do not override one-attempt, account challenges or platform review.
- Attribute failures only to evidence: SQLite error codes/stages, current pool heartbeats,
  per-task channel decisions and native receipts. A parent heartbeat, a historical global
  cloud error or an HTTP success alone does not establish publication or the current blocker.
- Validate complete source snapshots before runtime activation. Never leave merge markers
  or partially edited source in the live executor path. Preserve user work and frozen hashes.
