# Scoped phone rounds: development handoff

This is an opt-in development feature, not a production deployment or a measured ten-minute real-device result.

## Delivered behavior

- One durable round links original, frozen, unattempted live phone tasks. Each task has one automatic interaction and one account. Select any nonempty subset of the six supported platforms. Each selected platform appears once.
- Creation never starts a task. Explicit start sets one non-renewable 600-second clock and a 480-second cutoff for new send grants. Queue waiting, stopped time, startup failures and account cooldowns are not subtracted.
- Admission uses the existing Engine and persistent JobQueue. There is no second sender. A round start reuses fresh cached identity; stale identity fails the normal health/preflight checks. The device still verifies identity, target, exact text and separate one-use permission immediately before sending.
- Transactional guards apply at original start, item claim, provider claim verification, and final native permission for both Social and Reddit. The gates enforce the cutoff even if the coordinator or status client is down. Round stop fences future grants, preserves uncertain outcomes and leaves original read-only receipt lookup available. A previously accepted native permit may already be in flight; stop is not a retraction.
- An interrupted start is never automatically replayed. Active original tasks can rejoin their original queue; unstarted interrupted tasks are blocked for inspection. Each original has one transactional start intent. Separate workers can enqueue different original tasks. Actual execution continues to use existing account/device locks.
- Status keeps original task/item/attempt/execution IDs, queue time, time before native authorization and receipt waiting time. Only a verified original item with an external reference and an original completed attempt is counted. Late receipts update current status without turning a missed deadline into a pass. The terminal cutoff snapshot is retained.
- Local preflight reports blockers for all selected apps without navigating a phone, calling a social platform or granting submission permission.
- Candidate shortlist reuses up to three existing content records per account. It requires recent data, current-version review, explicit account-bound permission evidence, a contextual draft, enough remaining lifetime, and no historical attempt or active scheduled duplicate. Unknown attempts remain query-only. Empty pools are reported; no automatic generic promotional text is generated.
- Prepared evidence is looked up by immutable content, original task/item and device/client/reader binding with an index, including when more than 1,000 unrelated historical operations exist.

## Entry points

API factory: `create_app(..., phone_rounds_enabled=True, worker=True, pooled_worker=True, phone_social_config=..., phone_reddit_config=...)`. Supply existing protected service configuration through the normal deployment composition. Do not put tokens in requests or this document. CLI: `auto-promotion --db PATH serve --execution-mode pooled --phone-rounds --phone-social-config PRIVATE_SOCIAL.json --phone-reddit-config PRIVATE_REDDIT.json` loads the supplied validated bindings into the existing queue. Supply only the relevant configuration files. The CLI flag does not commission readers or enable live writes.

Existing local API authentication and cross-origin/CSRF checks apply to every route:

- `POST /api/phone-rounds` with `task_ids` (one to six original task IDs, each for a different platform) and a stable `request_key`.
- `GET /api/phone-readiness?platform=telegram` reports the current service, device capability, stop, worker and binding settings before preparing tasks.
- `GET /api/phone-rounds/{id}/preflight` checks task prerequisites and authenticated Fleet capabilities. Append `/platforms/{platform}/preflight` to the round URL to check one selected member.
- `POST /api/phone-rounds/{id}/platforms/{platform}/start` starts or rechecks only that unattempted member. Use `/stop` on the same member to fence its future grants. Other members and the original deadline are preserved.
- `POST /api/phone-rounds/{id}/start` records explicit intent and begins timing. `worker=False` records waiting without secretly starting an executor.
- `GET /api/phone-rounds/{id}` returns JSON status and timing suitable for exporting as diagnostic evidence. Native permit time is not itself a confirmed publication.
- `POST /api/phone-rounds/{id}/stop` blocks further grants. It cannot resume or reset a timed-out round.
- `GET /api/phone-round-candidates/{account_id}` returns the local reviewed shortlist. Normal campaign planning/freezing and send-time checks remain required.

## Current acceptance boundary

The timer begins at explicit start of already prepared original tasks. Upstream content discovery, review, task creation and prior dry preparation are preconditions; their earlier wall time is not represented as part of this round. A cold-start end-to-end ten-minute claim therefore requires a higher-level entry point that includes those steps. This release does not claim that acceptance.

