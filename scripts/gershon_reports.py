#!/usr/bin/env python3
"""Gershon add-on: email Harvey reports and alerts through Resend.

  gershon_reports.py daily   -> the daily report (scripts/gershon_daily.py, always sends)
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


def prospect_wrote(thread_json):
    try:
        msgs = json.loads(thread_json or "[]")
    except (TypeError, ValueError):
        return False
    return any(isinstance(m, dict) and m.get("sender") == "prospect" for m in msgs)

def in_gmail_drafts():
    """Emails really sitting in Gmail Drafts: pending rows the drafts sync put there."""
    try:
        tracked = json.loads((ROOT / "data" / "gmail_drafts.json").read_text())
    except Exception:
        return 0
    ids = [r[0] for r in q("SELECT id FROM outbox WHERE status='pending_review'")]
    return sum(1 for i in ids if i in tracked)


def login_auto_renewing():
    """Codex renews the login itself once it is 8 days old (harvey-codex-renew checks
    every 15 min); Harvey pauses 1h before that. Not an outage, so no red alert."""
    try:
        lr = json.loads(open("/home/harvey/.codex/auth.json").read())["last_refresh"]
        lr = datetime.fromisoformat(lr.replace("Z", "")[:26]).replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - lr).total_seconds() / 3600
        return 8 * 24 - 2 <= age <= 8 * 24 + 1
    except Exception:
        return False


def snapshot():
    status = sh([str(ROOT / ".venv/bin/harvey"), "status", "--check"])
    service = sh(["systemctl", "is-active", "harvey"])
    outbox = dict(q("SELECT status, COUNT(*) FROM outbox GROUP BY status"))
    prospects = dict(q("SELECT status, COUNT(*) FROM prospects GROUP BY status"))
    open_conv = q("SELECT COUNT(*) FROM conversations WHERE status='open'")
    replies = q("SELECT COUNT(*) FROM processed_replies")
    convs = q("SELECT thread_json FROM conversations")
    errors = q("SELECT to_email, subject, error, updated_at FROM outbox "
               "WHERE error != '' ORDER BY updated_at DESC LIMIT 5")
    sent_24h = q("SELECT COUNT(*) FROM outbox WHERE sent_at >= datetime('now','-1 day')")
    return {
        "service": service,
        "brain_ready": "NOT READY" not in status or login_auto_renewing(),
        "status_text": status,
        "outbox": outbox,
        "prospects": prospects,
        "pending_review": outbox.get("pending_review", 0),
        "in_drafts": in_gmail_drafts(),
        "open_conversations": open_conv[0][0] if open_conv else 0,
        # every inbox message Harvey looked at (newsletters included), NOT prospect replies
        "inbox_checked": replies[0][0] if replies else 0,
        # conversations where a prospect actually wrote back
        "prospect_replies": sum(1 for (t,) in convs if prospect_wrote(t)),
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


GMAIL_DRAFTS_LINK = "https://mail.google.com/mail/u/olivier@gershonconsulting.net/#drafts"


def gmail_review(env):
    """Review happens in Gmail Drafts (scripts/gershon_gmail_drafts.py): no
    draft-by-email, no separate "waiting" alert — that script sends one notice."""
    return env.get("GMAIL_DRAFTS", "1") != "0"


def fmt(s, env=None):
    where = (f" — review them in Gmail Drafts: {GMAIL_DRAFTS_LINK}"
             if s["pending_review"] and gmail_review(env or {}) else "")
    lines = [
        f"Service: {s['service']}",
        f"Brain (ChatGPT/Codex): {'ready' if s['brain_ready'] else 'NOT READY'}",
        f"Emails waiting for your approval: {s['pending_review']}{where}",
        f"Emails sent in last 24h: {s['sent_24h']}",
        f"Open conversations: {s['open_conversations']}",
        f"Prospect replies (total): {s['prospect_replies']}",
        f"Inbox messages checked (total, incl. newsletters): {s.get('inbox_checked', 0)}",
        f"Outbox by status: {s['outbox'] or '{}'}",
        f"Prospects by status: {s['prospects'] or '{}'}",
    ]
    if s["errors"]:
        lines.append("\nRecent send errors:")
        lines += [f"- {e[0]} | {e[1]} | {e[2]} ({e[3]})" for e in s["errors"]]
    lines.append("\n--- harvey status --check ---\n" + s["status_text"])
    lines.append("\nServer: srv1511567.hstgr.cloud /opt/harvey")
    return "\n".join(lines)


SALES_LINK = "https://sales.gershon.ai/#harvey"


def reply_threads():
    """Conversations where the prospect actually wrote, with who they are and what they said."""
    rows = q("""SELECT c.id, c.intent, c.thread_json,
                       p.first_name, p.last_name, p.title, p.email,
                       COALESCE(NULLIF(p.company, ''), co.name, '')
                FROM conversations c
                LEFT JOIN prospects p ON p.id = c.prospect_id
                LEFT JOIN companies co ON co.id = p.company_id
                ORDER BY c.updated_at""")
    return [r for r in rows if prospect_wrote(r[2])]


INTENT_WORDS = {
    "interested": "is interested",
    "question": "has a question",
    "objection": "pushed back",
    "not_interested": "said no",
    "unsubscribe": "asked to stop",
    "out_of_office": "is out of office (auto-reply)",
    "referral": "pointed you to someone else",
}


def plain_problem(err):
    e = (err or "").lower()
    if "risky" in e or "not deliverable" in e or "invalid" in e:
        return "address not confirmed as real, so Harvey held it back (nothing to do)"
    if "quota" in e or "limit" in e:
        return "daily sending limit reached"
    if "auth" in e or "token" in e or "401" in e or "403" in e:
        return "Gmail connection refused"
    return (err or "no reason given")[:120]


def alert_body(headline, details, s):
    """Short, readable alert: what happened, what to do, where things stand. No raw dumps."""
    out = [headline, ""]
    out += details
    out += ["", "Where things stand"]
    if s.get("in_drafts"):
        out.append(f"- {s['in_drafts']} emails waiting for you in Gmail Drafts: {GMAIL_DRAFTS_LINK}")
    out.append(f"- Emails sent in the last 24h: {s['sent_24h']}")
    out.append(f"- Harvey: {'running' if s['service'] == 'active' else s['service'].upper()}"
               f" · ChatGPT login: {'OK' if s['brain_ready'] else 'EXPIRED'}")
    out += ["", f"Full campaign view: {SALES_LINK}",
            "The daily report (8:00) has the details and the 10-companies/day target."]
    return "\n".join(out)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode == "daily":
        # the daily report is its own script now (one readable email, HTML + text)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gershon_daily
        sys.exit(gershon_daily.main([]))
    env = load_env()
    s = snapshot()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
    if fresh and gmail_review(env):
        drafts_seen.update(d[0] for d in fresh)  # they go to Gmail Drafts instead
        fresh = []
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
    if (not gmail_review(env) and not fresh
            and waiting_alert_due(s["pending_review"], prev.get("pending_review", 0), prev, today)):
        alerts.append((False, f"{s['pending_review']} email(s) are waiting for your approval."))
        waiting_sent = True
    threads = reply_threads()
    if "replies_notified" in prev:
        replied = set(prev["replies_notified"])
    else:
        # first run of this version: older replies were already announced by count
        replied = {t[0] for t in threads} if s["prospect_replies"] <= prev.get("prospect_replies", 0) else set()
    details = []
    for cid, intent, thread, first, last, title, email, company in threads:
        if cid in replied:
            continue
        replied.add(cid)
        if cid in seen:  # already got its own "New lead" email: one email per event
            continue
        name = f"{first or ''} {last or ''}".strip() or email or "A prospect"
        who = name + (f", {title}" if title else "") + (f" at {company}" if company else "")
        msg = " ".join(last_inbound(thread).split())
        what = INTENT_WORDS.get(intent or "", "replied")
        alerts.append((False, f"{name}" + (f" ({company})" if company else "") + f" {what}"))
        details += [f"{who} {what}:", f'  "{msg[:600]}"' if msg else "  (message not available)",
                    f"  Email: {email or '-'}", ""]
    if details:
        details.append("Answer from olivier@gershonconsulting.net "
                       "(Harvey drafts a reply in Gmail Drafts when it can).")
    new_err = [e for e in s["errors"] if e not in prev.get("errors", [])]
    if new_err and prev:
        real = [e for e in new_err if "risky" not in (e[2] or "").lower()
                and "not deliverable" not in (e[2] or "").lower()]
        if real:  # held-back risky addresses are Harvey working as intended, not news
            alerts.append((True, f"{len(real)} email(s) failed to send"))
            details += [f"- {e[0]}: {plain_problem(e[2])}" for e in real]
    state = {k: s[k] for k in ("service", "brain_ready", "pending_review", "prospect_replies", "errors")}
    state["replies_notified"] = sorted(replied)
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
            head = texts[0] if len(texts) == 1 else f"{texts[0]} (+{len(texts) - 1} more)"
            send(env, ("❌" if failure else "ℹ️") + " Harvey H01: " + head[:90],
                 alert_body("\n".join(f"• {t}" for t in texts), details, s))
            if waiting_sent:
                if state["waiting_sent_date"] != today:
                    state["waiting_sent_n"] = 0
                state["waiting_sent_date"] = today
                state["waiting_sent_n"] = max(state["waiting_sent_n"], s["pending_review"])
        except Exception as e:  # noqa: BLE001
            print(f"alert email failed: {e}", file=sys.stderr)
    STATE.write_text(json.dumps(state))
    if env.get("STREAK_API_KEY"):
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import gershon_streak
            gershon_streak.DB = DB
            gershon_streak.STATE = ROOT / "data" / "gershon_streak.json"
            gershon_streak.sync(env)
        except Exception as e:  # noqa: BLE001
            print(f"streak sync failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
