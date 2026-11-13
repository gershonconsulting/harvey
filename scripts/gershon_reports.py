#!/usr/bin/env python3
"""Gershon add-on: email Harvey reports and alerts through Resend.

  gershon_reports.py daily   -> full daily report (always sends)
  gershon_reports.py alerts  -> sends only when something changed that Olivier
                                needs to know (service down, brain not ready,
                                emails waiting for approval, new replies,
                                send errors). State kept in data/gershon_alerts.json.
                                The "N waiting for approval" alert goes out at most once
                                per UTC day, unless N grows past the last N we told you.
                                Subject prefix: failures (service down, brain not ready,
                                send errors) get a red cross; info mails get an info sign.
                                Also sends ONE email per new lead (a prospect whose
                                reply Harvey classified as interested), subject
                                "GC Campaign H01 — New lead: <name>, <company>".
                                Also sends ONE email listing every draft that newly
                                entered pending_review, in full (to, name, company,
                                subject, body), so they can be read in the inbox.
  gershon_reports.py drafts  -> emails ALL drafts currently in pending_review, in full
                                (always sends when there is at least one).

Config (in /opt/harvey/.env):
  RESEND_API_KEY=...            send-only key, gershon.ai domain
  REPORT_TO=harvey@gershonconsulting.com
  REPORT_FROM=GC Harvey <harvey@gershon.ai>
Stdlib only; reads data/harvey.db read-only and never changes Harvey's state.
"""
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "harvey.db"
STATE = ROOT / "data" / "gershon_alerts.json"


def load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("RESEND_", "REPORT_"))})
    return env