Auto Promotion now pins `phone-fleet-management==0.2.0.dev18` and `phone-fleet-protocol==0.2.0`. New requests check authenticated fleet capabilities before creating a command. Protocol mismatch blocks a new create before recording an intent or making a POST. Existing 0.1 operation records remain read-only: query only the original execution/lookup, retain unknown, and never reinterpret old native success as new business confirmation. Old protocol identity observations are not reused as fresh identity.

A new isolated development composition runs matching 0.2 Fleet and current AP code with independent databases. Missing hardware is explicitly `waiting_for_device` while software compatibility can be ready. Previously running services are not upgraded or restarted by this composition.

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


## Software integration before a phone is connected

The repeatable command is `auto-promotion-phone-check --output REPORT.json`, or `python -m auto_promotion.phone_software_check --output REPORT.json`. It uses the actual Fleet HTTP API application, real typed protocol, durable AP mappings and node journal, and an explicitly simulated node. Six separate business tasks perform twelve sequential observe/draft-readback operations on one simulated device. It asserts zero send attempts, unchanged manifests and original unattempted item status. It does not load an existing account database or .env, call social APIs, spend model credits or publish.

To run the isolated software services from this checkout:

```powershell
.venv/Scripts/python.exe -m auto_promotion.phone_lab --directory .local/software-integration/lab --ap-port 18452 --fleet-port 18450
```

The composition deliberately starts without a device or a simulated inventory. `/api/phone-readiness` is protected by existing local API authentication and reports authenticated version compatibility separately from hardware readiness. Expected state before device onboarding: `state=software_ready`, service `state=waiting_for_device`, `hardware_validation=not_started`. Real publication verification stays false. The AP worker is disabled and `disable_live_writes=True` overrides even an inherited live-writes environment variable. Development credentials are newly generated in ignored private files; this setup never imports credentials from the old services.

The directory must be empty on first use. Restart requires the same protocol and ports and preserves its private configuration; it refuses to adopt arbitrary databases/configuration. Ordinary services have no development fallback. The new composition binds loopback only. It is a development integration environment, not a production release.

Candidate collection/review uses the existing authenticated content collection and review workflow. An empty/expired shortlist now reports `refresh_required` and the required collection, account-permission review and contextual-draft steps. It does not extend old permission or task windows, fabricate content, or mark unknown attempts reusable. Actual content source authorization and real account pairing remain operator inputs.

The six-app software report, final test logs, isolated runtime fingerprint and current readiness are exported under ignored `.local/software-integration/`. The final handoff records measured test results and the source commit.


## 2026-11-05 software integration changes

- `.gitignore`: ignore `.local/` so isolated databases, development credentials and generated reports cannot be staged accidentally.
- `pyproject.toml`: matching fleet management dev18/protocol 0.2.0 phone dependencies; software-check and isolated-lab command entry points.
- `src/auto_promotion/phone_protocol.py`: bounded capabilities validation, a version gate before new creates and correlation-preserving read-only legacy result summaries.
- `src/auto_promotion/phone_readiness.py`: configured service/authorization/version/expected-device state, separate from publication success.
- `src/auto_promotion/phone_software_check.py`: repeatable six-platform software observation/draft-readback integration using actual Fleet HTTP handlers and a simulated node.
- `src/auto_promotion/phone_lab.py`: independent local AP/Fleet applications, newly created private development configuration, isolated stores and a hard live-write disable.
- `src/auto_promotion/api.py`: authenticated phone readiness, explicit live-write disable for the development composition, and explicit registry workspace isolation.
- `src/auto_promotion/phone_reddit.py`: protocol preflight, bounded capabilities reads, legacy original-execution queries, consistent fresh-identity health and authorization errors.
- `src/auto_promotion/phone_social.py`: legacy identity/submission records remain query-only and old-protocol cached observations are not current identity proof.
- `src/auto_promotion/phone_telegram.py`: old join records retain their original read-only execution mapping.
- `src/auto_promotion/phone_fleet.py`: version gate for new observation preparation and a separate read-only legacy path that cannot create/control an old command.
- `src/auto_promotion/phone_round_readiness.py`: explicit content refresh/review/draft actions when the shortlist is empty.
- `tests/test_phone_protocol_upgrade.py`: no-hardware readiness, mismatched versions, malformed responses, fresh isolated composition, existing directory/config protection and six-platform API integration without platform networking.
- `tests/test_phone_social.py`, `tests/test_phone_reddit.py`: current-protocol fixture capabilities, no create on mismatch, old receipt queries without new POST, and expired protocol identity proof.
- `docs/PHONE_ROUNDS.md`: updated operating instructions and acceptance boundary.

