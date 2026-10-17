# Explicit task execution

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
