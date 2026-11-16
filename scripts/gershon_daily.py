#!/usr/bin/env python3
"""Gershon add-on: the Harvey H01 daily report, written for Olivier (not for a sysadmin).

One email a day replaces the old "daily report" (raw counters) and "daily stats" (text table):

  1. the verdict in one sentence: did Harvey hit the target yesterday, and if not, why
  2. what needs you: emails to review in Gmail Drafts, leads to answer
  3. what is broken, in plain words, and what it stops
  4. yesterday's new companies, with the contact found and whether an email is ready
  5. the last 7 days against the target
  6. the campaign funnel from company found to interested lead

  gershon_daily.py            -> send the report
  gershon_daily.py --preview  -> write data/daily_preview.html, send nothing

Config (/opt/harvey/.env):
  RESEND_API_KEY, REPORT_FROM           as for gershon_reports.py
  REPORT_TO=harvey@gershonconsulting.com
  STATS_TO=report@gershonconsulting.com  (optional, also receives it; comma-separated ok)
  DAILY_TARGET=10                        new companies per day
  REPORT_UTC_OFFSET=-4                   days are counted in Sint Maarten time
Stdlib only; reads data/harvey.db read-only and never changes Harvey's state.
"""
import html
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("HARVEY_ROOT") or Path(__file__).resolve().parent.parent)
DB = ROOT / "data" / "harvey.db"
GMAIL_STATE = ROOT / "data" / "gmail_drafts.json"
GMAIL_DISMISSED = ROOT / "data" / "gmail_drafts_dismissed.json"
CODEX_AUTH = Path(os.environ.get("CODEX_AUTH", "/home/harvey/.codex/auth.json"))

CAMPAIGN = "H01"
DRAFTS_LINK = "https://mail.google.com/mail/u/olivier@gershonconsulting.net/#drafts"
SALES_LINK = "https://sales.gershon.ai/#harvey"
RED, DARK, INK, MUTED, LINE, TINT, GREEN = (
    "#FE1B04", "#d11200", "#1d1d1f", "#6b7280", "#e5e7eb", "#fff5f5", "#0f8a4b")
LEAD_STAGES = ("presenting", "negotiating", "ready_to_meet", "meeting_booked")
SENDABLE = ("verified", "risky")


# ── reading ──────────────────────────────────────────────────────────────────
def load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.startswith(("RESEND_", "REPORT_", "STATS_", "DAILY_")):
            env[k] = v
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


def one(sql, args=()):
    r = q(sql, args)
    return (r[0][0] or 0) if r else 0


