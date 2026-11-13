#!/usr/bin/env python3
"""Gershon add-on: copy Harvey's leads into Olivier's Streak "GC Campaigns" pipeline.

Every company Harvey finds (with at least one usable prospect) becomes one Streak box
in GC Campaigns, flagged Origin = "H01" (Campaign H01), stage "1. Contacted", with the
prospects (name, title, email, LinkedIn) and where Harvey found them in the box notes.
When a prospect replies with interest, the box moves to "2. Responded".

  gershon_streak.py sync     -> one sync pass (also run by gershon_reports.py alerts
                                every 15 minutes when STREAK_API_KEY is set)
  gershon_streak.py dry-run  -> prints what a sync would do, changes nothing

Dedupe: a company already synced (data/gershon_streak.json) is never created twice; a
box with the same company name already in the pipeline is reused. If that box belongs
to another campaign (Origin is not H01) it is left as it is and only gets a comment.

Config (in /opt/harvey/.env):
  STREAK_API_KEY=...                 Streak API key (Streak > Settings > Integrations)
  STREAK_PIPELINE_KEY=...            optional, defaults to GC Campaigns
  STREAK_ORIGIN_FIELD / STREAK_ORIGIN_VALUE / STREAK_STAGE_NEW / STREAK_STAGE_REPLIED
                                     optional overrides (defaults below)
Stdlib only; reads data/harvey.db read-only, never sends any email to anyone.
"""
import base64
import hashlib
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "harvey.db"
STATE = ROOT / "data" / "gershon_streak.json"
API = "https://api.streak.com/api"

DEFAULTS = {
    # GC Campaigns pipeline (gershonconsulting.com team)
    "STREAK_PIPELINE_KEY": "agxzfm1haWxmb29nYWVyPAsSDE9yZ2FuaXphdGlvbiIVZ2Vyc2hvbmNvbnN1bHRpbmcuY29tDAsSCFdvcmtmbG93GICAlZSi8K8JDA",
    "STREAK_ORIGIN_FIELD": "1002",   # "Origin" dropdown
    "STREAK_ORIGIN_VALUE": "9213",   # option "H01"
    "STREAK_STAGE_NEW": "5001",      # "1. Contacted"
    "STREAK_STAGE_REPLIED": "5002",  # "2. Responded"
}
CAMPAIGN = "H01"
SKIP_STATUSES = ("unfit", "opted_out", "bounced", "do_not_contact", "invalid")
REPLIED_STAGES = ("presenting", "negotiating", "ready_to_meet", "meeting_booked")


def q(sql, args=()):
    if not DB.exists():
        return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        print(f"db read failed: {e}", file=sys.stderr)
        return []
    finally:
        con.close()