Validation commands from this worktree:

```powershell
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -m pytest tests/test_phone_protocol_upgrade.py tests/test_phone_social.py tests/test_phone_reddit.py tests/test_phone_fleet.py tests/test_phone_rounds.py tests/test_phone_round_readiness.py tests/test_phone_identity_refresh.py tests/test_api.py tests/test_phone_protocol_receipt_gateway.py tests/test_telegram_receipt_budget.py tests/test_telegram_phone_observer.py tests/test_phone_telegram_join.py -q --tb=short --durations=5 --junitxml=.local/software-integration/final-regression.xml
.venv/Scripts/python.exe -m auto_promotion.phone_software_check --output .local/software-integration/six-app-check.json
```

`pip check` passed. Ruff and `git diff --check` must pass before commit. The final regression log and the six-app JSON report contain measured results. The synthetic node is never converted into a real device validation. The development server has no registered device and its expected authenticated readiness is software-ready/waiting-for-device. Before running a real customer task, use the normal device registration, account binding, permission review, new task preparation and original receipt commissioning workflow.


Final measured regression after the 0.2 upgrade: **179 passed in 123.84 seconds**. The full command above includes Telegram observed-receipt and original single-join permission coverage. `tests/test_phone_telegram_join.py` now advertises the synthetic peer's capabilities before the original one-use join test. The new software-check timeout defaults to 90 seconds; a cancellation test verifies a stalled check cannot return a pass. Targeted protocol cases also passed (14 cases). Dependency consistency (`pip check`), the explicitly checked changed Python modules/tests, and `git diff --check` passed.

Runtime proof after commit is stored separately in ignored `.local/software-integration/runtime-status.json`; it records the exact loaded source commit and readiness without exporting the local development tokens. Local ports are 18452 (new AP) and 18450 (new Fleet); the existing 18442/18440 services are not changed. The new private runtime directory is restricted to the current Windows user at launch. Hardware and real publication remain unverified.


## 2026-11-06 本地组合整合复验

在独立整合工作区，以 `655568e` 为主分支基线接收 `9a6523a`，保留 TikTok 目标证据错误与三个手机整轮错误提示。与群控 `f9c1ad2` 的协议 0.2/fleet 0.2.0.dev18 配套；未改动原 main 的未提交源代码或运行数据库。

手机功能、API、MCP、持久队列与原任务恢复组合：280 passed、4 subtests passed，416.88 秒。补充启动指纹、账号健康、启动恢复、Windows 只读验证和 MCP：67 passed、2 skipped、51 subtests passed，71.59 秒。新增指纹测试逐个验证手机源文件变更会改变指纹，缺失手机文件不会返回部分证据；旧代码的两项失败已先行复现并修正。跳过项与精确命令保存在忽略提交的 `.local/integration/`。

首次六 App 隔离软件联调在 11.578 秒完成 12 次观察/草稿回读，原项目保持 not_attempted，零发送和零真实发布。整合提交后再记录实际加载提交与指纹。它不是硬件、平台回执或十分钟实发验收；没有启动真实发送、激活服务或推送分支。
## 2026-11-06 readiness admission and honest timing

Explicit round start now records `checking` and the first 600-second clock before querying readiness. Each member must pass configuration, identity, receipt and authenticated fleet-capability checks before its original task can be admitted. Safe ready members continue while missing requirements block only their own members; shared stop/runtime/time blockers still fence the whole round. One authenticated capability request is shared across the social service's five members; the separate Reddit service retains its own authentication scope. A provider failure still returns every platform's blockers. These are service metadata reads, not platform navigation or submission.

A check costing 30 seconds or more cannot admit tasks. Original service HTTP/token timeouts remain bounded. Results and durations persist in `phone_round_preflights`; no provider exception text or credentials are logged. If no members are ready the round is `blocked`; otherwise ready members can run and the others remain explicitly `blocked`. Partial progress never satisfies the six-publication goal. Explicit recheck may proceed before the original send cutoff, but cannot reset the first start, submission cutoff or deadline. Waiting in `blocked` consumes that same clock. A check interrupted by process loss never causes automatic task starts; stop during checking wins.

