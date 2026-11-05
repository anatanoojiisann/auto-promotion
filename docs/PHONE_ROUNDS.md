# Six-app timed rounds: development handoff

This is an opt-in development feature, not a production deployment or a measured ten-minute real-device result.

## Delivered behavior

- One durable round links six original, frozen, unattempted live phone tasks. Each task has one automatic interaction and one account, covering Telegram, Reddit, Instagram, Facebook, X and TikTok exactly once.
- Creation never starts a task. Explicit start sets one non-renewable 600-second clock and a 480-second cutoff for new send grants. Queue waiting, stopped time, startup failures and account cooldowns are not subtracted.
- Admission uses the existing Engine and persistent JobQueue. There is no second sender. A round start reuses fresh cached identity; stale identity fails the normal health/preflight checks. The device still verifies identity, target, exact text and separate one-use permission immediately before sending.
- Transactional guards apply at original start, item claim, provider claim verification, and final native permission for both Social and Reddit. The gates enforce the cutoff even if the coordinator or status client is down. Round stop fences future grants, preserves uncertain outcomes and leaves original read-only receipt lookup available. A previously accepted native permit may already be in flight; stop is not a retraction.
- An interrupted start is never automatically replayed. Active original tasks can rejoin their original queue; unstarted interrupted tasks are blocked for inspection. Admission is serialized between coordinator workers. Actual execution continues to use existing account/device locks.
- Status keeps original task/item/attempt/execution IDs, queue time, time before native authorization and receipt waiting time. Only a verified original item with an external reference and an original completed attempt is counted. Late receipts update current status without turning a missed deadline into a pass. The terminal cutoff snapshot is retained.
- Local preflight reports blockers for all six apps without navigating a phone, calling a social platform or granting submission permission.
- Candidate shortlist reuses up to three existing content records per account. It requires recent data, current-version review, explicit account-bound permission evidence, a contextual draft, enough remaining lifetime, and no historical attempt or active scheduled duplicate. Unknown attempts remain query-only. Empty pools are reported; no automatic generic promotional text is generated.
- Prepared evidence is looked up by immutable content, original task/item and device/client/reader binding with an index, including when more than 1,000 unrelated historical operations exist.

## Entry points

API factory: `create_app(..., phone_rounds_enabled=True, worker=True, pooled_worker=True, phone_social_config=..., phone_reddit_config=...)`. Supply existing protected service configuration through the normal deployment composition. Do not put tokens in requests or this document. CLI: `auto-promotion serve --execution-mode pooled --phone-rounds` enables routes; actual phone bindings still need the existing protected configuration. The CLI flag does not commission readers or enable live writes.

Existing local API authentication and cross-origin/CSRF checks apply to every route:

- `POST /api/phone-rounds` with `task_ids` (six original task IDs) and a stable `request_key`.
- `GET /api/phone-rounds/{id}/preflight` checks cached evidence/configuration only. It does not prove live remote authorization or current target permissions.
- `POST /api/phone-rounds/{id}/start` records explicit intent and begins timing. `worker=False` records waiting without secretly starting an executor.
- `GET /api/phone-rounds/{id}` returns JSON status and timing suitable for exporting as diagnostic evidence. Native permit time is not itself a confirmed publication.
- `POST /api/phone-rounds/{id}/stop` blocks further grants. It cannot resume or reset a timed-out round.
- `GET /api/phone-round-candidates/{account_id}` returns the local reviewed shortlist. Normal campaign planning/freezing and send-time checks remain required.

## Current acceptance boundary

The timer begins at explicit start of already prepared original tasks. Upstream content discovery, review, task creation and prior dry preparation are preconditions; their earlier wall time is not represented as part of this round. A cold-start end-to-end ten-minute claim therefore requires a higher-level entry point that includes those steps. This release does not claim that acceptance.

Auto Promotion here retains its existing `phone-fleet-management==0.2.0.dev17` / fleet protocol 0.1.3 dependency. The phone development branch uses protocol 0.2.0. They were tested in separate owned virtual environments. They must not be deployed together without a versioned integration upgrade and cross-service validation.

Still required before claiming the full optimization plan complete:

