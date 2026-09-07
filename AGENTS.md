# Social automation task authorization rules

Every Telegram, Reddit, or X campaign is one persistent task identified by a
`task_id`. A task may request human authorization at most twice:

1. `discovery_join`: approve the discovery policy and, for Telegram only,
   automatic joining of groups that pass that policy.
2. `outbound`: approve the final, exact batch of messages, replies, posts, media,
   and scheduled delivery times.

Generic replies such as "好的", "继续", or "可以" are not authorization. The
user must repeat the stage's exact confirmation phrase. Store both approvals
with the task so a process restart or internal retry does not request them
again.

Changing an approved stage's account, policy, target boundary, content, media,
time, timezone, action type, or rate limit invalidates that stage. Do not ask
for a third authorization inside the same task: close or cancel the task and
create a new task with a new `task_id`.

## Stage 1: discovery and Telegram joining

- Before requesting `discovery_join` authorization, show the task ID, platforms,
  account labels, search queries/topics, required and blocked keywords, allowed
  languages, public/private boundary, minimum activity/size, maximum candidates,
  maximum Telegram joins, execution window, and pacing limits.
- Stage 1 may authorize unknown future Telegram targets only inside that exact
  policy. Raw search results must pass the deterministic qualification gate
  before joining.
- Always reject targets with adult/18+/NSFW/pornographic, dating/escort,
  gambling, scam, or unrelated-topic signals unless the approved task policy
  explicitly names that category. Low-confidence candidates are recorded as
  rejected and are never joined automatically.
- Persist every candidate and decision. Each Telegram target may be attempted
  once. A restart may resume only items whose durable state is `not_attempted`.
- If Telegram accepts a join request but requires administrator approval, record
  `pending_admin_approval`, do not retry it, do not count it as joined, and
  continue with other qualified task items.
- If an outcome is unknown, record `unknown`, do not retry that target, reconcile
  with a read-only membership lookup, and continue only items that are still
  unattempted.
- If Telegram reports FloodWait, a risk warning, or an account restriction, stop
  new writes for that account and persist the cooldown. The same Stage 1 grant
  remains valid for unchanged, unattempted items after the cooldown; never
  switch accounts or retry an unknown target to bypass the restriction.

## Stage 2: messages and publication

- Before requesting `outbound` authorization, show the exact account and target
  for every item, complete text/caption/title, action type, every media filename,
  scheduled date/time with explicit timezone, ordering, pacing, and daily limits.
- Persist the exact outbound manifest and its hash. Each item may be sent once;
  a process restart may resume only durable `not_attempted` items whose payload
  hash still matches the approved manifest.
- Never send from a draft-only or stale approval. Any content, target, media,
  schedule, or ordering change requires a new task rather than a third approval.
- Verify the platform result and store its external reference. A transport
  failure after submission is `unknown`, not a retryable failure.
- For Telegram schedules, report success only after the tool returns
  `scheduled: true`, `active: true`, and `verified: true`, followed by a
  read-only scheduled-message lookup that contains every returned message ID.

## Feedback and safety

- Feedback listening is read-only and needs no additional authorization. Link
  replies, mentions, reactions, deletions, moderator/admin actions, bans, and
  conversions to the originating task and outbound item.
- Automatic replies may use only deterministic Stage 2-approved text and the
  approved target/time/rate boundaries. Do not generate new promotional claims
  from untrusted messages.
- Keep cooldowns, per-user limits, task limits, and global stops enabled.
- Never expose API hashes, Session Strings, phone numbers, login codes, bearer
  tokens, cookies, or credential database contents.