Status now states `timing_scope=prepared_tasks_including_preflight`, `preparation_included=false` and `end_to_end_six_app_pass=false`. This prevents the prepared-task timer being mistaken for the full command-to-publication target. Candidate collection, account onboarding and prior preparation still require a higher-level, authoritative integration if a true cold-start claim is desired.

The six-app ten-minute criterion remains six independently confirmed original publications within the full intended interval. Observations, identity matches, typed drafts and successful metadata checks cannot satisfy it. No live or stop setting is enabled by this change. Shared real-account scheduling and unknown deduplication across the old sender and phone environment, plus allowed independent readers, remain integration prerequisites.


### Repair validation evidence

Independent worktree `D:/Projects/auto-promotion-verification-repair`, branch `codex/aurax-verification-repair`. Initial affected-feature regression: **193 passed in 125.22 seconds**, XML `.local/repair-qa/promotion.xml`. After the final per-platform admission refinement, **129 passed in 88.83 seconds**, XML `.local/repair-qa/admission-final.xml`; it covers original one-use grants, stop-before-final-grant, queued-start recovery, preflight races, deadlines and partial platform progress. These are overlapping suites, not additive publication results.

A bounded check using synthetic business tasks and the current real fleet's read-only capabilities returned all six missing-prerequisite summaries in **0.907 seconds**, with two metadata GETs, zero original task starts and zero attempts. This verifies fast rejection under the current configuration; it is not real publication acceptance. Real-device read-only connection/IME diagnostics took 0.516 seconds in the companion control branch.

No live writes, old tasks, global stops, production services or platform authorizations were changed. Current full publication remains unverified. Source timing analysis is in `docs/PHONE_VALIDATION_TIMING_2026-11-06.md`; raw sanitized timings remain outside the commit.


## Platform independence and deployment

The stdio MCP entry accepts the same protected binding-file options together with `--phone-rounds`. Point both processes at the same authoritative AP database. Chat clients use `phone_platform_status`, `create_phone_round`, `check_phone_round`, `start_phone_round`, `stop_phone_round` and `get_phone_round`. The AP worker handles execution after the client disconnects.

A round accepts any of the 63 nonempty subsets of Telegram, Reddit, Instagram, Facebook, X and TikTok. An operator may use separate single-platform rounds for independent deadlines, or a selected group with one deadline. Group and member starts validate only the previously unadmitted originals in scope. A stopped member is terminal. A worker restart or stale preflight cannot grant a second start to a previously attempted item. The existing device lock serializes native actions.

Mixed Telegram API and phone execution requires one authoritative AP database and the same registered account ID in the observer and sender configuration. An explicitly bound phone task uses the native pool and device resource. The same registered MTProto connection continues to serve other tasks. Old or unknown publications retain their original IDs for read-only verification. Use the same trusted database and account ID on the node, Fleet and AP before commissioning a task. Quiesce older sender versions during the integration release.

The authenticated `POST /api/phone-social/accounts/{account_id}/telegram-target` accepts a public group URL in `target_url`. It reads the group type, membership, description and pinned message using the existing authorized observer. Target discovery leaves 16 reads available in the account window for a receipt. Group-root phone tasks require an ordinary chat. Forum topics need their own frozen topic ID and matching receipt in a future shared contract.

`all_members_within_budget` uses the frozen original selection. `all_six_within_budget` applies only to a six-member round. The 600-second timer starts with the first start of the prepared original tasks. Measuring the user's full ten-minute goal also requires discovery, preparation and an independently confirmed publication.


## 2026-11-06 单平台手机执行整合

在本地整合提交 `a217ba9` 上接收 `8dac2cd`：手机轮次可选 1–6 个平台，并按平台独立预检、启动和停止；设备动作仍由同一设备控制权串行保护。Telegram 只读观察和发送必须绑定同一个权威数据库与原账号，未知提交保持原任务查询，不自动重发。保留现有 TikTok API、Tabbit 和目标证据校验。

本地回归：手机隔离、原始回执与身份测试 272 passed，另有 17 subtests passed；队列、API/MCP、限额及 TikTok 校验 234 passed、1 skipped、72 subtests passed。跳过项为未启用的隔离浏览器用例。该候选未经正式服务切换或真实发布验收。