1. Commission independent receipt readers for each actual platform/account with permitted protected authorization; cached preparation or an editor is not public success.
2. Formalize the versioned prepared-editor ownership handoff to avoid typing/reading/clearing and then typing again. Do not adopt a user draft or drop the final fresh checks.
3. Formalize receipt-pending/native-quiescence handling before releasing a phone while independent API readback continues. The existing unknown quarantine and signed reconciliation remain unchanged. This branch does not release a device just because a background task was launched.
4. Move Telegram reservation ahead of statistics contention through the same authoritative real-account ledger. Existing prepaid read budgets remain enforced; this change does not add quota, reset windows, or disable other account guards.
5. Run real-device cold and warm rounds, log every stage from content selection to final native receipt, and measure repeated success before asserting a 10-minute service level.

Shared wire contracts and integration/deployment changes belong to the integration checkout. No production service was restarted, no message was sent, and no branch was pushed or merged by this development work.

## Validation

See the final handoff for exact commands, results and commit IDs. All new tests use explicit synthetic tasks, temporary databases or mock transports. External provider and hardware acceptance must be reported separately.


## Exact changed files

- `src/auto_promotion/phone_rounds.py`: persistent round coordinator, original-ID status/timing, admission and cutoff guards, metadata-only original queue recovery.
- `src/auto_promotion/phone_rounds_api.py`: authenticated create/start/status/stop/preflight/candidate routes.
- `src/auto_promotion/phone_round_readiness.py`: local readiness and reviewed shortlist.
- `src/auto_promotion/api.py`: opt-in API and independent durable round coordinator loop.
- `src/auto_promotion/cli.py`: explicit `--phone-rounds` enablement.
- `src/auto_promotion/core.py`: original start/claim guards and round-only fresh-cache admission.
- `src/auto_promotion/backends/common.py`: original provider claim guard.
- `src/auto_promotion/phone_social.py`: indexed original preparation lookup and transactional final permission guard.
- `src/auto_promotion/phone_reddit.py`: transactional final permission guard.
- `src/auto_promotion/phone_telegram.py`: original task/item preparation scope.
- `src/auto_promotion/readiness.py`: actionable round blocker messages.
- `tests/test_phone_rounds.py`: synthetic persistence, deadline, concurrency, stop, unknown, late receipt and original queue recovery tests.
- `tests/test_phone_round_readiness.py`: synthetic age, revision, account permission, draft and duplicate filtering tests.
- `tests/test_phone_social.py`: original lookup past 1,000 unrelated operations and final-permission stop race for five apps.
- `tests/test_phone_reddit.py`: Reddit final-permission stop race.
- `docs/PHONE_ROUNDS.md`: this handoff.

## Measured development checks

From `D:/Projects/auto-promotion-ten-minute`, using its independent Python 3.12 virtual environment:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_phone_rounds.py tests/test_phone_round_readiness.py tests/test_phone_social.py tests/test_phone_reddit.py tests/test_phone_protocol_receipt_gateway.py tests/test_phone_identity_refresh.py tests/test_api.py tests/test_start_api_idempotency.py tests/test_start_account_checks.py tests/test_engine.py tests/test_persistent_pools.py tests/test_task_channels.py tests/test_telegram_receipt_budget.py tests/test_telegram_read_budget.py -q --tb=short --durations=8 --junitxml=.local/ten-minute-validation/regression.xml
```

Result before the final metadata-only recovery correction: **249 passed, 14 subtests passed, 392.95 seconds**. This is development test wall time, not real six-app execution time. The longest test was the existing simulation coverage of all MVP outbound actions (63.13 seconds); independent-process pool coverage took 11.06 seconds.

The final correction explicitly synchronizes the original JobQueue before changing an interrupted active member to queued, retains the intent if metadata sync fails, and allows other apps to progress. Verification after that correction:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_phone_rounds.py tests/test_phone_round_readiness.py -q --tb=short --junitxml=.local/ten-minute-validation/recovery.xml
```

Result: **36 passed, 11.53 seconds**. It includes two new cases for interrupted active startup and a failed first metadata synchronization, proving the original task is not started again and only one queue reference is created.

All 15 changed Python files passed `ruff check`; `git diff --check` passed. The companion fleet branch full suite passed 51 tests with four external integration skips. Logs and XML live under `.local/ten-minute-validation/` and are intentionally excluded from the commit by explicit file staging. No runtime database, protected configuration, screenshot, social credential or virtual environment is included in the source commit.
