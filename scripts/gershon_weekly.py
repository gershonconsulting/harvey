#!/usr/bin/env python3
"""Gershon add-on: the Harvey H01 WEEKLY report (replaces the daily report and daily stats).

Sent every Monday 08:00 Sint Maarten time about the week that just ended (Mon-Sun):

  1. a red header band and the verdict for the week (target = DAILY_TARGET x 7 companies)
  2. four KPI tiles with the change vs the week before (arrow up / down)
  3. charts (PNG images rendered by quickchart.io, so they show in Gmail and on a phone):
       - new companies per day vs the daily target
       - the last 8 weeks: companies found and emails sent
       - the campaign funnel, from company found to interested
       - email quality of the contacts found this week
  4. what needs Olivier (buttons), what is broken, interested leads
  5. the companies found this week with their status

  gershon_weekly.py            -> send the report
  gershon_weekly.py --preview  -> write data/weekly_preview.html, send nothing

Config (/opt/harvey/.env), same keys as gershon_daily.py:
  RESEND_API_KEY, REPORT_FROM, REPORT_TO, STATS_TO, DAILY_TARGET=10, REPORT_UTC_OFFSET=-4
Stdlib only; reads data/harvey.db read-only and never changes Harvey's state.
Only counts (no names, no emails) are sent to quickchart.io to draw the charts.
"""
import html
import json
import os
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("HARVEY_ROOT") or Path(__file__).resolve().parent.parent)
DB = ROOT / "data" / "harvey.db"
GMAIL_STATE = ROOT / "data" / "gmail_drafts.json"
CODEX_AUTH = Path(os.environ.get("CODEX_AUTH", "/home/harvey/.codex/auth.json"))

CAMPAIGN = "H01"
DRAFTS_LINK = "https://mail.google.com/mail/u/olivier@gershonconsulting.net/#drafts"
SALES_LINK = "https://sales.gershon.ai/#harvey"
CHART_BASE = "https://quickchart.io/chart"
RED, DARK, INK, MUTED, LINE, TINT, GREEN, GREEN_TINT, AMBER, GREY = (
    "#FE1B04", "#d11200", "#1d1d1f", "#6b7280", "#e5e7eb", "#fff5f5",
    "#0f8a4b", "#effaf3", "#f59e0b", "#9ca3af")
LEAD_STAGES = ("presenting", "negotiating", "ready_to_meet", "meeting_booked")
SENDABLE = ("verified", "risky")
FONT = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"


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


