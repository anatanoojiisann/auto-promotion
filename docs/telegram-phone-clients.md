# Android Telegram phone clients

This route keeps the existing AP persistent queue, qualified candidates, join attempts,
single-use write grants, global/account stops, pacing and unknown-outcome policy.
It does not replace the existing Telegram API sender or start any task by itself.

Phone bindings accept an optional `client_id` and `receipt_reader: telegram_api`.
The client ID must match B's AccountBinding and A's RuntimeAccount. Package/version/UI
rules live in A's protected profile registry. Unsupported rules fail closed; a new client
never falls back to another installed client or inherits its identity evidence.

Native identity evidence is renewed at an explicit start and before due work is claimed.
Routine account health checks remain read-only. Unsettled native requests are looked up
by their original execution IDs instead of replayed.

`POST /api/phone-social/accounts/{id}/membership` accepts target_url and target_title for
read-only native public-group membership inspection. The join path uses `join_enabled`
(false by default), a normal explicitly started discovery task and its original leased
candidate. A final join grant is consumed once. Admin review stays pending and never
counts as joined; polling the original receipt cannot itself detect later approval.
Use a new read-only membership inspection to inspect later changes, never repeat join.

For optional independent native-send receipts, install the existing `telegram` extra and
authorize a separate API profile for the SAME account through the existing account
connection flow. Do not copy sessions from Android. Configure `telegram_observers` by
phone account ID with `backend_label`, pinned positive `user_id` and optional `accounts_dir`,
`api_account_id`, `api_database` references. The reader compares native identity too.
Only public megagroups and plain text messages are supported by this first observer.

The observer takes a pre-send peer/message baseline, then searches only later own
messages matching the exact text and time. Missing or ambiguous results stay unknown.
Its Telethon RPC allowlist rejects mutations and uses AP read budgets. The private
`/internal/phone-social/v1/telegram-receipt` route checks the spent grant, request hash,
execution/device/native-command correlation and returns an HMAC signed receipt.
Repeated receipt requests may reuse the immutable cached evidence even after stop;
they cannot issue new writes. AP reconciliation never rewrites B's unknown result.

No global live switch or stop is changed by installing this code. The real phone has
only the official Telegram client installed; third-party clients and the independent
API observer require commissioning. Synthetic test success is not hardware acceptance.
