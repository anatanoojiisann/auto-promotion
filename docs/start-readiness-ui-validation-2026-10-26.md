# Start and readiness UI/API regression — 2026-10-26

Local checkout: `<workspace>`,
based on `7c9ee153e128964fd10c96ffc7c3555cf8abcd44`. Unrelated private assessment material is excluded from the public change. This report covers local
source and isolated fixtures only; it does not identify the code running on a
field worker or establish a Windows/platform deployment result.

## Changes and observed failures

- A pending Start formerly disabled only its original button. Refreshing the
  same task created a new enabled button and could issue another request. The
  UI now merges the same task's pending operation and keeps redrawn controls
  disabled until it completes.
- Late Start results/errors could overwrite another task's notice or an open
  draft. Same-route task GETs could complete out of order. Both render and
  polling responses now check route/view/render generations; polling success
  and failure cannot replace a newer same-route observation.
- A Start response's older readiness message could announce success after a
  newer GET showed a stop. Notices now use the current task view, including
  schedule or real gate information. Service retry time and original scheduled
  send time have separate labels.
- Intent-owner references were available from core but discarded by the UI.
  Readiness now shows the blocking task/item identifiers, with HTML escaping.
- Delayed template-save success could close a newer template draft. The
  response now checks its panel generation. The same checks were applied to
  local task/account/item/manual-result/candidate saves and stale item-editor
  lookup/upload responses. Closing a form does not cancel a request already
  accepted by the server; it prevents that response from replacing a newer form.
- Readiness preserves the future schedule alongside service observations.
  Only a fresh, exact-task, validated, unexpired successful scoped observation
  can explain an older global `FrozenJobChanged`. The old global observation is
  still shown. A later global error, write quota/configuration failure, stale or
  invalid task scope, and unvalidated/foreign local scope remain blocking
  observations. Equal-timestamp global/task observations remain conservative
  because their order is not proven. Global stops, live-write flags and supplied account safety
  blockers take priority over active-task item/service waiting states.

The initial task UI run passed one existing schedule case and reproduced eight
failures. Additional expected-red cases reproduced omitted intent-owner
references and a delayed template save closing the next draft. Pure readiness
cases initially failed for scope/schedule fields and account/foreign-local
priority. No platform call or actual task was needed to demonstrate these bugs.

## Validation

| Check | Result |
| --- | --- |
| New task-start/response UI fixtures | 16 passed after final review correction |
| Task-scoped readiness pure fixtures | 10 passed after final review correction |
| Actual HTTP API with temporary Store | 4 passed |
| Existing cloud workflow presentation fixtures | 8 passed |
| Target Ruff check / format | Passed before final full run |
| Configured UI Prettier range | Passed before final full run |
| Full seven-suite offline UI | Passed across the final main/pool/interaction run and focused remainder run |
| Existing task-readiness tests | 30 passed after the core membership fix |

The HTTP fixtures use a fake clock and only a simulation account. Repeated Start
keeps the frozen manifest/items, produces no attempts before due time, and calls
neither health nor dispatch again; it reports the current global stop and still
rejects changed account configuration, missing authentication and terminal tasks.
The helper's binding/configuration invalidation is covered by the separate cloud
control tests. Presentation evidence is never a claim or submission authorization.

The first full UI attempt exposed an old acceptance-test assumption: in a
`--no-worker` simulation service, the script used a second Start to advance the
scheduler after clearing a stop (the pool script used the same pattern after
waiting for due time). Repeated Start is now intentionally read-only. Those
fixtures now first verify `mode=simulation`, `already_started=true` and empty
`processed`, then explicitly run a simulation scheduler tick through the local
fixture `/run` route. Existing checks require live writes disabled and synthetic
accounts. No production behavior or final gate was relaxed to satisfy the tests.
The prior failing logs are preserved as `ui-full-prior-start-assumption.log`
and `ui-full-prior-discord-start-assumption.log`.

The next full run passed main UI (45 named checks), pools (2 synthetic accounts,
3 contents, 2 batches and 3 confirmed simulations), and interactions (22 named
checks). Its fault suite detected a current-settings-GET error hidden by the new
common busy guard: the settings loader deliberately increments its own panel
generation. The loader now reports its own still-current error inside its
existing request-generation guard. This regression was introduced during this
change and fixed without weakening stale-response protection. Its failing log
is `ui-full-current-settings-error.log`. The final focused remainder run exited
0: faults 64, account recovery 11, Telegram read settings 11, task Start 15.
Previously passing main/pool/interaction suites were not rerun unnecessarily.
Final UI evidence therefore spans `ui-full.log` and `ui-remainder.log`; the old
full-run failure is not described as a single all-suite successful process.
Target Ruff/format, full configured Prettier and `git diff --check` all passed.
Validation used macOS arm64, Node 24.13.0, Python 3.14.2, Playwright 1.62.1 and
Chrome 155.0.8059.86; final remainder completed at 09:29 UTC.

## Reproduction and evidence

```sh
PYTHONPATH=src /absolute/path/to/python -m unittest discover -s tests -p test_start_api_idempotency.py
PYTHONPATH=src /absolute/path/to/python -m unittest discover -s tests -p test_readiness_scope_display.py
AP_UI_CHROMIUM_PATH=/absolute/path/to/test-chrome npm run test:ui:task-start
AUTO_PROMOTION_PYTHON=/absolute/path/to/python \
AP_UI_CHROMIUM_PATH=/absolute/path/to/test-chrome \
node scripts/test_ui_offline.cjs main pools interactions faults blocks telegram_reads task_start
npm run format:check
```

Logs: `/tmp/ap-start-readiness-validation-20261026/`. Expected-red logs include
`ui-red.log`, `ui-owner-red.log`, `ui-template-red.log`, and `readiness-red.log`.
Temporary services/profiles and SQLite stores are isolated; the new task-start
UI suite intercepts every request with local synthetic fixtures. Tests reuse
installed local Python/Playwright dependencies. No real accounts, credentials,
production tasks, live Cloudflare/Windows actions, commits, pushes or deployment
were performed by this UI/API task. Full Python/Worker results and independent
security review are reported separately.

## Final independent-review correction (09:42 UTC)

Review reproduced one additional presentation blind spot: a fresh, exact-bound
failed task sync (`FrozenJobChanged`, `execution_ready=false`) was hidden by an
older global `connected` record. Expected-red pure fixtures reproduced two
incorrect ready/schedule results and a missing failed-observation field. The
workflow now separates a valid bound observation from a successful one. Without
valid local authority, the failed task sync is shown as waiting for this task's
sync, with its own retry time and the original schedule retained. The UI displays
the task failure code/time/retry even if the global record says connected.

A stale, foreign, expired or invalidated failure does not become a new blocker.
A valid local fallback remains independent: it can keep its schedule while the
failed cloud task observation stays visible. No claim, final authorization or
channel authority was changed by this correction.

Final targeted results: readiness scope **10/10**, task UI **16/16**, actual
fixture API **4/4**, existing cloud-workflow presentation **8/8**; targeted Ruff,
Prettier and diff checks passed. These supersede the earlier 7/15 target counts.
The seven broad UI suites were not repeated for this narrow correction. Logs:
`scope-failure-red.log`, `scope-failure-green.log`, `ui-scope-failure.log`,
`api-final-scope.log`, `cloud-workflow-final-scope.log`, `format-final-scope.log`.
Runtime source was frozen before the lead's final complete Python validation;
only this documentation appendix was subsequently added.
