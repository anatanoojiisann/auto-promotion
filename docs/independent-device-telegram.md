# Independent Windows device: scoped Telegram text

This additive protocol allows a new computer to create and control its own cloud
plan without the legacy computer database. It does not migrate existing tasks,
remove legacy HMAC/store fencing, change FALLBACK_NODE_ID, or grant local sending.
The ordinary task editor retains its existing reply/review behavior. The new
standalone UI and CLI use `/v2/device/*` exclusively.

## Architecture and limits

* Each enrollment has a new random node ID, store ID and secret. The server stores
  only its hash; the Windows client stores the secret and draft using current-user
  DPAPI. Copying an old node identity or an empty database is unnecessary.
* Operator enrollment binds one existing cloud account, one group and one plain
  text message, expiring in 24 hours by CLI default (server maximum 30 days).
  This first release deliberately provides a **one-shot grant**, not unrestricted
  recurring promotion. Cancellation consumes the grant; it never restores quota.
* Preparing freezes account, numeric peer, exact text, expiry and request ID.
  Public usernames resolve through the authorized executor before freezing.
  Private IDs resolve from that session's dialog entities; access hashes are not
  guessed or accepted from clients. No automatic joining is performed.
* Explicit Start records the start source and uses the existing native queue,
  execution gate, global account identity lock, lease, quota and cooldown checks.
  A preexisting local channel is rejected with
  `DeviceRequiresExplicitChannelHandoff`. This API has no takeover operation.
  Existing owner reconciliation and an explicit handoff are required separately.
* The scheduler cannot start or resume these plans. After a safe pre-submission
  cooldown, the user explicitly starts the same prepared plan. Attempted or
  unknown jobs cannot be resent. Cancellation/revocation does not retract messages
  already submitted and preserves uncertain attempt evidence.
* Devices cannot inspect/control each other's plans. Legacy sync/node endpoints
  cannot overwrite device registrations or tasks. Revocation disables the grant
  and cancels unattempted work without resetting unrelated jobs or credentials.

## Future authorized setup (not executed by implementation/testing)

After code review and a separately approved deployment, obtain the **existing
cloud account ID** from the deployment owner; this is not a phone number or
Telegram Web session. Confirm the executor is authorized as that account and the
account is cloud-owned and explicitly allows `send_text`; enrollment does not expand
account actions or clear any global stop. Login/setup remains a separate privileged existing flow;
this release does not move browser sessions or bypass legacy login ownership.
If there is no authorized executor session, its official login must be arranged
before using this client. Do not reset its existing secrets.

Run from this worktree with its `.venv` Python, using a private D-drive config path:

```powershell
.venv\Scripts\python.exe -m auto_promotion.device_control init --config D:\code\device-private\device.dpapi --account EXISTING_CLOUD_ACCOUNT_ID --target -1002123962275
.venv\Scripts\python.exe -m auto_promotion.device_control enroll --config D:\code\device-private\device.dpapi --operator-token-file EXISTING_OPERATOR_FILE
.venv\Scripts\python.exe -m auto_promotion.device_ui --config D:\code\device-private\device.dpapi --port 8755
```

Open `http://127.0.0.1:8755`. Prepare is draft-only. Start sends the frozen text
once. Status and Cancel address the persisted plan; no manual plan ID is needed.
CLI equivalents are `prepare --text TEXT`, `start`, `status`, `cancel`, all with
`--config`. `revoke` requires the existing operator file; it neither rotates nor
persists that operator credential. Stop the UI with Ctrl+C. Retain the encrypted
config for uncertain-result reconciliation; do not recreate a grant to retry.
Delete a config only after revocation and resolution of all uncertain receipts.
Do not commit any credential/config, auth ZIP or production database.

## Deployment and evidence boundaries

Base: cc8b7cae590e84d68d674a5062f0314297256205. The four control modules also
preserve the previously inspected live cooldown/deferred activation implementation
from control version c2bb71c2-ca3c-4aa5-ad70-3ea4ef5388e7. Current main kernel X
changes remain. These are local changes, not a deployed version. Recheck current
remote source/config before any later deployment; do not use bootstrap scripts
that regenerate OPERATOR_TOKEN. Tables are additive; rollback must first revoke
new grants and settle pending/unknown work, not discard their audit tables.

Offline tests use synthetic tokens, SQLite, Miniflare, actual Windows DPAPI and
isolated Chrome. A fake executor confirms routing/gates only; it does not prove
Telegram membership, platform write permissions, session login or real delivery.
The requested AIGramLab message has not been sent by these tests.