def norm(name):
    """Company name key for dedupe: lower case, no legal suffix, no punctuation."""
    n = (name or "").lower()
    n = re.sub(r"\b(the|ltd|limited|llc|inc|gmbh|bv|b\.v|plc|sa|sas|srl)\b\.?", " ", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def leads():
    """Companies with usable prospects, grouped, plus whether anyone replied with interest."""
    ph = ",".join("?" * len(SKIP_STATUSES))
    rows = q(f"""SELECT COALESCE(NULLIF(p.company_id, ''), 'name:' || p.company),
                        COALESCE(NULLIF(co.name, ''), p.company, ''),
                        COALESCE(co.website, ''), COALESCE(co.domain, ''),
                        COALESCE(co.location, ''), COALESCE(co.industry, p.industry, ''),
                        COALESCE(NULLIF(co.source, ''), p.source, ''),
                        COALESCE(NULLIF(co.source_url, ''), p.source_url, ''),
                        p.id, p.first_name, p.last_name, p.title, p.email,
                        p.linkedin_url, p.status, p.created_at
                 FROM prospects p
                 LEFT JOIN companies co ON co.id = p.company_id
                 WHERE COALESCE(p.status, '') NOT IN ({ph})
                 ORDER BY p.created_at, p.id""", SKIP_STATUSES)
    replied = {r[0] for r in q(
        "SELECT prospect_id FROM conversations WHERE intent = 'interested' OR stage IN "
        f"({','.join('?' * len(REPLIED_STAGES))})", REPLIED_STAGES)}
    out = {}
    for r in rows:
        cid, name, site, domain, loc, ind, src, src_url = r[:8]
        pid, first, last, title, email, li, status, created = r[8:]
        if not (name or "").strip():
            continue
        c = out.setdefault(cid, {"id": cid, "name": name.strip(), "website": site or domain,
                                 "location": loc, "industry": ind, "source": src,
                                 "source_url": src_url, "found": created, "people": [],
                                 "replied": False})
        c["people"].append({"name": f"{first or ''} {last or ''}".strip(), "title": title or "",
                            "email": email or "", "linkedin": li or "", "status": status or ""})
        c["replied"] = c["replied"] or pid in replied
    return list(out.values())


def notes_for(c):
    lines = [f"Campaign {CAMPAIGN} - GC Harvey (autonomous cold-email agent)",
             f"Found by Harvey: {str(c['found'] or '')[:10] or '-'}"
             + (f" via {c['source']}" if c["source"] else ""),
             ]
    if c["source_url"]:
        lines.append(f"Source: {c['source_url']}")
    if c["website"]:
        lines.append(f"Website: {c['website']}")
    if c["location"] or c["industry"]:
        lines.append(" | ".join(x for x in (c["location"], c["industry"]) if x))
    lines.append("Status: " + ("REPLIED WITH INTEREST - follow up" if c["replied"]
                               else "not contacted yet (Harvey's emails wait for approval)"))
    lines.append("")
    lines.append("People:")
    for p in c["people"]:
        bits = [p["name"] or "-", p["title"], p["email"], p["linkedin"],
                f"Harvey status {p['status']}" if p["status"] else ""]
        lines.append("- " + " | ".join(b for b in bits if b))
    return "\n".join(lines)


class Streak:
    def __init__(self, key, dry=False):
        self.auth = "Basic " + base64.b64encode(f"{key}:".encode()).decode()
        self.dry = dry

    def call(self, method, path, body=None):
        if self.dry and method != "GET":
            print(f"DRY {method} {path} {json.dumps(body)[:300] if body else ''}")
            return {"key": f"dry:{path}"}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": self.auth, "Content-Type": "application/json",
            "User-Agent": "gershon-harvey-streak/1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw else {}

    def boxes(self, pipeline):
        return self.call("GET", f"/v1/pipelines/{urllib.parse.quote(pipeline)}/boxes") or []

    def create(self, pipeline, name, stage):
        return self.call("POST", f"/v2/pipelines/{urllib.parse.quote(pipeline)}/boxes",
                         {"name": name, "stageKey": stage})

    def update(self, box, **fields):
        return self.call("POST", f"/v1/boxes/{urllib.parse.quote(box)}", fields)

    def set_field(self, box, field, value):
        return self.call("POST", f"/v1/boxes/{urllib.parse.quote(box)}/fields/{field}",
                         {"value": value})

    def comment(self, box, text):
        return self.call("POST", f"/v2/boxes/{urllib.parse.quote(box)}/comments",
                         {"message": text})


def box_key(b):
    return b.get("boxKey") or b.get("key") or ""


def sync(env, dry=False):
    key = env.get("STREAK_API_KEY")
    if not key:
        print("STREAK_API_KEY missing in .env", file=sys.stderr)
        return {"error": "no key"}
    cfg = {k: env.get(k) or v for k, v in DEFAULTS.items()}
    pipeline = cfg["STREAK_PIPELINE_KEY"]
    api = Streak(key, dry=dry)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    existing = None
    done = {"created": 0, "adopted": 0, "updated": 0, "responded": 0}
    for c in leads():
        notes = notes_for(c)
        sig = hashlib.sha256(notes.encode()).hexdigest()[:16]
        st = state.get(c["id"])
        if st is None:
            if existing is None:
                existing = {}
                for b in api.boxes(pipeline):
                    existing.setdefault(norm(b.get("name")), b)
            match = existing.get(norm(c["name"]))
            if match:
                ours = str((match.get("fields") or {}).get(cfg["STREAK_ORIGIN_FIELD"])) \
                    == cfg["STREAK_ORIGIN_VALUE"]
                st = {"box": box_key(match), "ours": ours, "sig": "", "replied": False}
                if not ours:
                    api.comment(st["box"], f"Also found by GC Harvey (Campaign {CAMPAIGN}).\n\n"
                                + notes)
                    st["sig"] = sig
                done["adopted"] += 1
            else:
                b = api.create(pipeline, c["name"], cfg["STREAK_STAGE_NEW"])
                st = {"box": box_key(b), "ours": True, "sig": "", "replied": False}
                api.set_field(st["box"], cfg["STREAK_ORIGIN_FIELD"], cfg["STREAK_ORIGIN_VALUE"])
                existing[norm(c["name"])] = {"boxKey": st["box"], "name": c["name"]}
                done["created"] += 1
            state[c["id"]] = st
        if st["ours"] and st["sig"] != sig:
            api.update(st["box"], notes=notes)
            st["sig"] = sig
            done["updated"] += 1
        if c["replied"] and not st.get("replied"):
            if st["ours"]:
                api.update(st["box"], stageKey=cfg["STREAK_STAGE_REPLIED"])
            else:
                api.comment(st["box"], f"Campaign {CAMPAIGN}: a prospect replied with interest "
                                       "to Harvey. Follow up.")
            st["replied"] = True
            done["responded"] += 1
        if not dry:
            STATE.write_text(json.dumps(state, indent=1))
    print(json.dumps(done))
    return done


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gershon_reports import load_env
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    sync(load_env(), dry=(mode == "dry-run"))


if __name__ == "__main__":
    main()
