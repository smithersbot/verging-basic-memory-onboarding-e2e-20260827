# Wiring check — Basic Memory

Everything on this page proves your integration reaches us and reports come back.
This is a wiring check, not a regression report: no test suite ran, nothing is scored, and it is free.

| **Wiring check** | Basic Memory |
|---|---|
| **Date** | 2026-08-28 |
| **Stage** | Wiring check |

## What this page proves

- **Your API key authenticated.** The request that produced this page presented your key, and it matched your account.
- **Reports come back.** This page reached you through the same route every regression report uses (GET /v1/releases/run_20260828_52f3dafa1354/report), and your integration commits it the same way.

## What this page does not prove

No test suite ran and no question was asked. Nothing on this page says anything about your product's memory. Your first regression report comes from your first release.

## Next steps

1. **This page was committed by your workflow's first job: the whole loop works.** Key, release, report, and the commit back into your repository's "Verging Memory CI" folder are all proven. Nothing else is needed from you. (If you use the raw API instead of the GitHub Action, commit this page the same way your integration commits a report.)
2. **Continue onboarding to connect your product.** The next step collects and verifies your non-production HTTPS endpoint and scoped credential. An endpoint is intentionally not required for this wiring check.
3. **Releases go ahead once your suites are set up.** Verging Labs sets up (activates) each test suite on your agent setups and tells you when they are ready. Until then, every push performs this wiring check instead of a release and the job passes.
4. **Repeat this wiring check whenever you change your integration.** It is always free and normally returns within seconds because no tests or model sessions run: with the GitHub Action, set the input wiring_check to true for one job.

---
This page is a wiring check. It is free and never billed, and no test result appears on it. Terms used in regression reports are defined in the reading guide: https://verginglabs.com/memory-ci/reading-guide

