#!/usr/bin/env python3
"""Gershon add-on: mirror Harvey's campaign (GC Campaign H01) into sales.gershon.ai.

  gershon_sales_sync.py sync     -> push a full snapshot (companies, prospects, outbox,
                                    conversations, server health) to SALES_URL, then apply
                                    the follow-up actions decided in the app and confirm them
  gershon_sales_sync.py dry-run  -> print what would be pushed (counts only), change nothing

Actions coming back from the app, applied with Harvey's own state functions so every
safety rule stays in force (approval only from pending_review, edits never approve,
the Sender still runs its pre-send gate, daily cap and quiet hours):
  edit      -> edit_outbox_draft(id, subject, body)        (pending_review only)
  approve   -> approve_outbox(id)                           (one email, never all)
  reject    -> reject_outbox_item(id)                       (pending_review/approved only)
  followup  -> add_outbox_item(kind="reply", status="pending_review")  — a NEW email that
               still waits for an explicit approve; threaded on the last email sent.
Nothing is sent by this script. Applied action ids are remembered in
data/gershon_sales_actions.json so a lost confirmation never creates a follow-up twice.

Config (/opt/harvey/.env): SALES_INGEST_TOKEN=<random>   SALES_URL=https://sales.gershon.ai
"""
import asyncio
import json
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "harvey.db"
DONE = ROOT / "data" / "gershon_sales_actions.json"
UA = "harvey-gershon-sales-sync/1.0"


def load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def rows(sql, args=()):
    if not DB.exists():
        return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _cols(table):
    return {r["name"] for r in rows(f"PRAGMA table_info({table})")}


def snapshot():
    ccols = _cols("companies")
    sig = ", signals_json AS signals" if "signals_json" in ccols else ""
    companies = rows(
        "SELECT id, name, domain, website, description, industry, company_size, location, "
        f"source, notes, created_at{sig} FROM companies")
    prospects = rows(
        "SELECT id, company_id, first_name, last_name, title, email, email_verified, linkedin_url, "
        "status, score, personalization_notes, source_url, created_at, updated_at FROM prospects")
    outbox = rows(
        "SELECT id, prospect_id, kind, step, to_email, subject, body, status, send_at, sent_at, "
        "error, created_at, updated_at FROM outbox")
    conversations = rows(
        "SELECT id, prospect_id, intent, stage, status, thread_json, updated_at FROM conversations")

    def sh(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""
    server = {
        "service": sh(["systemctl", "is-active", "harvey-loop"]) or sh(["systemctl", "is-active", "harvey"]),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sending_paused": next((r["value"] for r in rows("SELECT value FROM settings WHERE key = 'sending_paused'")), "") or "",
    }
    return {"full": True, "server": server, "companies": companies, "prospects": prospects,
            "outbox": outbox, "conversations": conversations}


def post(url, token, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload, default=str).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def _done():
    try:
        return set(json.loads(DONE.read_text()))
    except Exception:  # noqa: BLE001
        return set()


async def apply_actions(actions):
    from harvey.state import StateManager
    state = StateManager(str(DB))
    done = _done()
    results = []
    for a in actions:
        aid, typ, oid = int(a["id"]), a.get("type"), a.get("outbox_id") or ""
        if aid in done:
            results.append({"id": aid, "ok": True, "detail": "already applied"})
            continue
        try:
            if typ == "edit":
                n = await state.edit_outbox_draft(oid, a.get("subject") or "", a.get("body") or "")
                ok, detail = bool(n), "edited" if n else "not edited: email no longer waiting for approval"
            elif typ == "approve":
                n = await state.approve_outbox(oid) if oid else 0  # never approve-all
                ok, detail = bool(n), "approved — Harvey sends it at its next send slot" if n else "not approved: email no longer waiting"
            elif typ == "reject":
                n = await state.reject_outbox_item(oid)
                ok, detail = bool(n), "rejected" if n else "not rejected: already sent or closed"
            elif typ == "followup":
                ok, detail = await _followup(state, a)
            else:
                ok, detail = False, f"unknown action {typ}"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"error: {e}"[:300]
        if ok:
            done.add(aid)
        results.append({"id": aid, "ok": ok, "detail": detail})
    DONE.write_text(json.dumps(sorted(done)))
    return results


async def _followup(state, a):
    pid = a.get("prospect_id") or ""
    p = rows("SELECT email FROM prospects WHERE id = ?", (pid,))
    if not p or not p[0]["email"]:
        return False, "lead has no email address"
    last = rows("SELECT campaign_id, conversation_id, message_id, thread_ref FROM outbox WHERE prospect_id = ? "
                "AND status = 'sent' ORDER BY sent_at DESC LIMIT 1", (pid,))
    conv = rows("SELECT id FROM conversations WHERE prospect_id = ? ORDER BY updated_at DESC LIMIT 1", (pid,))
    last = last[0] if last else {}
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=p[0]["email"], subject=a.get("subject") or "", body=a.get("body") or "",
        send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), status="pending_review",
        campaign_id=last.get("campaign_id", ""), conversation_id=(conv[0]["id"] if conv else last.get("conversation_id", "")),
        kind="reply", thread_ref=last.get("thread_ref", ""), in_reply_to=last.get("message_id", ""),
    )
    return bool(item), "queued, waiting for your approval" if item else "not queued"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "sync"
    env = load_env()
    snap = snapshot()
    counts = {k: len(v) for k, v in snap.items() if isinstance(v, list)}
    if cmd == "dry-run":
        print(json.dumps({"would_push": counts, "server": snap["server"]}))
        return 0
    token, base = env.get("SALES_INGEST_TOKEN"), env.get("SALES_URL", "https://sales.gershon.ai").rstrip("/")
    if not token:
        print("SALES_INGEST_TOKEN missing in .env — nothing pushed")
        return 1
    res = post(base + "/api/harvey/ingest", token, snap)
    actions = res.get("actions") or []
    results = asyncio.run(apply_actions(actions)) if actions else []
    if results:
        post(base + "/api/harvey/ack", token, {"results": results})
        # push again so the app shows the result straight away
        post(base + "/api/harvey/ingest", token, snapshot())
    print(json.dumps({"pushed": counts, "actions": len(actions), "applied": sum(r["ok"] for r in results)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
