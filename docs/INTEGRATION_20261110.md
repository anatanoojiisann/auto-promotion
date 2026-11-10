# 2026-11-10 integration

This integrates the completed content/receipt handoff (`a41c03c`) and phone-routing/original-receipt repairs (`5794552`) on the current GitHub main. Package version: `0.1.1.dev6`; paired protocol `0.2.2`, fleet `0.2.0.dev20`.

The content producer remains separate from execution. Existing task, item, candidate-version and receipt bindings survive integration. Native X and Instagram IDs are retained for both legacy and v2 receipt consumers. Original unknown phone submissions are verified through the existing task's read-only receipt path; no second attempt or submission authorization is created.

The new readback module participates in startup fingerprints. Native bridge logging suppression is restored on both normal exit and invalid input, keeping later cloud diagnostics available while retaining silent bridge execution.

CI retains all existing unittest cases on Python 3.11 and 3.14. Pure pytest phone modules are tested on the required Python 3.12 runtime. Discovery was compared before/after the runner change: all 1561 existing unittest cases were retained; two bridge-log restoration cases were added. Phone SDK imports can be unavailable during legacy discovery.

The private phone repository runs paired acceptance against an exact public AP commit, using its own exact checkout. This avoids copying a privileged credential into the public repository. The public duplicate paired job is explicitly skipped when PHONE_PLATFORM_READ_TOKEN is absent; it is not reported as device acceptance. The final phone-pair manifest is pinned in the follow-up integration commit.

Local integration checks include 149 content/receipt scenarios and 25 bridge/diagnostic/runtime checks (31 subtests), plus the existing package/contract checks. Full-suite results and remote checks are recorded in the final integration evidence. No phone, external submission, model spending, live-service replacement or production migration is part of this integration.

Reproduce paired acceptance with Python 3.12:

```powershell
python scripts/verify_phone_integration.py --phone-source PATH_TO_EXACT_PHONE_CHECKOUT --full-legacy
```

The private CI can provide --phone-commit with the exact 40-character SHA of its current checkout; package versions remain checked against docs/integrations/phone-pair.json. It cannot select an execution URL or grant external writes.
