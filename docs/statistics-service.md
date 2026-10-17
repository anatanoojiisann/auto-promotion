# Local statistics service

Port 8782 is a separate statistics service, using the same project database as
the existing sender on 8780. Install its Windows Task Scheduler host with:

```powershell
.\scripts\install-reporting-service.ps1
```

The `AutoPromotion-Reports-8782` task uses the current signed-in user without a
password. It starts at logon, restarts after a failure and checks every five
minutes using IgnoreNew plus a named mutex. It never replaces an occupied or
unhealthy listener. Logs and status are in `var/reporting-service*`.

The child runs `serve --port 8782 --reports-only`. This mode forces its own
live-write flag off and starts only the existing once-per-boundary Telegram
statistics collector and local report builder. It does not start sender pools,
TikTok reconciliation, inbox collection or feeds. All mutation endpoints except
the local session bootstrap are rejected. Existing database stops, cooldowns,
quotas, tasks, manifests and the 8780 sender configuration remain unchanged.

Health probes must distinguish `PermissionError` / WinError 10013 (the observer
cannot access localhost) from `ConnectionRefusedError` / WinError 10061 (no
listener). Verify a denied probe from an allowed host context before reporting
an outage; never restart the sender based on observer access denial.

Telegram feedback diagnostics are available through authenticated
`GET /api/statistics/feedback-issues`. Only safe error codes are stored, never
message text, exception messages or credentials. A parent that is no longer
readable can still have its direct replies observed using its exact, previously
verified receipt and platform timestamp. Such coverage remains partial, so a
missing parent never becomes a complete zero-reply result. A mismatched readable
parent or an unverified receipt is rejected. Group access restrictions stay
explicitly unavailable; the collector never joins, sends or resets cooldowns to
work around them. Cursors and per-boundary collection slots are preserved.
