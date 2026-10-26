# Telegram local read settings — UI/API validation

This addition configures local read budgets independently for each Telegram
account. It does not change frozen task manifests, perform a health probe,
start tasks or replace required identity/permission checks. The UI states that
budgets are not platform permission and do not guarantee freedom from limits.
It also distinguishes recorded read calls, message counts and underlying network
request totals. The form explicitly limits this setting to the built-in local executor and
states that the legacy external MCP channel is not covered;
cloud executors have independent configuration and accounting, with no aggregate
cap across devices. A targeted UI rerun verified this scope text after the full
suite; no Cloudflare operation was performed.

## Contract

- `GET /api/accounts/{id}/telegram-reads` returns `{account_id, policy, revision}`.
- `PUT` accepts only the six `TelegramReadPolicy` integer fields. The UI sends
  the loaded revision as `If-Match`; stale saves remain HTTP 409. The server
  accepts an omitted header only for an initial revision-0 configuration.
- Response fields are explicitly selected; session/access-hash fields are not
  returned. Authentication remains mandatory. Unknown accounts and non-Telegram
  accounts retain core rejection behavior. No health probe runs on read/save.
- Defaults: 120 requests per 3600-second window; 40 scan reads; 100 messages per
  page; 10 groups per scan; 300-second content-cache TTL. The form validates the
  same ranges as the model, including 100-message/100-group maxima and an allowed
  zero cache TTL.

The account list exposes this setting only for Telegram. Saving is guarded
against repeated clicks. Closing or navigating away prevents delayed responses
from replacing another form; it does not claim to cancel a submitted save.
A concurrent revision conflict requires reopening the current settings.

## Test evidence

The initial expected red phase produced five failing API tests (including
validation subtests) because the routes did not exist, and an offline UI failure
because the Telegram settings control did not exist. After implementation:

| Check | Result |
| --- | --- |
| New API tests | 10 passed |
| New offline UI scenarios | 11 passed |
| Target API/test Ruff | Passed |
| Full configured UI Prettier range | Passed |
| Full offline UI regression | Passed all six suites, exit 0; completed 09:27 UTC |

API tests include a real temporary Store round trip from revision 0 to 1,
stale/missing revision conflicts after the first save, non-Telegram rejection,
unauthenticated requests, strict budget and revision validation, and response
field filtering. A boundary round trip saves every legal maximum through the actual API/Store, reloads it through Engine and the reader context, validates it with the offline reader helper, and rejects each maximum plus one. The 101-page/group acceptance was first reproduced as a failing test before model alignment. All account data are synthetic. UI scenarios cover defaults,
mobile layout and Telegram-only controls, invalid input, repeated save, server
rejection, stale revision, cancellation, and delayed GET/PUT success/failure
preserving a newer draft.

Full UI results: main 45 named checks; pools complete synthetic planning/start/receipt chain (2 accounts, 3 contents, 2 batches, 3 confirmed simulations); interactions 22 named checks; delayed-response faults 64 cases; account recovery 11 cases; Telegram read settings 11 cases. No failures were reported. After review aligned page/group maxima to 100 and clarified read-call/network-count wording, the targeted 10 API and 11 new UI cases were rerun and passed; the full surrounding suites were not repeated unnecessarily.

Logs: `/tmp/ap-telegram-read-validation-20261023/` (`api-red.log`, `ui-red.log`,
`api.log`, `ui-new.log`, `ui-full.log`, `format.log`, `ruff-api.log`).

## Reproduction and boundaries

```sh
PYTHONPATH=src /absolute/path/to/python -m unittest discover -s tests -p test_telegram_read_api.py
AP_UI_CHROMIUM_PATH=/absolute/path/to/test-chrome npm run test:ui:telegram-reads
AUTO_PROMOTION_PYTHON=/absolute/path/to/python \
AP_UI_CHROMIUM_PATH=/absolute/path/to/test-chrome \
node scripts/test_ui_offline.cjs main pools interactions faults blocks telegram_reads
npm run format:check
```

Validation runs on macOS arm64, Node 24.13.0, Python 3.14.2, Playwright 1.62.1,
Chrome 155.0.8059.86 and Prettier 3.6.2. UI services use temporary simulation
databases/account paths with live writes disabled. The new UI suite intercepts
all requests with local fixtures. No actual Telegram session or platform
request, credential, production task, GitHub upload, Cloudflare or Windows
validation is part of this task. Backend read accounting/cache/cooldown tests
and independent review are recorded separately.
