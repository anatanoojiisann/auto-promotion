# Account outbound pause UI validation — 2026-10-23

These results were collected in the isolated macOS checkout
`<workspace>`, based on main
`40d270b2b2abfbe0be029782d1731a0bc995bcda`. They do not establish Windows field
behavior, live platform login, challenge resolution, permissions or delivery.
No production task, credential, platform account or actual message was used.

## Environment and isolation

- macOS arm64; Node `24.13.0`; Python `3.14.2`.
- Playwright `1.62.1`; Google Chrome `155.0.8059.86`; Prettier `3.6.2`.
- Python path used below: `<python-env>/bin/python`.
- Existing test dependencies were reused through links in the new checkout's ignored
  `node_modules`; the old checkout was not modified.
- The UI runner creates temporary HOME, account directories and SQLite databases,
  forces live writes off, and removes temporary profiles/services on exit. The new
  account-pause suite intercepts every request with synthetic fixtures and requires
  no HTTP service. Test servers are temporary simulation services only.

## Results

Completed at 2026-10-23 06:13 UTC; full UI runner exited `0`.

| Check | Result | Evidence |
| --- | --- | --- |
| Main UI | Passed, 45 named checks | `var/ui-test-result.json` |
| Account pools UI | Passed, 2 simulated accounts, 3 contents, 2 batches, 3 confirmed simulations; desktop/mobile settings | Full UI log |
| High-frequency interactions UI | Passed, 22 named checks | `var/hf-ui-test-result.json` |
| Delayed-response UI faults | Passed, 64 cases, zero failures | Full UI log |
| New account-pause recovery UI | Passed, 11 cases | Full UI log |
| New recovery API contract | Passed, 3 tests | `api-blocks.log` |
| Existing API tests | Passed, 10 tests | `api-existing.log` |
| Full configured Prettier range | Passed | `format.log` |
| Recovery API Ruff | Passed | `ui-api-ruff.log` |
| `git diff --check` | Passed | `diff-check.log` |
| Python package build | Passed, sdist + wheel, no build isolation/network | `build.log` |

The account-pause UI cases cover explicit check then restore, cancellation before
checking, duplicate checks, cancelled delayed check success/failure, identity
failure, expired health checks, duplicate restore, an unhealthy check, rejection
of an old generation after a new block, and cancelled delayed restore
success/failure. Failed restore requires a new health check. Cancelling a window
does not claim to cancel an already submitted server request.

The API contract rejects missing, empty, string, boolean, zero and extra-field
restore generations. Health-check and restore requests remain authenticated and
server recovery conflicts remain HTTP 409. Backend identity, generation,
expiry and queue-preservation semantics are tested by the separate implementation
suite, not proven by UI mocks.

The initial expected red phase observed all three new API cases fail with missing
routes (404) and the UI test fail waiting for the missing recovery button. After
implementation these passed. Setup attempts also observed sandbox browser launch
failure and the isolated runner not inheriting `NODE_PATH`; the browser was rerun
with authorized test-only escalation and local dependency links corrected module
resolution. These setup failures were not counted as passing tests.

## Reproduction

```sh
AUTO_PROMOTION_PYTHON=/absolute/path/to/python \
AP_UI_CHROMIUM_PATH=/absolute/path/to/test-chrome \
node scripts/test_ui_offline.cjs main pools interactions faults blocks
npm run format:check
PYTHONPATH=src /absolute/path/to/python -m unittest discover -s tests -p test_account_block_api.py
PYTHONPATH=src /absolute/path/to/python -m unittest discover -s tests -p test_api.py
/absolute/path/to/python -m build --no-isolation --outdir /tmp/ap-outbound-validation-20261023/dist
```

Full logs are in `/tmp/ap-outbound-validation-20261023/`. The build initially
completed at 06:10 UTC, so later backend edits require a final rebuild; the main
validation report should record that final snapshot. Wheel inspection confirmed
`auto_promotion/account_blocks.py` and the web assets were included. Build outputs
are `/tmp/ap-outbound-validation-20261023/dist/auto_promotion-0.1.0.tar.gz` and
`auto_promotion-0.1.0-py3-none-any.whl`.

Python 3.11, Windows execution, live social-platform tests, production deployment
and real captcha flows were not run by this UI validation task. Cloudflare tests
and final independent security review are recorded separately.