def collect(env, now=None, probe=True):
    """Everything the report says, as plain data. `probe=False` skips shell calls (tests)."""
    off = float(env.get("REPORT_UTC_OFFSET", "-4"))
    daily = int(env.get("DAILY_TARGET", "10"))
    now = now or datetime.now(timezone.utc)
    today = (now + timedelta(hours=off)).date()
    # the week that just ended: Monday..Sunday before today
    end = today - timedelta(days=today.weekday() + 1)          # last Sunday
    start = end - timedelta(days=6)                             # its Monday
    shift = f"{off:+g} hours"

    def per_day(table, col="created_at", where="1=1"):
        return {r[0]: r[1] for r in q(
            f"SELECT date({col}, ?), COUNT(*) FROM {table} "
            f"WHERE {col} IS NOT NULL AND {col} != '' AND {where} GROUP BY 1", (shift,))}

    series = {
        "companies": per_day("companies"),
        "contacts": per_day("prospects"),
        "written": per_day("outbox", where="step = 1 AND kind = 'sequence'"),
        "sent": per_day("outbox", "sent_at", "status = 'sent' AND step = 1"),
        "replies": per_day("conversations"),
    }

    def total(key, a, b):
        return sum(v for d, v in series[key].items() if a.isoformat() <= d <= b.isoformat())

    days = []
    for i in range(7):
        d = (start + timedelta(days=i)).isoformat()
        days.append({"date": d, **{k: series[k].get(d, 0) for k in series}})

    weeks = []
    for w in range(7, -1, -1):
        a = start - timedelta(weeks=w)
        b = a + timedelta(days=6)
        weeks.append({"start": a.isoformat(), "companies": total("companies", a, b),
                      "sent": total("sent", a, b)})

    pstart, pend = start - timedelta(days=7), end - timedelta(days=7)
    kpi = {k: (total(k, start, end), total(k, pstart, pend)) for k in series}

    rows = q(
        """SELECT c.name, COALESCE(NULLIF(c.website, ''), c.domain, ''), c.location,
                  TRIM(COALESCE(p.first_name, '') || ' ' || COALESCE(p.last_name, '')),
                  COALESCE(p.title, ''), COALESCE(p.email_status, ''),
                  (SELECT COUNT(*) FROM outbox o WHERE o.prospect_id = p.id
                    AND o.status IN ('pending_review', 'approved', 'sent')),
                  (SELECT COUNT(*) FROM outbox o WHERE o.prospect_id = p.id AND o.status = 'sent')
           FROM companies c LEFT JOIN prospects p ON p.company_id = c.id
           WHERE date(c.created_at, ?) BETWEEN ? AND ? ORDER BY c.created_at, c.id""",
        (shift, start.isoformat(), end.isoformat()))
    by_co = {}
    for name, site, loc, person, title, status, drafts, sent in rows:
        key = name or site or "(no name)"
        e = by_co.setdefault(key, {"name": key, "site": site, "location": loc or "",
                                   "contacts": [], "written": False, "sendable": False,
                                   "sent": False})
        if person:
            e["contacts"].append({"name": person, "title": title})
        e["sendable"] = e["sendable"] or status in SENDABLE
        e["written"] = e["written"] or bool(drafts)
        e["sent"] = e["sent"] or bool(sent)

    quality = dict(q(
        """SELECT CASE WHEN email_status IN ('verified', 'risky', 'invalid') THEN email_status
                       WHEN COALESCE(email, '') = '' THEN 'no email' ELSE 'unchecked' END, COUNT(*)
           FROM prospects WHERE date(created_at, ?) BETWEEN ? AND ? GROUP BY 1""",
        (shift, start.isoformat(), end.isoformat())))

    pending = [r[0] for r in q("SELECT id FROM outbox WHERE status = 'pending_review'")]
    tracked = read_json(GMAIL_STATE)
    leads = q(
        """SELECT TRIM(COALESCE(p.first_name, '') || ' ' || COALESCE(p.last_name, '')),
                  COALESCE(NULLIF(p.company, ''), co.name, ''), c.stage
           FROM conversations c LEFT JOIN prospects p ON p.id = c.prospect_id
           LEFT JOIN companies co ON co.id = p.company_id
           WHERE c.intent = 'interested' OR c.stage IN (?, ?, ?, ?)
           ORDER BY c.updated_at DESC LIMIT 10""", LEAD_STAGES)

    status = sh([str(ROOT / ".venv/bin/harvey"), "status", "--check"]) if probe else ""
    funnel = [
        ("Companies found", one("SELECT COUNT(*) FROM companies")),
        ("With a contact", one(
            "SELECT COUNT(DISTINCT company_id) FROM prospects WHERE company_id != ''")),
        ("Email written", one(
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
        "start": start.isoformat(), "end": end.isoformat(), "daily_target": daily,
        "target": daily * 7, "days": days, "weeks": weeks, "kpi": kpi,
        "companies": list(by_co.values()), "quality": quality, "funnel": funnel,
        "drafts_in_gmail": len([p for p in pending if p in tracked]),
        "pending_total": len(pending), "leads": [list(r) for r in leads],
        "service": sh(["systemctl", "is-active", "harvey"]) if probe else "active",
        "loop": sh(["systemctl", "is-active", "harvey-loop"]) if probe else "active",
        "brain_ready": "NOT READY" not in status,
        "paused": str(one("SELECT value FROM settings WHERE key = 'sending_paused'")) in ("1", "true"),
    }


# ── judging ──────────────────────────────────────────────────────────────────
def problems(d):
    out = []
    if d["service"] != "active":
        out.append(("Harvey is not running on the server",
                    "Nothing happens until it is restarted."))
    if d["loop"] != "active":
        out.append(("Prospecting is stopped", "No new companies are being searched for."))
    if not d["brain_ready"]:
        out.append(("Harvey's ChatGPT login has expired",
                    "No research and no new emails until it is signed in again."))
    if d["paused"]:
        out.append(("Sending is paused", "Harvey stopped sending (bounce safety). Emails wait."))
    return out


def verdict(d, probs):
    n, t = d["kpi"]["companies"][0], d["target"]
    hit_days = sum(1 for x in d["days"] if x["companies"] >= d["daily_target"])
    ok = n >= t and not probs
    head = "Target met" if n >= t else "Target missed"
    line = f"{head}: {n} new companies this week (target {t}), daily target hit on {hit_days}/7 days."
    if probs:
        line += f" Main problem: {probs[0][0]}."
    return ok, line


def week_label(d):
    a = datetime.strptime(d["start"], "%Y-%m-%d")
    b = datetime.strptime(d["end"], "%Y-%m-%d")
    left = a.strftime("%d %b") if a.month != b.month else a.strftime("%d")
    return f"{left}–{b.strftime('%d %b %Y')}"


def subject(d, ok):
    k = d["kpi"]
    bits = [f"{k['companies'][0]}/{d['target']} companies", f"{k['sent'][0]} sent",
            f"{k['replies'][0]} replies"]
    if d["leads"]:
        bits.append(f"{len(d['leads'])} interested")
    return f"{'✅' if ok else '❌'} Harvey {CAMPAIGN} weekly · {week_label(d)}: " + " · ".join(bits)


# ── charts (quickchart.io PNG, Chart.js v2 config) ───────────────────────────
def chart_url(cfg, w=560, h=260):
    c = json.dumps(cfg, separators=(",", ":"))
    return (f"{CHART_BASE}?w={w}&h={h}&bkg=white&devicePixelRatio=2&c="
            + urllib.parse.quote(c, safe=""))


def _axes(stacked=False, horizontal=False):
    grid = {"color": "#f0f0f0", "zeroLineColor": LINE}
    val = {"ticks": {"beginAtZero": True, "precision": 0, "fontColor": MUTED}, "gridLines": grid,
           "stacked": stacked}
    cat = {"ticks": {"fontColor": MUTED}, "gridLines": {"display": False}, "stacked": stacked}
    return ({"xAxes": [val], "yAxes": [cat]} if horizontal else {"xAxes": [cat], "yAxes": [val]})


def chart_daily(d):
    labels = [datetime.strptime(x["date"], "%Y-%m-%d").strftime("%a %d") for x in d["days"]]
    vals = [x["companies"] for x in d["days"]]
    return chart_url({
        "type": "bar",
        "data": {"labels": labels, "datasets": [
            {"type": "line", "label": f"Target ({d['daily_target']}/day)",
             "data": [d["daily_target"]] * 7, "borderColor": INK, "borderDash": [6, 4],
             "borderWidth": 2, "fill": False, "pointRadius": 0,
             "datalabels": {"display": False}},
            {"label": "New companies", "data": vals,
             "backgroundColor": [GREEN if v >= d["daily_target"] else RED for v in vals]},
        ]},
        "options": {"legend": {"position": "bottom", "labels": {"fontColor": MUTED}},
                    "layout": {"padding": {"top": 22}}, "scales": _axes(),
                    "plugins": {"datalabels": {"anchor": "end", "align": "top",
                                               "color": INK, "font": {"weight": "bold"}}}},
    })


def chart_trend(d):
    labels = [datetime.strptime(w["start"], "%Y-%m-%d").strftime("%d %b") for w in d["weeks"]]
    return chart_url({
        "type": "bar",
        "data": {"labels": labels, "datasets": [
            {"type": "line", "label": "First emails sent", "data": [w["sent"] for w in d["weeks"]],
             "borderColor": INK, "backgroundColor": INK, "fill": False, "borderWidth": 3,
             "pointRadius": 4, "lineTension": 0.3},
            {"label": "Companies found", "data": [w["companies"] for w in d["weeks"]],
             "backgroundColor": [TINT] * 7 + [RED], "borderColor": RED, "borderWidth": 1},
        ]},
        "options": {"legend": {"position": "bottom", "labels": {"fontColor": MUTED}},
                    "scales": _axes(), "plugins": {"datalabels": {"display": False}}},
    })


def chart_funnel(d):
    shades = ["#FE1B04", "#fe3a26", "#fe5a49", "#ff7a6b", "#ff998d", GREEN]
    return chart_url({
        "type": "horizontalBar",
        "data": {"labels": [k for k, _ in d["funnel"]],
                 "datasets": [{"data": [v for _, v in d["funnel"]], "backgroundColor": shades}]},
        "options": {"legend": {"display": False}, "scales": _axes(horizontal=True),
                    "layout": {"padding": {"right": 30}},
                    "plugins": {"datalabels": {"anchor": "end", "align": "right",
                                               "color": INK, "font": {"weight": "bold"}}}},
    }, h=280)


def chart_quality(d):
    order = [("verified", GREEN, "Verified"), ("risky", AMBER, "Risky"),
             ("invalid", RED, "Invalid"), ("unchecked", GREY, "Not checked"),
             ("no email", LINE, "No email")]
    items = [(lbl, d["quality"].get(k, 0), col) for k, col, lbl in order if d["quality"].get(k)]
    if not items:
        return ""
    return chart_url({
        "type": "doughnut",
        "data": {"labels": [i[0] for i in items],
                 "datasets": [{"data": [i[1] for i in items],
                               "backgroundColor": [i[2] for i in items]}]},
        "options": {"cutoutPercentage": 60,
                    "legend": {"position": "right", "labels": {"fontColor": INK}},
                    "plugins": {"datalabels": {"color": "#fff", "font": {"weight": "bold"}},
                                "doughnutlabel": {"labels": [
                                    {"text": str(sum(i[1] for i in items)),
                                     "font": {"size": 26, "weight": "bold"}},
                                    {"text": "contacts"}]}}},
    }, w=520, h=240)


# ── writing ──────────────────────────────────────────────────────────────────
def esc(s):
    return html.escape(str(s or ""))


def delta(now_v, prev_v):
    if now_v == prev_v:
        return MUTED, "= same as last week"
    if prev_v == 0:
        return (GREEN, f"▲ +{now_v} vs last week")
    pct = round(100 * (now_v - prev_v) / prev_v)
    return (GREEN, f"▲ +{pct}% vs last week") if pct > 0 else (RED, f"▼ {pct}% vs last week")


def for_you(d):
    out = []
    if d["drafts_in_gmail"]:
        out.append((f"Review {d['drafts_in_gmail']} email(s) in Gmail Drafts",
                    "Send the ones you like from Gmail.", DRAFTS_LINK, "Open Drafts"))
    if d["paused"]:
        out.append(("Decide: resume sending?", "Harvey paused itself after bounces. Tell Claude \"resume Harvey\" and it "
                    "restarts once the bad addresses are removed.", SALES_LINK, "See status"))
    if d["leads"]:
        out.append((f"Answer {len(d['leads'])} interested prospect(s)",
                    ", ".join(f"{n or '?'} ({c or '?'})" for n, c, *_ in d["leads"][:5]),
                    SALES_LINK, "Open leads"))
    return out


def render_text(d):
    probs = problems(d)
    ok, line = verdict(d, probs)
    k = d["kpi"]
    out = [f"Harvey {CAMPAIGN} weekly report, {week_label(d)}", "", line, ""]
    for key, label in (("companies", "New companies"), ("contacts", "New contacts"),
                       ("sent", "First emails sent"), ("replies", "Replies")):
        out.append(f"- {label}: {k[key][0]} ({delta(*k[key])[1]})")
    out += ["", "PER DAY (companies / emails sent)"]
    out += [f"- {x['date']}: {x['companies']} / {x['sent']}" for x in d["days"]]
    todo = for_you(d)
    out += ["", "FOR YOU"] + ([f"- {t}: {s} {u}" for t, s, u, _ in todo] or ["- Nothing."])
    if probs:
        out += ["", "BROKEN"] + [f"- {h}. {w}" for h, w in probs]
    out += ["", f"COMPANIES THIS WEEK ({len(d['companies'])})"]
    out += [f"- {c['name']}" + (f" ({c['site']})" if c["site"] else "") for c in d["companies"]] or ["- None."]
    out += ["", "CAMPAIGN SO FAR"] + [f"- {a}: {b}" for a, b in d["funnel"]]
    out += ["", f"Everything on {SALES_LINK}"]
    return "\n".join(out)


def render_html(d):
    probs = problems(d)
    ok, line = verdict(d, probs)
    k = d["kpi"]
    td = f"padding:9px 10px;border-bottom:1px solid {LINE};font-size:14px;color:{INK}"
    th = (f"padding:8px 10px;border-bottom:2px solid {LINE};font-size:11px;color:{MUTED};"
          "text-transform:uppercase;letter-spacing:.05em;text-align:left")

    def h2(t, sub=""):
        s = f'<div style="font-size:13px;color:{MUTED};font-weight:400;margin-top:2px">{esc(sub)}</div>' if sub else ""
        return (f'<tr><td style="padding:30px 28px 10px"><div style="font-size:17px;font-weight:700;'
                f'color:{INK};border-left:4px solid {RED};padding-left:10px">{esc(t)}{s}</div></td></tr>')

    def img(url, alt):
        return (f'<tr><td style="padding:0 22px"><img src="{esc(url)}" alt="{esc(alt)}" width="596" '
                f'style="width:100%;max-width:596px;height:auto;display:block;border:1px solid {LINE};'
                f'border-radius:12px"></td></tr>')

    def tile(n, label, key, color):
        c, txt = delta(*k[key])
        return (f'<td width="25%" style="padding:16px 6px;text-align:center;background:#ffffff;'
                f'border:1px solid {LINE};border-top:4px solid {color};border-radius:12px">'
                f'<div style="font-size:30px;font-weight:800;color:{INK};line-height:1.1">{n}</div>'
                f'<div style="font-size:12px;color:{MUTED};margin-top:4px">{esc(label)}</div>'
                f'<div style="font-size:11px;color:{c};font-weight:600;margin-top:6px">{esc(txt)}</div></td>')

    pct = min(100, round(100 * k["companies"][0] / max(d["target"], 1)))
    parts = [
        f'<div style="background:#f5f5f7;padding:24px 0;font-family:{FONT};color:{INK}">',
        '<table role="presentation" width="640" align="center" cellpadding="0" cellspacing="0" '
        'style="max-width:640px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden">',
        # header band
        f'<tr><td style="background:{RED};background-image:linear-gradient(135deg,{RED},{DARK});'
        f'padding:26px 28px 22px;color:#ffffff">'
        f'<div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;opacity:.85">'
        f'Gershon Consulting · Campaign {CAMPAIGN}</div>'
        f'<div style="font-size:26px;font-weight:800;margin-top:6px">Harvey weekly report</div>'
        f'<div style="font-size:15px;opacity:.92;margin-top:2px">{esc(week_label(d))}</div></td></tr>',
        # verdict + progress bar
        f'<tr><td style="padding:20px 28px 0"><div style="padding:16px 18px;border-radius:12px;'
        f'background:{GREEN_TINT if ok else TINT};border-left:5px solid {GREEN if ok else RED};'
        f'font-size:16px;line-height:1.45;font-weight:600">{"✅" if ok else "❌"} {esc(line)}'
        f'<div style="margin-top:12px;background:#ffffff;border-radius:999px;height:12px;'
        f'border:1px solid {LINE}"><div style="width:{pct}%;height:12px;border-radius:999px;'
        f'background:{GREEN if pct >= 100 else RED}"></div></div>'
        f'<div style="font-size:12px;color:{MUTED};font-weight:400;margin-top:6px">'
        f'{pct}% of the weekly target ({k["companies"][0]}/{d["target"]} companies)</div>'
        f'</div></td></tr>',
        # KPI tiles
        '<tr><td style="padding:16px 22px 0"><table role="presentation" width="100%" cellspacing="6" '
        'cellpadding="0"><tr>',
        tile(k["companies"][0], "new companies", "companies", RED),
        tile(k["contacts"][0], "new contacts", "contacts", AMBER),
        tile(k["sent"][0], "first emails sent", "sent", INK),
        tile(k["replies"][0], "replies", "replies", GREEN),
        "</tr></table></td></tr>",
    ]

    todo = for_you(d)
    parts.append(h2("For you"))
    if todo:
        for t, s, u, btn in todo:
            parts.append(
                f'<tr><td style="padding:4px 28px"><table role="presentation" width="100%" '
                f'style="background:#fafafa;border:1px solid {LINE};border-radius:12px"><tr>'
                f'<td style="padding:12px 14px;font-size:15px;line-height:1.45"><b>{esc(t)}</b><br>'
                f'<span style="color:{MUTED};font-size:13px">{esc(s)}</span></td>'
                f'<td align="right" width="130" style="padding:12px 14px"><a href="{esc(u)}" '
                f'style="background:{RED};color:#fff;text-decoration:none;font-size:13px;font-weight:700;'
                f'padding:10px 16px;border-radius:999px;display:inline-block">{esc(btn)}</a></td>'
                f'</tr></table></td></tr>')
    else:
        parts.append(f'<tr><td style="padding:4px 28px;font-size:15px;color:{MUTED}">'
                     'Nothing waiting for you.</td></tr>')

    if probs:
        parts.append(h2("Needs fixing"))
        for hd, why in probs:
            parts.append(f'<tr><td style="padding:4px 28px"><div style="background:{TINT};'
                         f'border-radius:10px;padding:10px 14px;font-size:14px;line-height:1.45">'
                         f'<b style="color:{DARK}">⚠ {esc(hd)}</b><br>'
                         f'<span style="color:{MUTED};font-size:13px">{esc(why)}</span></div></td></tr>')

    parts.append(h2("New companies per day", f"Green = daily target of {d['daily_target']} reached"))
    parts.append(img(chart_daily(d), "New companies per day vs target"))
    parts.append(h2("Last 8 weeks", "Companies found (bars) and first emails sent (line)"))
    parts.append(img(chart_trend(d), "Companies found and emails sent, last 8 weeks"))
    parts.append(h2("Campaign funnel", "Since the start of H01"))
    parts.append(img(chart_funnel(d), "Campaign funnel"))
    q_url = chart_quality(d)
    if q_url:
        parts.append(h2("Email quality this week", "Contacts found this week, by email check result"))
        parts.append(img(q_url, "Email quality of this week's contacts"))

    if d["leads"]:
        parts.append(h2("Interested leads"))
        for n, c, stage in d["leads"]:
            parts.append(f'<tr><td style="padding:3px 28px;font-size:14px"><span style="color:{GREEN}">●'
                         f'</span> <b>{esc(n or "?")}</b> · {esc(c or "?")} '
                         f'<span style="color:{MUTED}">({esc((stage or "").replace("_", " "))})</span></td></tr>')

    parts.append(h2(f"Companies found this week ({len(d['companies'])})"))
    if d["companies"]:
        parts.append('<tr><td style="padding:0 18px"><table role="presentation" width="100%" '
                     f'cellspacing="0" cellpadding="0"><tr><th style="{th}">Company</th>'
                     f'<th style="{th}">Contacts</th><th style="{th}">Status</th></tr>')
        for c in d["companies"][:40]:
            site = c["site"] if c["site"].startswith("http") else ("https://" + c["site"] if c["site"] else "")
            name = (f'<a href="{esc(site)}" style="color:{INK};font-weight:600;text-decoration:none">'
                    f'{esc(c["name"])}</a>' if site else f"<b>{esc(c['name'])}</b>")
            who = "<br>".join(esc(p["name"]) + (f' <span style="color:{MUTED};font-size:12px">'
                                                f'{esc(p["title"])}</span>' if p["title"] else "")
                              for p in c["contacts"][:2]) or f'<span style="color:{MUTED}">—</span>'
            fg, bg, label = ((GREEN, GREEN_TINT, "Sent") if c["sent"] else
                             (DARK, TINT, "In Drafts") if c["written"] else
                             (INK, "#f3f4f6", "To write") if c["sendable"] else
                             (MUTED, "#f9fafb", "No email"))
            parts.append(
                f'<tr><td style="{td}">{name}<br><span style="font-size:12px;color:{MUTED}">'
                f'{esc(c["location"])}</span></td><td style="{td}">{who}</td>'
                f'<td style="{td}"><span style="background:{bg};color:{fg};font-size:12px;font-weight:700;'
                f'padding:4px 10px;border-radius:999px;white-space:nowrap">{label}</span></td></tr>')
        parts.append("</table></td></tr>")
        if len(d["companies"]) > 40:
            parts.append(f'<tr><td style="padding:6px 28px;font-size:12px;color:{MUTED}">'
                         f'+ {len(d["companies"]) - 40} more on sales.gershon.ai</td></tr>')
    else:
        parts.append(f'<tr><td style="padding:4px 28px;font-size:15px;color:{MUTED}">None.</td></tr>')

    parts.append(
        f'<tr><td style="padding:28px 28px 26px;text-align:center"><a href="{SALES_LINK}" '
        f'style="background:{INK};color:#fff;text-decoration:none;font-size:14px;font-weight:700;'
        f'padding:12px 22px;border-radius:999px;display:inline-block">Open Harvey on sales.gershon.ai</a>'
        f'<div style="font-size:12px;color:{MUTED};margin-top:14px">Weekly report · every Monday '
        f'08:00 Sint Maarten time · week = Monday to Sunday</div></td></tr>')
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
                                          "User-Agent": "gershon-harvey-reports/3"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(r.status, r.read().decode()[:200])
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    env = load_env()
    d = collect(env)
    ok, _ = verdict(d, problems(d))
    subj, text, page = subject(d, ok), render_text(d), render_html(d)
    if "--preview" in argv:
        out = ROOT / "data" / "weekly_preview.html"
        out.write_text(page)
        print(subj, "\n", out)
        return 0
    return 0 if send(env, subj, text, page) else 1


if __name__ == "__main__":
    sys.exit(main())
