# Harvey (Gershon) — STATUS

Last updated: 2026-11-11

Harvey = autonomous cold-email agent on the Hostinger VPS srv1511567.hstgr.cloud (/opt/harvey).
Every email waits for Olivier's approval; nothing has been sent to prospects yet.
Full history and commands: HANDOFF.txt.

## Done ✅
- Installed on the VPS as systemd service `harvey`; brain = ChatGPT/Codex (renew login ~8 days).
- Reports/alerts by email (scripts/gershon_reports.py, Resend): daily 08:00 + 15-min alerts.
- Email verification keys (MillionVerifier, ZeroBounce) on the VPS.
- PRs #1, #2 merged.

## Open ❌
- Not yet deployed to the VPS: new-lead email, full drafts by email, CC01 email rules,
  and the Streak GC Campaigns sync below. Deploy route: hPanel > VPS > Web console.
- Streak sync (branch gershon/streak-h01-sync): Harvey's leads -> Streak "GC Campaigns",
  Origin H01, deduped. Needs STREAK_API_KEY in /opt/harvey/.env + deploy (Olivier's OK).
- Reply alert fixed in the same branch: it counted inbox newsletters as "prospect replies".
- No sending mailbox yet (separate domain + warmup), Olivier to decide.
- traefik container crash-loops; linalysis container restart-loops (CPU warning).

## Known issues
- 31 drafts in pending_review were written to the old rules: reject and let Harvey rewrite.
- Leads in Streak by hand (2026-11-11): GoodCore, The Munro Agency (fit); iCreativez,
  RevenueBase, Techreviewer (poor fit / unverified).
