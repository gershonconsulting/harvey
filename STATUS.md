# Harvey (Gershon) — status

Last updated: 2026-11-10

## Where it runs
VPS srv1511567.hstgr.cloud, /opt/harvey, systemd `harvey` (dashboard 127.0.0.1:5555, private).
Deploy route: hPanel > VPS > Web console (root shell). Details in HANDOFF.txt.

## Done ✅
- Harvey installed, Codex brain (gpt-6.1-sol), Resend reports/alerts every 15 min.
- PR #1 full drafts by email, PR #2 Olivier's CC01 email rules + signature (merged 11-10).
- Review page (branch gershon/review-page): `harvey review-page` on 127.0.0.1:5556 lists
  every email waiting for approval; edit, approve (one at a time, never sends) or reject.
  Private link `https://harvey.gershon.ai/review#k=<key>` (key in data/review-token).
  Alert, drafts and daily emails carry the link when HARVEY_REVIEW_URL is set.

## Open ❌
- Deploy main + review page to /opt/harvey (needs Olivier's OK; tied to sending mailbox
  choice .net / .org / hold). Steps: git pull, pip install -e ., copy
  scripts/deploy/harvey-review.service to /etc/systemd/system, enable it,
  add HARVEY_REVIEW_URL=https://harvey.gershon.ai to .env, DNS A harvey.gershon.ai ->
  187.124.235.148 (Cloudflare, DNS only), append scripts/deploy/Caddyfile.harvey-review
  to the host Caddyfile, reload Caddy, run `scripts/gershon_reports.py drafts`.
- 31 drafts pending were written before the CC01 rules: reject them and let Harvey rewrite.
- Sending mailbox not connected (Gmail OAuth for the chosen address).
- traefik crash loop, linalysis container restart loop (see HANDOFF.txt).

## Known issues
- Codex login needs renewal about every 8 days (alert says "brain NOT READY").
