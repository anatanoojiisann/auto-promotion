# Independent safety review: outbound account blocks

Date: 2026-10-23. Reviewer: independent security_review agent; no functional edits made by reviewer.
Workspace: <workspace>
Base: origin/main 40d270b2b2abfbe0be029782d1731a0bc995bcda.

## Current conclusion

No open P1 or P2 findings remain in the reviewed change. All reproducible findings below were repaired and independently rechecked. This is an approval of the local implementation for the reviewed scope, not authorization to deploy, run production tasks, or send real messages. Full-suite/build results remain the implementation coordinator’s separate evidence.

## Scope and safeguards reviewed

Reviewed the complete changed Python control path, Telegram classification and transport diagnostics, X browser detection and response parsing, persistent account blocks and private journals, queue claim and final authorization, identity/generation-bound recovery, API/UI, task evidence export, Cloudflare claim/authorization/completion and snapshot merge, standby/offline permits, and Telegram cloud executor classification.

Observed retained safeguards: once-only attempts and terminal unknown results; durable IDs and references take precedence over failure classifications; account lanes, private OS account locks and cloud fencing remain; recovery does not requeue attempted, unknown or expired items; identity health checking and explicit recovery are distinct; no automatic channel switch; manual stops do not expire with ordinary cooldowns.

No production task, real message, real account login, captcha resolution/bypass, credential registration, deployment, push, or local-memory modification was performed by this reviewer. Browser tests intercept all requests; cloud tests use synthetic credentials and local state. macOS results do not establish Windows field behavior.

## Findings and closure evidence

1. P1: same-reason fresh evidence retained an earlier successful recovery check. Fixed by event deduplication plus invalidating successful checks/generation on new observations. Independently verified in account-block tests.
2. P1: cleared local state exported health_ok=false and was rejected by the cloud merge. Fixed; independently executed actual Python check/resume/export through the production Worker kernel and confirmed stale active snapshots cannot reverse the clear.
3. P1: bridge initialization submission_started=false incorrectly classified Instagram/Discord submission timeouts as known not submitted. Removed the generic inference; unrelated adapters retain unknown semantics.
4. P1: central database mirror failure discarded detected block evidence. Fixed by private per-event journals and retaining provider evidence; independently verified central-failure/restart and final-authorization regressions.
5. P1: read/compare/unlink could delete a concurrently replaced newer private event. Fixed with per-event files and deletion of only the specific event; independent focused regression passed.
6. P2: Telegram disconnect failure overwrote a definite UserBannedInChannelError rejection. Fixed; independent Telegram regression passed and preserves teardown diagnostics separately.
7. P1: a new verification of a historical unknown receipt reused its prior account_block_event_id, retaining an old successful recovery check. Fixed by excluding historical guard fields and generating a fresh observation ID; independent native-to-Engine event and clean-receipt regressions passed. Durable references and unknown state remain.
8. P1: partial/fsynced staging files left before rename were ignored on restart. Fixed: matching staging remnants now fail closed as account_guard_unavailable. Independently reran partial and complete staging crash cases through reader, final authorization and Engine restart.
9. P1: journal write/rename failure discarded provider evidence before central persistence. Fixed by retaining the pending guard and independently attempting central persistence even when the private journal fails. Independently verified that the original platform reason, status and block remain and the central block is durable. A simultaneous failure of both guard stores now returns an explicit account_guard_unavailable / guard_persistence_failed signal; this does not claim restart durability when every guard storage path is unwritable.
10. P2: the cloud Telegram adapter labeled an already submitted but explicitly rejected RPC as confirmed_not_submitted. Fixed and independently rechecked: submitted refusal uses confirmed_rejected, pre-submission refusal uses confirmed_not_submitted, and accepted IDs keep unknown with both flags false.

The suspected standby/offline identity-alias bypass was ruled out: existing single-identity-alias fencing rejects aliases, and independent tests used an actual standby-generated signed checkpoint. A CI coverage gap was corrected by adding the new opt-in X browser-stage test file to the browser job.

## Independently executed checks

| Check | Actual result | Evidence |
| --- | --- | --- |
| Final account blocks, API, Telegram restrictions, historical X event, native backends | 69 tests passed, no skips, 9.809 s | /tmp/independent-outbound-python-final.log |
| X signal/unit + Chromium stage + existing browser contracts + owned posts | 44 tests passed, no skips, 47.951 s | Tool run output; synthetic intercepted pages |
| Final X detector after historical-event repair | 21 tests passed, no skips, 16.264 s | /tmp/independent-outbound-x-final.log |
| Recovery UI | 11 scenarios passed | Tool output from npm run test:ui:blocks |
| Cloud client/snapshot/import/recovery integration | 26 tests passed, 2.970 s | /tmp/independent-outbound-cloud-client.log |
| Cloud kernel, standby, offline blocks and Worker-channel enforcement | 59 tests passed, no skips | /tmp/independent-outbound-cloud.log |
| Final cloud Telegram adapter and executor core | 11 tests passed, no skips | /tmp/independent-outbound-executor-final.log |
| Added partial/fsynced staging crash test | 1 test passed, two subcases | Direct unittest tool output |
| git diff --check | Passed | Direct tool output |

Setup failures are not passes: sandbox Chromium launch initially aborted; approved isolated escalation succeeded. A sandbox cloud test could not bind localhost; approved local-only escalation succeeded. An initial cloud-client invocation used a nested working directory and failed module resolution; the corrected root invocation passed. A short environment transport interruption was retried for read-only inspection after availability returned.

## Limits

The implementation coordinator owns full-suite, lint, format, build and complete UI acceptance results; this independent review does not claim those were independently rerun. Python 3.11, Windows field execution, real platform challenge behavior and live delivery were not validated by this reviewer. Unreadable/corrupt guard storage deliberately remains stopped and requires repairing that storage before normal recovery can succeed. If both private guard storage and the central database cannot persist writes, the code can report and retain a stop for its caller but cannot guarantee that an unpersisted observation survives a process or machine restart; no test or conclusion here asserts otherwise. These are explicit storage-availability limits, not evidence of successful durable recovery. The reviewed scope is the built-in Playwright X path identified in the supplied failure evidence; this review does not establish detection coverage for independent third-party browser channels.