def q(sql, args=()):
    if not DB.exists():
        return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def sh(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"(could not run {cmd[0]}: {e})"


def snapshot():
    status = sh([str(ROOT / ".venv/bin/harvey"), "status", "--check"])
    service = sh(["systemctl", "is-active", "harvey"])
    outbox = dict(q("SELECT status, COUNT(*) FROM outbox GROUP BY status"))
    prospects = dict(q("SELECT status, COUNT(*) FROM prospects GROUP BY status"))
    open_conv = q("SELECT COUNT(*) FROM conversations WHERE status='open'")
    replies = q("SELECT COUNT(*) FROM processed_replies")
    errors = q("SELECT to_email, subject, error, updated_at FROM outbox "
               "WHERE error != '' ORDER BY updated_at DESC LIMIT 5")
    sent_24h = q("SELECT COUNT(*) FROM outbox WHERE sent_at >= datetime('now','-1 day')")
    return {
        "service": service,
        "brain_ready": "NOT READY" not in status,
        "status_text": status,
        "outbox": outbox,
        "prospects": prospects,
        "pending_review": outbox.get("pending_review", 0),
        "open_conversations": open_conv[0][0] if open_conv else 0,
        "replies_total": replies[0][0] if replies else 0,
        "sent_24h": sent_24h[0][0] if sent_24h else 0,
        "errors": [list(e) for e in errors],
    }


def send(env, subject, text):
    key = env.get("RESEND_API_KEY")
    if not key:
        print("RESEND_API_KEY missing in .env", file=sys.stderr)
        return False
    body = json.dumps({
        "from": env.get("REPORT_FROM", "GC Harvey <harvey@gershon.ai>"),
        "to": [env.get("REPORT_TO", "harvey@gershonconsulting.com")],
        "subject": subject,
        "text": text,
    }).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "gershon-harvey-reports/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(r.status, r.read().decode()[:200])
    return True


LEAD_CAMPAIGN = "GC Campaign H01"


def pending_drafts():
    """Every outbox email waiting for approval, oldest first, with who it is for."""
    return q("""SELECT o.id, o.to_email, o.subject, o.body, o.step, o.kind, o.created_at,
                       COALESCE(p.first_name, ''), COALESCE(p.last_name, ''),
                       COALESCE(p.title, ''),
                       COALESCE(NULLIF(p.company, ''), co.name, ''),
                       COALESCE(co.website, '')
                FROM outbox o
                LEFT JOIN prospects p ON p.id = o.prospect_id
                LEFT JOIN companies co ON co.id = p.company_id
                WHERE o.status = 'pending_review'
                ORDER BY o.created_at, o.id""")


def fmt_drafts(drafts):
    """Plain-text email body: one numbered block per draft, full text, nothing cut."""
    out = [f"{len(drafts)} email draft(s) waiting for your approval in Harvey.",
           "Nothing has been sent. Each draft goes out only after it is approved.", ""]
    for i, d in enumerate(drafts, 1):
        oid, to, subject, body, step, kind, created, first, last, title, company, site = d
        name = f"{first} {last}".strip() or "-"
        out += [
            "=" * 60,
            f"DRAFT {i} of {len(drafts)}",
            f"To: {name} <{to or '-'}>",
            f"Title: {title or '-'}",
            f"Company: {company or '-'} {site}".rstrip(),
            f"Subject: {subject or '-'}",
            f"Step: {step} ({kind}) | created {created} UTC | id {oid}",
            "-" * 60,
            (body or "(empty body)").rstrip(),
            "",
        ]
    out.append("Server: srv1511567.hstgr.cloud /opt/harvey")
    return "\n".join(out)


def send_drafts(env, drafts, label="new"):
    if not drafts:
        return False
    names = ", ".join(sorted({d[10] for d in drafts if d[10]}))[:80]
    subject = f"ℹ️ GC Harvey: {len(drafts)} {label} draft(s) to approve" + (f" — {names}" if names else "")
    draft_env = dict(env)
    if env.get("DRAFTS_TO"):
        draft_env["REPORT_TO"] = env["DRAFTS_TO"]
    return send(draft_env, subject, fmt_drafts(drafts))


def waiting_alert_due(n, prev_pending, state, today):
    """Should the "N waiting for approval" alert go out now?

    Needs N to have grown since the last run, and either nothing was sent yet today or
    N is higher than the N we last told you about (so 33 -> 32 -> 33 stays quiet).
    """
    if n <= prev_pending:
        return False
    if state.get("waiting_sent_date") != today:
        return True
    return n > state.get("waiting_sent_n", 0)


def new_leads(seen):
    """Conversations Harvey classified as interested (or further along) not yet notified."""
    rows = q("""SELECT c.id, c.intent, c.stage, c.thread_json, c.updated_at,
                       p.first_name, p.last_name, p.title, p.email, p.linkedin_url,
                       COALESCE(NULLIF(p.company, ''), co.name, ''), COALESCE(co.website, '')
                FROM conversations c
                LEFT JOIN prospects p ON p.id = c.prospect_id
                LEFT JOIN companies co ON co.id = p.company_id
                WHERE c.intent = 'interested'
                   OR c.stage IN ('presenting', 'negotiating', 'ready_to_meet', 'meeting_booked')
                ORDER BY c.updated_at""")
    return [r for r in rows if r[0] not in seen]


def last_inbound(thread_json):
    try:
        thread = json.loads(thread_json or "[]")
    except ValueError:
        return ""
    for m in reversed(thread):
        if isinstance(m, dict) and (m.get("sender") or m.get("role")) == "prospect":
            return str(m.get("content") or m.get("body") or m.get("text") or "")[:3000]
    if thread and isinstance(thread[-1], dict):
        return str(thread[-1].get("content") or thread[-1].get("body") or "")[:3000]
    return ""


def send_lead(env, r):
    cid, intent, stage, thread, updated, first, last, title, email, li, company, site = r
    name = f"{first} {last}".strip() or email or "Unknown"
    subject = f"{LEAD_CAMPAIGN} — New lead: {name}" + (f", {company}" if company else "")
    body = "\n".join([
        f"New lead from Harvey ({LEAD_CAMPAIGN})",
        "",
        f"Name: {name}",
        f"Title: {title or '-'}",
        f"Company: {company or '-'} {site}".rstrip(),
        f"Email: {email or '-'}",
        f"LinkedIn: {li or '-'}",
        f"Intent: {intent or '-'} | Stage: {stage or '-'} | {updated} UTC",
        "",
        "Their latest message:",
        last_inbound(thread) or "(not available)",
        "",
        "Booking link: https://www.gershonconsulting.com/calendar",
        f"Conversation id: {cid} — Harvey dashboard on srv1511567 (/opt/harvey)",
    ])
    lead_env = dict(env)
    lead_env["REPORT_FROM"] = env.get("LEAD_FROM", f"{LEAD_CAMPAIGN} <harvey@gershon.ai>")
    if env.get("LEAD_TO"):
        lead_env["REPORT_TO"] = env["LEAD_TO"]
    return send(lead_env, subject, body)


def fmt(s):
    lines = [
        f"Service: {s['service']}",
        f"Brain (ChatGPT/Codex): {'ready' if s['brain_ready'] else 'NOT READY'}",
        f"Emails waiting for your approval: {s['pending_review']}",
        f"Emails sent in last 24h: {s['sent_24h']}",
        f"Open conversations: {s['open_conversations']}",
        f"Replies processed (total): {s['replies_total']}",
        f"Outbox by status: {s['outbox'] or '{}'}",
        f"Prospects by status: {s['prospects'] or '{}'}",
    ]
    if s["errors"]:
        lines.append("\nRecent send errors:")
        lines += [f"- {e[0]} | {e[1]} | {e[2]} ({e[3]})" for e in s["errors"]]
    lines.append("\n--- harvey status --check ---\n" + s["status_text"])
    lines.append("\nServer: srv1511567.hstgr.cloud /opt/harvey")
    return "\n".join(lines)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    env = load_env()
    s = snapshot()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if mode == "daily":
        # red cross only for real failures; waiting drafts are information, not a fault
        flag = ("❌" if (s["service"] != "active" or not s["brain_ready"])
                else "ℹ️" if s["pending_review"] else "✅")
        send(env, f"{flag} GC Harvey daily report {today}", fmt(s))
        return
    if mode == "test-lead":
        send_lead(env, ("test", "interested", "presenting", json.dumps([{"sender": "prospect",
                  "content": "Sounds interesting — can we talk next week?"}]), "now",
                  "Test", "Lead", "CEO", "test@example.com", "", "Example Ltd", ""))
        return
    if mode == "drafts":
        if not send_drafts(env, pending_drafts(), label="waiting"):
            print("no drafts waiting")
        return
    if mode == "test":
        send(env, "✅ GC Harvey reports are connected", "Harvey can email you.\n\n" + fmt(s))
        return
    prev = json.loads(STATE.read_text()) if STATE.exists() else {}
    seen = set(prev.get("leads_notified", []))
    for r in new_leads(seen):
        try:
            if send_lead(env, r):
                seen.add(r[0])
        except Exception as e:  # noqa: BLE001
            print(f"lead email failed for {r[0]}: {e}", file=sys.stderr)
    drafts_seen = set(prev.get("drafts_notified", []))
    fresh = [d for d in pending_drafts() if d[0] not in drafts_seen]
    if fresh:
        try:
            if send_drafts(env, fresh):
                drafts_seen.update(d[0] for d in fresh)
        except Exception as e:  # noqa: BLE001
            print(f"drafts email failed: {e}", file=sys.stderr)
    still_pending = {d[0] for d in pending_drafts()}
    alerts = []      # (is_failure, text)
    waiting_sent = False
    if s["service"] != "active" and prev.get("service") == "active":
        alerts.append((True, f"Harvey service is {s['service']} (was active)."))
    if not s["brain_ready"] and prev.get("brain_ready", True):
        alerts.append((True, "Harvey's brain (ChatGPT/Codex login) is NOT READY — Harvey is paused."))
    if waiting_alert_due(s["pending_review"], prev.get("pending_review", 0), prev, today) and not fresh:
        alerts.append((False, f"{s['pending_review']} email(s) are waiting for your approval."))
        waiting_sent = True
    if s["replies_total"] > prev.get("replies_total", 0):
        alerts.append((False, f"{s['replies_total'] - prev.get('replies_total', 0)} new prospect reply(ies)."))
    new_err = [e for e in s["errors"] if e not in prev.get("errors", [])]
    if new_err and prev:
        alerts.append((True, f"{len(new_err)} new send error(s): " + "; ".join(e[2] for e in new_err)))
    state = {k: s[k] for k in ("service", "brain_ready", "pending_review", "replies_total", "errors")}
    state["leads_notified"] = sorted(seen)
    # keep only ids still pending, so the state file does not grow forever
    state["drafts_notified"] = sorted(drafts_seen & still_pending)
    # last "waiting" alert actually sent: kept across runs, reset by the date check
    state["waiting_sent_date"] = prev.get("waiting_sent_date", "")
    state["waiting_sent_n"] = prev.get("waiting_sent_n", 0)
    if alerts and prev:
        failure = any(f for f, _ in alerts)
        texts = [t for _, t in alerts]
        try:
            send(env, ("❌" if failure else "ℹ️") + " GC Harvey: " + texts[0][:80],
                 "\n".join(texts) + "\n\n" + fmt(s))
            if waiting_sent:
                if state["waiting_sent_date"] != today:
                    state["waiting_sent_n"] = 0
                state["waiting_sent_date"] = today
                state["waiting_sent_n"] = max(state["waiting_sent_n"], s["pending_review"])
        except Exception as e:  # noqa: BLE001
            print(f"alert email failed: {e}", file=sys.stderr)
    STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
