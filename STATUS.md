# STATUS - harvey (Gershon cold-email agent)
- 2026-11-23: weekly report (scripts/gershon_weekly.py + deploy/harvey-weekly.{service,timer}, Mon 12:00 UTC) replaces harvey-daily + harvey-stats timers. Olivier: daily email made no sense.
Updated: 2026-11-16

## State
- Harvey is PAUSED on purpose and runs on a VPS (/opt/harvey). Nothing here is deployed by merge.
- main includes PR #7 (no double sign-off on old drafts) and PR #8 (alert "N waiting" at most once per UTC day,
  red cross only for real failures).
- Full test suite on merged main: 3021 passed, 26 skipped (local run, 2026-11-14).
- Known baseline: the two windows-latest CI jobs fail on main too (test_unusable_html_signature_holds_sending:
  "environment variable is longer than 32767 characters"). Not caused by #7/#8.
- 2026-11-15: branch nightly/2026-11-15-harvey-botcheck-guard (v0.3.1) - bot-check / error page titles
  ("Checking your browser", "404 Not Found") are never a company name. Scout wiring still to push.
- 2026-11-16: branch nightly/2026-11-16-harvey-daily-v2 (v0.3.2) - daily report v2 (one readable email,
  scripts/gershon_daily.py), rebased on main; replaces PR #10. Full suite 3072 passed, 26 skipped.

## Deploy route
- VPS only: git pull in /opt/harvey, restart the Harvey service and the report timers. Do not edit its .env here.

## Done
- [x] #7, #8 merged.
- [x] #9 merged 2026-11-15: review in Gmail Drafts (two-way sync timer, not deployed).
- [x] #4 merged 2026-11-15: Streak "GC Campaigns" sync (Origin H01) + reply alert counts real prospect replies only.
  Runs only once STREAK_API_KEY is in /opt/harvey/.env (not set; Olivier's OK). Try `gershon_streak.py dry-run` first.

## Open
- [ ] VPS pull + restart (not done; needs access to the VPS).
- [ ] Daily report v2 PR: merge, then VPS pull; check the "harvey-loop" unit exists; close #10.
- [ ] Discard the drafts already written for "Checking your browser" on the VPS (Ariel).
- [ ] Codex (ChatGPT) re-login on the VPS: brain is not ready until then.
- [ ] Check REPORT_TO / DRAFTS_TO / LEAD_TO in the VPS .env (typo address "harvery@" is not in the repo).
- [ ] PR #3 (private review web page) left open: overlaps #9 (Gmail Drafts review); Olivier to choose.
- [ ] First alerts run after the VPS pull may report existing prospect replies once (state key renamed replies_total -> prospect_replies).
- [ ] Windows CI failure on main (test uses an env var over the Windows limit).
