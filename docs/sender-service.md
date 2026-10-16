# Windows sender hosting

The sender at port 8780 must be hosted outside an interactive coding terminal.
Start-Process alone did not provide process supervision or persistence when its
launching session ended. Two observed outages coincided with Windows Application
events recording the Codex sandbox service stopping (2026-10-15 23:30 and
2026-10-16 13:28, UTC+8). The original sender had no OS exit-code observer, so
these events establish correlation rather than a proven identity of the killer.

Install or update the current user's host from the repository:

```powershell
.\scripts\install-sender-service.ps1
```

The Windows task `AutoPromotion-Sender-8780` uses the existing signed-in user's
interactive session with limited privileges, allowing the existing desktop
browser integration. No password is stored. It starts on user logon, retries
host failures after one minute, and has a five-minute fallback trigger with
IgnoreNew. It has no execution time limit and does not stop when switching to
battery. A second named-mutex guard prevents duplicate hosts for the same
database and port. The fallback can report an ignored launch while the original
task remains Running; check the host status file and worker heartbeat as well.

The host adopts a healthy existing sender, observes its actual exit code using
a retained Windows process handle, and starts the existing CLI if the port and
process have disappeared. An occupied or hung listener is reported for attention
and is never forcibly replaced across a possibly uncertain platform operation.
The host does not activate drafts, change stop switches, set live-write flags,
change quotas, reset item outcomes, or retry attempts. All publication checks
remain in the existing sender and backends.

Status is written to `var/sender-service-8780.json`. Timestamped lifecycle events
are in `var/sender-service.jsonl` and the normal audit table; child output is in
`var/sender-service-logs/`. Future abrupt exits have observed exit codes when the
handle was available. The original historical outages retain unknown exit codes.

The host requests temporary prevention of idle sleep when an active item's
delivery is within 15 minutes or an item is executing. It does not change the
machine's power plan or override lid closure, forced sleep, shutdown, or logout.
Local browser sends require the computer and signed-in session to be available.

On 2026-10-16, a controlled recovery test first checked that no items or joins
were executing and no live items were due within two minutes. Terminating the
idle legacy sender was observed with exit code 4294967295; the host relaunched
the sender within approximately 15 seconds. No item states or attempts were
reset as part of that test.