def sh(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"(could not run {cmd[0]}: {e})"


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return {}


def brain_since():
    """Day the ChatGPT login was last refreshed, if the file says so."""
    a = read_json(CODEX_AUTH)
    return str(a.get("last_refresh") or "")[:10]


def collect(env, now=None, probe=True):
    """Everything the report says, as plain data. `probe=False` skips shell calls (tests)."""
    off = float(env.get("REPORT_UTC_OFFSET", "-4"))
    target = int(env.get("DAILY_TARGET", "10"))
    now = now or datetime.now(timezone.utc)
    local = now + timedelta(hours=off)
    today = local.date()
    yday = today - timedelta(days=1)
    shift = f"{off:+g} hours"

    def per_day(table, col="created_at", where="1=1"):
        return {r[0]: r[1] for r in q(
            f"SELECT date({col}, ?), COUNT(*) FROM {table} "
            f"WHERE {col} IS NOT NULL AND {col} != '' AND {where} GROUP BY 1", (shift,))}

    comp, pros = per_day("companies"), per_day("prospects")
    written = per_day("outbox", where="step = 1 AND kind = 'sequence'")
    sent = per_day("outbox", "sent_at", "status = 'sent'")
    days = []
    for i in range(7, 0, -1):
        d = (today - timedelta(days=i)).isoformat()
        days.append({"date": d, "companies": comp.get(d, 0), "contacts": pros.get(d, 0),
                     "written": written.get(d, 0), "sent": sent.get(d, 0)})

    new_companies = q(
        """SELECT c.name, COALESCE(NULLIF(c.website, ''), c.domain, ''), c.location,
                  TRIM(COALESCE(p.first_name, '') || ' ' || COALESCE(p.last_name, '')),
                  COALESCE(p.title, ''), COALESCE(p.email, ''), COALESCE(p.email_status, ''),
                  (SELECT COUNT(*) FROM outbox o WHERE o.prospect_id = p.id
                    AND o.status IN ('pending_review', 'approved', 'sent'))
           FROM companies c LEFT JOIN prospects p ON p.company_id = c.id
           WHERE date(c.created_at, ?) = ? ORDER BY c.created_at, c.id""",
        (shift, yday.isoformat()))
    by_co = {}
    for name, site, loc, person, title, email, status, drafts in new_companies:
        e = by_co.setdefault(name or site or "(no name)", {
            "name": name or site or "(no name)", "site": site, "location": loc or "",
            "contacts": [], "email_ready": False, "sendable": False})
        if person or email:
            e["contacts"].append({"name": person, "title": title, "email": email, "status": status})
            e["sendable"] = e["sendable"] or status in SENDABLE
            e["email_ready"] = e["email_ready"] or bool(drafts)

    pending = q("SELECT id, step, kind FROM outbox WHERE status = 'pending_review'")
    tracked, dismissed = read_json(GMAIL_STATE), read_json(GMAIL_DISMISSED)
    in_gmail = [p for p in pending if p[0] in tracked]
    put_aside = [p for p in pending if p[0] in dismissed and p[0] not in tracked]
    later = [p for p in pending if p[0] not in tracked and p[0] not in dismissed]

    leads = q(
        """SELECT TRIM(COALESCE(p.first_name, '') || ' ' || COALESCE(p.last_name, '')),
                  COALESCE(NULLIF(p.company, ''), co.name, ''), c.intent, c.stage, c.updated_at
           FROM conversations c LEFT JOIN prospects p ON p.id = c.prospect_id
           LEFT JOIN companies co ON co.id = p.company_id
           WHERE c.intent = 'interested' OR c.stage IN (?, ?, ?, ?)
           ORDER BY c.updated_at DESC LIMIT 10""", LEAD_STAGES)
    errors = q("SELECT to_email, error FROM outbox WHERE error != '' "
               "AND updated_at >= datetime('now', '-1 day') ORDER BY updated_at DESC LIMIT 5")

    status = sh([str(ROOT / ".venv/bin/harvey"), "status", "--check"]) if probe else ""
    funnel = [
        ("Companies found", one("SELECT COUNT(*) FROM companies")),
        ("With a contact", one(
            "SELECT COUNT(DISTINCT company_id) FROM prospects WHERE company_id != ''")),
        ("Contact email confirmed", one(
            "SELECT COUNT(DISTINCT company_id) FROM prospects "
            "WHERE company_id != '' AND email_status IN ('verified', 'risky')")),
        ("First email written", one(
            "SELECT COUNT(DISTINCT p.company_id) FROM outbox o JOIN prospects p "
            "ON p.id = o.prospect_id WHERE o.step = 1 "
            "AND o.status IN ('pending_review', 'approved', 'sent')")),
        ("Email sent", one(
            "SELECT COUNT(DISTINCT p.company_id) FROM outbox o JOIN prospects p "
            "ON p.id = o.prospect_id WHERE o.status = 'sent'")),
        ("Replied", one("SELECT COUNT(DISTINCT prospect_id) FROM conversations")),
        ("Interested", one(
            "SELECT COUNT(*) FROM conversations WHERE intent = 'interested' "
            "OR stage IN (?, ?, ?, ?)", LEAD_STAGES)),
    ]
    return {
        "today": today.isoformat(), "yesterday": yday.isoformat(), "target": target,
        "days": days, "y": days[-1], "new_companies": list(by_co.values()),
        "drafts_in_gmail": len(in_gmail), "drafts_put_aside": len(put_aside),
        "drafts_later": len(later), "gmail_sync_known": bool(tracked or dismissed),
        "pending_total": len(pending),
        "leads": [list(r) for r in leads], "errors": [list(r) for r in errors],
        "write_failed": one("SELECT COUNT(*) FROM prospects WHERE status = 'write_failed'"),
        "service": sh(["systemctl", "is-active", "harvey"]) if probe else "active",
        "loop": sh(["systemctl", "is-active", "harvey-loop"]) if probe else "active",
        "brain_ready": "NOT READY" not in status,
        "brain_since": brain_since() if probe else "",
        "paused": one("SELECT value FROM settings WHERE key = 'sending_paused'") in ("1", "true"),
        "funnel": funnel,
        "sent_total": one("SELECT COUNT(*) FROM outbox WHERE status = 'sent'"),
    }


# ── judging ──────────────────────────────────────────────────────────────────
def problems(d):
    """What is broken, each as (headline, what it stops). Plain words, no log lines."""
    out = []
    if d["service"] != "active":
        out.append(("Harvey is not running on the server",
                    "Nothing happens until it is restarted: no research, no emails, no reply tracking."))
    if d["loop"] != "active":
        out.append(("Harvey's prospecting is stopped",
                    "No new companies are being searched for."))
    if not d["brain_ready"]:
        since = f" (last renewed {d['brain_since']})" if d["brain_since"] else ""
        out.append((f"Harvey's ChatGPT login has expired{since}",
                    "Harvey cannot research companies or write emails until it is signed in again. "
                    "This is why no new email is being written."))
    if d["write_failed"]:
        out.append((f"{d['write_failed']} contact(s) have no email written",
                    "Harvey's drafts for them were not good enough to keep; they will be retried."))
    for to, err in d["errors"]:
        e = (err or "").lower()
        if "risky" in e or "not deliverable" in e or "invalid" in e:
            why = "the address could not be confirmed as real, so the email was held back"
        elif "quota" in e or "limit" in e or "cap" in e:
            why = "the daily sending limit was reached"
        elif "auth" in e or "token" in e or "401" in e or "403" in e:
            why = "the Gmail connection was refused"
        else:
            why = "the send failed (" + (err or "no reason given")[:120] + ")"
        out.append((f"Email to {to} did not go out", why[:1].upper() + why[1:] + "."))
    return out


def verdict(d, probs):
    y, t = d["y"]["companies"], d["target"]
    blocked = [p for p in probs if p[0].startswith(("Harvey is not", "Harvey's prospecting",
                                                    "Harvey's ChatGPT"))]
    if y >= t and not blocked:
        return True, f"Target met: {y} new companies yesterday (target {t})."
    if blocked:
        main = blocked[0][0]
        head = (f"{y} new companies yesterday (target {t})" if y >= t
                else f"Target missed: {y} new companies yesterday (target {t})")
        return False, f"{head}. Main problem: {main}."
    return False, f"Target missed: {y} new companies yesterday (target {t})."


def subject(d, ok, probs):
    bits = [f"{d['y']['companies']}/{d['target']} new companies"]
    if d["drafts_in_gmail"]:
        bits.append(f"{d['drafts_in_gmail']} emails to review")
    if d["leads"]:
        bits.append(f"{len(d['leads'])} interested")
    if probs:
        bits.append(f"{len(probs)} problem" + ("s" if len(probs) > 1 else ""))
    return f"{'✅' if ok and not probs else '❌'} Harvey {CAMPAIGN} daily {d['today']}: " + " · ".join(bits)


def for_you(d):
    """Only the things Olivier himself acts on."""
    out = []
    if d["drafts_in_gmail"]:
        out.append((f"Review {d['drafts_in_gmail']} email(s) in Gmail Drafts",
                    "Send the ones you like from Gmail. Deleting one rejects nothing.", DRAFTS_LINK))
    elif d["pending_total"] and not d["gmail_sync_known"]:
        out.append((f"{d['pending_total']} email(s) are waiting for your approval",
                    "They have not reached Gmail Drafts yet.", SALES_LINK))
    if d["leads"]:
        out.append((f"Answer {len(d['leads'])} interested prospect(s)",
                    ", ".join(f"{n or '?'} ({c or '?'})" for n, c, *_ in d["leads"][:5]), SALES_LINK))
    return out


# ── writing ──────────────────────────────────────────────────────────────────
def esc(s):
    return html.escape(str(s or ""))


def short_day(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%a %d %b")


def render_text(d):
    probs = problems(d)
    ok, line = verdict(d, probs)
    out = [f"Harvey {CAMPAIGN} daily report, {short_day(d['today'])}", "", line, "", "FOR YOU"]
    todo = for_you(d)
    out += [f"- {t}: {s} {u}" for t, s, u in todo] or ["- Nothing to do today."]
    if probs:
        out += ["", "BROKEN"] + [f"- {h}. {w}" for h, w in probs]
    out += ["", f"NEW COMPANIES YESTERDAY ({len(d['new_companies'])})"]
    for c in d["new_companies"]:
        who = "; ".join(f"{p['name'] or p['email']}" + (f", {p['title']}" if p["title"] else "")
                        for p in c["contacts"]) or "no contact found yet"
        state = ("email ready" if c["email_ready"] else
                 "email confirmed, not written yet" if c["sendable"] else "no confirmed email")
        out.append(f"- {c['name']} ({c['site']}): {who} [{state}]")
    if not d["new_companies"]:
        out.append("- None.")
    out += ["", "LAST 7 DAYS (new companies / contacts / emails written / sent)"]
    out += [f"- {short_day(x['date'])}: {x['companies']} / {x['contacts']} / {x['written']} / {x['sent']}"
            + ("  target met" if x["companies"] >= d["target"] else "") for x in d["days"]]
    out += ["", "CAMPAIGN SO FAR"] + [f"- {k}: {v}" for k, v in d["funnel"]]
    out += ["", f"Everything on {SALES_LINK}"]
    return "\n".join(out)


def render_html(d):
    probs = problems(d)
    ok, line = verdict(d, probs)
    good = ok and not probs
    td = f"padding:8px 10px;border-bottom:1px solid {LINE};font-size:14px;color:{INK}"
    th = (f"padding:8px 10px;border-bottom:2px solid {LINE};font-size:11px;color:{MUTED};"
          "text-transform:uppercase;letter-spacing:.04em;text-align:left")

    def h2(t):
        return (f'<tr><td style="padding:26px 28px 8px;font-size:12px;font-weight:700;color:{MUTED};'
                f'text-transform:uppercase;letter-spacing:.06em">{esc(t)}</td></tr>')

    def tile(n, label, sub=""):
        return (f'<td width="25%" style="padding:14px 8px;text-align:center;border:1px solid {LINE};'
                f'border-radius:12px"><div style="font-size:26px;font-weight:700;color:{INK}">{n}</div>'
                f'<div style="font-size:12px;color:{MUTED}">{esc(label)}</div>'
                f'<div style="font-size:11px;color:{MUTED}">{esc(sub)}</div></td>')

    parts = [
        f'<div style="background:#f5f5f7;padding:24px 0;font-family:-apple-system,Segoe UI,Helvetica,'
        f'Arial,sans-serif;color:{INK}">',
        '<table role="presentation" width="640" align="center" cellpadding="0" cellspacing="0" '
        'style="max-width:640px;width:100%;background:#ffffff;border-radius:12px">',
        f'<tr><td style="padding:24px 28px 4px;font-size:13px;color:{MUTED}">'
        f'<span style="color:{RED};font-weight:700">Gershon</span> · Harvey campaign {CAMPAIGN} · '
        f'{esc(short_day(d["today"]))}</td></tr>',
        f'<tr><td style="padding:6px 28px 0"><div style="padding:16px 18px;border-radius:12px;'
        f'background:{"#f0faf4" if good else TINT};border-left:5px solid {GREEN if good else RED};'
        f'font-size:18px;line-height:1.4;font-weight:600">{"✅" if good else "❌"} {esc(line)}</div></td></tr>',
        '<tr><td style="padding:16px 22px 0"><table role="presentation" width="100%" cellspacing="6" '
        'cellpadding="0"><tr>',
        tile(f"{d['y']['companies']}<span style=\"font-size:15px;color:{MUTED}\">/{d['target']}</span>",
             "new companies", "yesterday"),
        tile(d["y"]["contacts"], "new contacts", "yesterday"),
        tile(d["drafts_in_gmail"], "emails to review", "in Gmail Drafts"),
        tile(d["sent_total"], "emails sent", "since the start"),
        "</tr></table></td></tr>",
        h2("For you"),
    ]
    todo = for_you(d)
    if todo:
        for t, s, u in todo:
            parts.append(
                f'<tr><td style="padding:4px 28px"><table role="presentation" width="100%"><tr>'
                f'<td style="font-size:15px;line-height:1.45"><b>{esc(t)}</b><br>'
                f'<span style="color:{MUTED};font-size:13px">{esc(s)}</span></td>'
                f'<td align="right" width="110"><a href="{esc(u)}" style="background:{RED};color:#fff;'
                f'text-decoration:none;font-size:13px;font-weight:600;padding:9px 16px;'
                f'border-radius:999px;display:inline-block">Open</a></td></tr></table></td></tr>')
    else:
        parts.append(f'<tr><td style="padding:4px 28px;font-size:15px;color:{MUTED}">'
                     'Nothing to do today.</td></tr>')
    held = []
    if d["drafts_later"]:
        held.append(f"{d['drafts_later']} more written, held until their turn "
                    "(follow-ups wait for the first email to go out)")
    if d["drafts_put_aside"]:
        held.append(f"{d['drafts_put_aside']} you removed from Drafts (kept aside, not rejected)")
    if held:
        parts.append(f'<tr><td style="padding:6px 28px 0;font-size:12px;color:{MUTED}">'
                     f'Also: {esc("; ".join(held))}.</td></tr>')

    if probs:
        parts.append(h2("Broken"))
        for hd, why in probs:
            parts.append(f'<tr><td style="padding:4px 28px;font-size:15px;line-height:1.45">'
                         f'<b style="color:{DARK}">{esc(hd)}</b><br>'
                         f'<span style="color:{MUTED};font-size:13px">{esc(why)}</span></td></tr>')

    parts.append(h2(f"New companies yesterday ({len(d['new_companies'])})"))
    if d["new_companies"]:
        parts.append('<tr><td style="padding:0 18px"><table role="presentation" width="100%" '
                     f'cellspacing="0" cellpadding="0"><tr><th style="{th}">Company</th>'
                     f'<th style="{th}">Contact</th><th style="{th}">Email</th></tr>')
        for c in d["new_companies"]:
            site = c["site"] if c["site"].startswith("http") else ("https://" + c["site"] if c["site"] else "")
            name = (f'<a href="{esc(site)}" style="color:{INK};font-weight:600">{esc(c["name"])}</a>'
                    if site else f"<b>{esc(c['name'])}</b>")
            who = "<br>".join(
                esc(p["name"] or p["email"]) + (f' <span style="color:{MUTED}">{esc(p["title"])}</span>'
                                                if p["title"] else "") for p in c["contacts"][:2])
            state = ((GREEN, "Ready in Drafts") if c["email_ready"] else
                     (INK, "Confirmed, to write") if c["sendable"] else
                     (MUTED, "No confirmed email") if c["contacts"] else (MUTED, "No contact yet"))
            parts.append(
                f'<tr><td style="{td}">{name}<br><span style="font-size:12px;color:{MUTED}">'
                f'{esc(c["location"])}</span></td><td style="{td}">{who or "—"}</td>'
                f'<td style="{td};color:{state[0]};white-space:nowrap">{state[1]}</td></tr>')
        parts.append("</table></td></tr>")
    else:
        parts.append(f'<tr><td style="padding:4px 28px;font-size:15px;color:{MUTED}">None.</td></tr>')

    parts.append(h2("Last 7 days"))
    top = max([x["companies"] for x in d["days"]] + [d["target"]])
    parts.append('<tr><td style="padding:0 18px"><table role="presentation" width="100%" cellspacing="0" '
                 f'cellpadding="0"><tr><th style="{th}">Day</th><th style="{th}">New companies '
                 f'(target {d["target"]})</th><th style="{th}">Contacts</th><th style="{th}">Written</th>'
                 f'<th style="{th}">Sent</th></tr>')
    for x in d["days"]:
        hit = x["companies"] >= d["target"]
        w = max(2, round(150 * x["companies"] / top)) if x["companies"] else 0
        bar = (f'<span style="display:inline-block;height:10px;width:{w}px;border-radius:5px;'
               f'background:{GREEN if hit else RED};vertical-align:middle"></span> ' if w else "")
        parts.append(f'<tr><td style="{td};white-space:nowrap">{esc(short_day(x["date"]))}</td>'
                     f'<td style="{td}">{bar}<b>{x["companies"]}</b>{" ✅" if hit else ""}</td>'
                     f'<td style="{td}">{x["contacts"]}</td><td style="{td}">{x["written"]}</td>'
                     f'<td style="{td}">{x["sent"]}</td></tr>')
    parts.append("</table></td></tr>")

    parts.append(h2("Campaign so far"))
    base = max(d["funnel"][0][1], 1)
    parts.append('<tr><td style="padding:0 18px"><table role="presentation" width="100%" cellspacing="0" '
                 'cellpadding="0">')
    for label, n in d["funnel"]:
        w = max(2, round(260 * n / base)) if n else 0
        parts.append(f'<tr><td style="{td}" width="190">{esc(label)}</td><td style="{td}">'
                     f'<span style="display:inline-block;height:10px;width:{w}px;border-radius:5px;'
                     f'background:{RED};vertical-align:middle"></span> <b>{n}</b></td></tr>')
    parts.append("</table></td></tr>")

    parts.append(f'<tr><td style="padding:22px 28px 26px;font-size:12px;color:{MUTED}">'
                 f'Leads, emails and replies: <a href="{SALES_LINK}" style="color:{RED}">'
                 f'sales.gershon.ai</a> · Days are counted in Sint Maarten time.</td></tr>')
    parts.append("</table></div>")
    return "\n".join(parts)


def recipients(env):
    raw = ",".join([env.get("REPORT_TO", "harvey@gershonconsulting.com"), env.get("STATS_TO", "")])
    seen, out = set(), []
    for a in (x.strip() for x in raw.split(",")):
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def send(env, subj, text, html_body):
    key = env.get("RESEND_API_KEY")
    if not key:
        print("RESEND_API_KEY missing in .env", file=sys.stderr)
        return False
    body = json.dumps({"from": env.get("REPORT_FROM", "GC Harvey <harvey@gershon.ai>"),
                       "to": recipients(env), "subject": subj, "text": text,
                       "html": html_body}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "gershon-harvey-reports/2"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(r.status, r.read().decode()[:200])
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    env = load_env()
    d = collect(env)
    probs = problems(d)
    ok, _ = verdict(d, probs)
    subj, text, page = subject(d, ok, probs), render_text(d), render_html(d)
    if "--preview" in argv:
        out = ROOT / "data" / "daily_preview.html"
        out.write_text(page)
        print(subj, "\n", out)
        return 0
    return 0 if send(env, subj, text, page) else 1


if __name__ == "__main__":
    sys.exit(main())
