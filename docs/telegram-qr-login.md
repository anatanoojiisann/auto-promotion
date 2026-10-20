# Cloud Telegram QR login

Login setup confirms an identity and a recoverable encrypted session. It does
not start a reader, resume a workflow or authorize a native write. Reader start
remains a separate explicit operation with the existing workflow guards.

## Caller contract

Call `login-begin` explicitly with the assigned `plan_id` and a setup `source`.
Keep the returned `flow_id` and `generation`; include both in `login-poll`.
Ignore responses belonging to an older pair. A repeated begin returns the active
flow; replacement requires its exact `replace_flow_id`. Poll never silently
creates a QR, restarts a lost connection or clears a terminal failure. Begin
again explicitly after a terminal result. A page must remove its old QR when
the flow changes, expires or leaves `waiting_device_confirmation`.

This changes two previous behaviors: polling an expired login no longer starts
another login, and an authorized poll no longer starts the Telegram reader.
The investigation attachment's loopback QR page is not included or upgraded by
this change. Existing callers need to implement the contract above.

`authorized` requires the current client's identity check, encrypted session
storage, reading that stored ciphertext, and identity verification through a
different client restored from it. `last_verified_at` describes that completed
verification; it does not guarantee a permanently open TCP connection. Legacy
authorized state without this proof reports `verification_required`. A lost
in-memory owner for a pending durable flow reports `login_interrupted`.
That interruption is a derived view (`state_source=derived_lost_connection`);
status and polling do not update storage or delete pending session material.

## Deadlines and diagnostics

Initial connection/export has a 10-second budget. Token refresh can update the
QR expiry, but the entire flow is capped at 120 seconds. The first observed
cloud Update/Migrate/Success starts a bounded 20-second confirmation window,
including identity, encryption, persistence and restored-client verification.
Terminal persistence and socket cleanup each have bounded waits. A timeout
does not cancel an already running promise: flow/generation and deadline checks
inside the storage transaction reject late promotion and roll it back.

`login_diagnostics` contains flow ownership, safe phase history, timestamps, DC,
check count, `cloud_confirmation_observed`, stable reason/exception type and
`persistence_verified`. It contains no token, QR URI, session, expected identity
or raw exception message. The QR URI is returned only to the current waiting
flow and must not be logged or put in a diagnostic bundle.

`login_expired` / `TelegramQrExpiredWithoutConfirmation` means that cloud
confirmation was not observed within the window. It does **not** establish
whether the person scanned or allowed the login on their phone. An in-flight
token RPC has `TelegramQrTokenCheckTimedOut`; confirmation/finalization timeout
has `TelegramQrConfirmationTimedOut`. Identity, encryption, storage and restored
authentication errors retain their phase instead of being relabeled QR expiry.
No report should infer a phone action from absence of a cloud event.

## Correlating an existing failure safely

No new login is required to inspect an existing flow. The existing authorized
operator can read status/poll or inspect already captured structured logs. These
operations remain read-only. **Do not export a whole begin/poll/status response**:
an active response can contain `qr_uri`, which is a login credential. Do not
dump an exception, stack, request body, headers or environment.

Export only `diagnostic_trace` and these scalar fields when present:
`request_id`, `flow_id`, `generation`, `qr_generation`, `status`,
`last_verified_at`. A failed Python request exposes only its `.diagnostics`
metadata (also available as `CloudControl.last_request_diagnostics`), not the
exception object or its attributes wholesale. That metadata contains a generated
request UUID, fixed operation/outcome, elapsed/timeout milliseconds, HTTP status,
safe transport/parse cause, and validated flow/generation identifiers. It never
includes the URL, response body or credential values.

Each HTTP request has its own UUIDv4 `request_id`. The Python client sends it in
`X-AP-Request-ID`; the control plane validates and forwards it to the executor.
The initial begin request ID stays on the flow's stage events. Later poll IDs
identify their own requests and associate with that same `flow_id`. Invalid IDs
are replaced, never echoed as arbitrary log text. `generation` fences a flow;
`qr_generation` counts token changes without recording the token or a hash of it.

