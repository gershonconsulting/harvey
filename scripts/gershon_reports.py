#!/usr/bin/env python3
"""Gershon add-on: email Harvey reports and alerts through Resend.

  gershon_reports.py daily   -> full daily report (always sends)
  gershon_reports.py alerts  -> sends only when something changed that Olivier
                                needs to know (service down, brain not ready,
                                emails waiting for approval, new replies,
                                send errors). State kept in data/gershon_alerts.json.

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
        flag = "❌" if (s["service"] != "active" or not s["brain_ready"] or s["pending_review"]) else "✅"
        send(env, f"{flag} GC Harvey daily report {today}", fmt(s))
        return
    if mode == "test":
        send(env, "✅ GC Harvey reports are connected", "Harvey can email you.\n\n" + fmt(s))
        return
    prev = json.loads(STATE.read_text()) if STATE.exists() else {}
    alerts = []
    if s["service"] != "active" and prev.get("service") == "active":
        alerts.append(f"Harvey service is {s['service']} (was active).")
    if not s["brain_ready"] and prev.get("brain_ready", True):
        alerts.append("Harvey's brain (ChatGPT/Codex login) is NOT READY — Harvey is paused.")
    if s["pending_review"] > prev.get("pending_review", 0):
        alerts.append(f"{s['pending_review']} email(s) are waiting for your approval.")
    if s["replies_total"] > prev.get("replies_total", 0):
        alerts.append(f"{s['replies_total'] - prev.get('replies_total', 0)} new prospect reply(ies).")
    new_err = [e for e in s["errors"] if e not in prev.get("errors", [])]
    if new_err and prev:
        alerts.append(f"{len(new_err)} new send error(s): " + "; ".join(e[2] for e in new_err))
    STATE.write_text(json.dumps({k: s[k] for k in
                                 ("service", "brain_ready", "pending_review", "replies_total", "errors")}))
    if alerts and prev:
        send(env, "❌ GC Harvey: " + alerts[0][:80], "\n".join(alerts) + "\n\n" + fmt(s))


if __name__ == "__main__":
    main()
