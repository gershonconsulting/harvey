# STATUS - harvey (Gershon cold-email agent)
Updated: 2026-11-14 (night push)

## State
- Harvey is PAUSED on purpose and runs on a VPS (/opt/harvey). Nothing here is deployed by merge.
- main includes PR #7 (no double sign-off on old drafts) and PR #8 (alert "N waiting" at most once per UTC day,
  red cross only for real failures).
- Full test suite on merged main: 3021 passed, 26 skipped (local run, 2026-11-14).
- Known baseline: the two windows-latest CI jobs fail on main too (test_unusable_html_signature_holds_sending:
  "environment variable is longer than 32767 characters"). Not caused by #7/#8.

## Deploy route
- VPS only: git pull in /opt/harvey, restart the Harvey service and the report timers. Do not edit its .env here.

## Done
- [x] #7, #8 merged.

## Open
- [ ] VPS pull + restart (not done; needs access to the VPS).
- [ ] Codex (ChatGPT) re-login on the VPS: brain is not ready until then.
- [ ] Check REPORT_TO / DRAFTS_TO / LEAD_TO in the VPS .env (typo address "harvery@" is not in the repo).
- [ ] PRs #3, #4, #9 left open on purpose (they change what Harvey sends/reads).
- [ ] Windows CI failure on main (test uses an env var over the Windows limit).