Executor console records are JSON with `schema: 1`, a fixed event/phase, safe
identifiers and numeric timing/DC fields. Filter by the exact flow UUID and
sort by sequence/time. Look for `stage_started` followed by `stage_completed`
or `stage_failed`, the `timeout_fired.source`, `first_error`, and `flow_terminal`.
This covers Update receipt, Export/Import/DC switch, getMe, encryption, pending
storage, decryption, a different restored client's identity, and atomic session
promotion. A timeout records which phase and timer expired, without attributing
an unobserved action to the phone. Terminal evidence travels in the existing
atomic state write, so controller reconstruction retains it. Final storage
commit completion is proved by that committed terminal state; its complete
wall-clock duration is available in the console completion event, not invented
in a pre-commit snapshot.

Retention is bounded: 64 history records plus the independently retained first
error, each at most 768 JSON bytes (under 50 KiB per trace); 96 console entries
per flow, with budget reserved for errors/terminal/cleanup. Only homogeneous
poll/ignored/duplicate events aggregate, within the same generations. Important
stage evidence has eviction priority over routine token checks. Terminal history
is frozen under later polling. Python keeps one latest safe diagnostic object
and emits at most 96 records per client instance (64 ordinary plus 32 failure
records). Python successes use the existing logger's INFO level, failures use
WARNING; this change does not configure logging or create a log file. Provider
console retention remains subject to the existing operator logging policy.

Logs are best effort and introduce no separate required storage transaction.
A throwing/rejecting sink cannot change login success or mask its original
error. Failure of the necessary session/state write still fails closed; its
trace may survive only in memory/console if storage itself is unavailable.
Share that selected metadata with UTC timestamps and the same-flow phone
observation, with QR/token, phone number, account identity and session removed.

The supplied field report ends with a pending token and no saved-authorization
milestone. It cannot associate that flow with the earlier phone acceptance or
prove which event, RPC or persistence stage failed. The boundary regressions
below demonstrate reproducible risks in the supplied implementation; they are
not acceptance of a real Telegram login. A separately authorized deployment and
same-flow field capture are still required.

## Offline verification

Dependencies must already be installed. From the repository root, this runs the
two existing complete Node suites using fake clients/local Miniflare only and a
temporary home with no inherited account or Cloudflare credential environment:

```python
# Run with: .venv/bin/python -I -S <your-saved-copy-of-this-script.py>
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path.cwd()
node = Path(shutil.which("node"))
with tempfile.TemporaryDirectory(prefix="ap-qr-offline-") as home:
    env = {
        "PATH": str(node.parent) + os.pathsep + os.defpath,
        "HOME": home, "USERPROFILE": home,
        "APPDATA": str(Path(home) / "AppData/Roaming"),
        "LOCALAPPDATA": str(Path(home) / "AppData/Local"),
        "AP_DB": str(Path(home) / "unused.db"),
        "AP_ACCOUNTS_DIR": str(Path(home) / "accounts"),
        "AUTO_PROMOTION_LIVE_WRITES": "0",
        "WRANGLER_SEND_METRICS": "false", "CI": "true",
    }
    if os.name == "nt":
        for key in ("SystemRoot", "WINDIR"):
            if key in os.environ:
                env[key] = os.environ[key]
    for component in ("telegram-executor", "control-plane"):
        cwd = root / "cloudflare" / component
        files = sorted(str(p.relative_to(cwd)) for p in (cwd / "test").glob("*.test.mjs"))
        subprocess.run([str(node), "--test", *files], cwd=cwd, env=env,
                       check=True, timeout=180)
```

For the focused boundary suite select only `test/qr-login-flow.test.mjs` and
`test/qr-login-controller.test.mjs` under `telegram-executor`. Tests use fake
timers, clients and transaction storage; they do not call Telegram. Control
integration tests need loopback listening permission for local Miniflare.
`npm run check` in each component is a bundling dry run, not deployment.
