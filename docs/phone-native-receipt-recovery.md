# Facebook and Instagram phone receipt recovery

The normal `verify_task_item` MCP tool and item reconciliation API can request a bounded read of the original native submission. This does not start or retry publication. The original account, task, item, attempt, execution, used one-use authorization and immutable request hash must match.

Receipt jobs are persisted in the business database and survive MCP disconnection. The device center owns browser read scheduling and signed observations; the business service only polls those observations and adds verified publication evidence to the original attempt. Neither service reads the other's database.

`receipt_followup_enabled` defaults to false. When enabled for a coordinated release, newly attempted supported phone sends may create a receipt job after an uncertain outcome. It does not enroll historical unknown items on startup. Normal status and statistics queries cannot create a platform receipt read; explicit verification supplies the separate read intent.

Global stop, task pause and cancel record a local stop and cancel the exact remote check, even if the check-creation response was lost. Unknown results and send reservations are preserved. Resuming reads requires an explicit `verify_task_item(item_id, resume_receipt=true)` invocation; it uses a new read generation and never resets the send attempt or permission.

`require_workflow_heartbeat` also defaults to false for compatibility. Enable it only after deploying the coordinated worker/API update. The worker posts its own authenticated loop heartbeat. Status exposes loop presence separately from server availability and publication permission. A missing or stale heartbeat can reject preparation before expensive phone work.

Local Meta outcome blockers are evaluated before queue leasing/account handoff and before native preparation. A deterministic blocker waits for local state changes; scheduler ticks can re-evaluate local facts without repeating platform health or phone observations. Global/account stops, permissions, cooldowns, quotas, target deduplication and final fresh checks remain authoritative.

Build a code-only release manifest with `python -m auto_promotion.runtime_release --source promotion=<source-dir> --source phone=<source-dir> --source fleet=<source-dir> --output <new-manifest>`. Use an optional base manifest and exact expected changed-file list to reject no-op or extra patches. Private configuration, databases and screenshots are not inputs. Verify actual MCP, sender, node and worker runtime reports; HTTP health alone and stopping the scheduled launcher do not prove the old child process exited. Loading requires the existing idle/drain procedure and a matching release fingerprint.

All added tests use isolated databases and synthetic receipts. Do not treat them as live publication acceptance. Before later user-authorized validation, preserve the current unknown items and query their original receipts first; never create replacements for their possibly completed sends.


The existing task evidence/export entry now adds `phone_execution_chain`: exact item, attempt, original execution, command, consumed authorization, native command and receipt-check IDs with the next read checkpoint. It uses indexed item lookups and a two-second query budget, emits selected metadata only and never fetches a platform page. Runtime startup fingerprints include all receipt, workflow and evidence modules. Phone stage journals persist a running marker before each measured operation and its elapsed/completion outcome afterwards; existing deterministic operation step/deadline budgets remain in force.
