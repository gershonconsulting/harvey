"""Gershon add-on: Gmail Drafts is the place to review Harvey's emails.

Every email Harvey writes that waits for approval is put in the Drafts folder
of the sending mailbox (olivier@gershonconsulting.net), with the real
signature and the BCC to harvey@. This script NEVER sends anything itself.

Two-way, every run (systemd timer, every 10 minutes):
  * new email waiting for approval        -> new Gmail draft
    (first emails and replies at once; a follow-up once the email before it
    went out and its planned wait is over, in the same Gmail thread)
  * you SEND the draft from Gmail         -> Harvey records it as sent, so the
    lead is "contacted", follow-ups are scheduled, replies are tracked
  * you DELETE the draft in Gmail         -> nothing is rejected; that version
    is not put back (a rewritten email comes back as a new draft)
  * rejected / edited / approved elsewhere (sales.gershon.ai, dashboard)
                                          -> the Gmail draft is removed or
                                             replaced, so it can't go out twice
  * new drafts                            -> ONE short email to REPORT_TO
    saying how many are in Gmail Drafts, with the link.

State: data/gmail_drafts.json  {outbox_id: {"draft": id, "thread": id, "hash": h}}
The older data/gmail_drafts_done.json (ids only) is taken over on first run:
those drafts are found again in Gmail by recipient + subject.

  gershon_gmail_drafts.py            -> one sync run
  gershon_gmail_drafts.py --dry-run  -> print what it would do, change nothing
"""
import asyncio, hashlib, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("HARVEY_ROOT", "/opt/harvey"))
STATE_FILE = ROOT / "data" / "gmail_drafts.json"
DISMISSED_FILE = ROOT / "data" / "gmail_drafts_dismissed.json"  # {outbox_id: content hash}
FORMAT_FILE = ROOT / "data" / "gershon_h01_format.json"
OLD_DONE = ROOT / "data" / "gmail_drafts_done.json"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAILBOX = "olivier@gershonconsulting.net"
DRAFTS_LINK = f"https://mail.google.com/mail/u/{MAILBOX}/#drafts"
MIN_GAP = timedelta(days=2)  # never offer a follow-up sooner than this after the last send


def content_hash(subject, body):
    return hashlib.sha256(f"{subject}\n\x00\n{body}".encode()).hexdigest()[:16]


def decide(row, entry, draft_alive, sent_msg):
    """What to do with one tracked outbox email.

    row: the outbox row now (None if gone). entry: what we stored. draft_alive:
    the Gmail draft still exists. sent_msg: a SENT message found in its Gmail
    thread (or None). Returns one of:
      keep | mark_sent | dismiss | delete_draft | replace_draft | forget
    """
    status = (row or {}).get("status")
    if status == "pending_review":
        if draft_alive:
            if entry.get("hash") and entry["hash"] != content_hash(row["subject"], row["body"]):
                return "replace_draft"  # edited in sales.gershon.ai / dashboard
            return "keep"
        return "mark_sent" if sent_msg else "dismiss"
    # approved, sent by Harvey, rejected, cancelled, or the row is gone
    if draft_alive:
        return "delete_draft"
    if sent_msg and status in ("approved",):
        return "mark_sent"  # sent from Gmail just as it was approved elsewhere
    return "forget"


def follow_up_ready(row, prev, delay_days, now):
    """A sequence follow-up becomes a draft once the email before it was sent
    and its planned wait is over."""
    if not prev or prev.get("status") != "sent" or not prev.get("sent_at"):
        return False
    try:
        sent_at = datetime.fromisoformat(str(prev["sent_at"]))
    except ValueError:
        return False
    gap = timedelta(days=delay_days) if delay_days is not None else MIN_GAP
    return now >= sent_at + max(MIN_GAP, gap)


class Gmail:
    def __init__(self, provider):
        self.p = provider

    async def call(self, method, path, **kw):
        import httpx
        token = await self.p._token()
        if not token:
            raise RuntimeError("Gmail token unavailable")
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.status_code == 404:
            return 404, None
        if r.status_code >= 400:
            raise RuntimeError(f"Gmail {method} {path}: {r.status_code} {r.text[:200]}")
        return r.status_code, (r.json() if r.content else None)

    async def draft_alive(self, draft_id):
        code, _ = await self.call("GET", f"/drafts/{draft_id}", params={"format": "minimal"})
        return code != 404

    async def delete_draft(self, draft_id):
        await self.call("DELETE", f"/drafts/{draft_id}")

    async def sent_in_thread(self, thread_id, to_email, after_ms=0):
        """The SENT message to `to_email` in that thread, if any."""
        if not thread_id:
            return None
        code, t = await self.call("GET", f"/threads/{thread_id}", params={
            "format": "metadata", "metadataHeaders": ["To", "Message-ID", "Subject"]})
        if code == 404 or not t:
            return None
        for m in t.get("messages", []):
            if "SENT" not in m.get("labelIds", []) or "DRAFT" in m.get("labelIds", []):
                continue
            if int(m.get("internalDate", 0)) < after_ms:
                continue
            h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
            if to_email.lower() in h.get("to", "").lower():
                return {"id": m["id"], "message_id": h.get("message-id", ""),
                        "thread": m.get("threadId", thread_id),
                        "sent_at": datetime.fromtimestamp(int(m["internalDate"]) / 1000, timezone.utc)
                        .replace(tzinfo=None).isoformat()}
        return None

    async def all_drafts(self):
        out, token = [], None
        while True:
            params = {"maxResults": 500}
            if token:
                params["pageToken"] = token
            _, d = await self.call("GET", "/drafts", params=params)
            out += (d or {}).get("drafts", [])
            token = (d or {}).get("nextPageToken")
            if not token:
                return out

    async def draft_headers(self, draft_id):
        _, d = await self.call("GET", f"/drafts/{draft_id}", params={"format": "metadata"})
        m = (d or {}).get("message", {})
        h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
        return h.get("to", ""), h.get("subject", ""), m.get("threadId", "")


def make_draft_provider(cfg, env):
    from harvey.integrations.gmail import GmailProvider

    class DraftProvider(GmailProvider):
        """Same message Harvey would send (signature, BCC, threading) — but
        POSTed to /drafts instead of /messages/send."""
        last = {}

        async def _request(self, method, path, *a, **kw):
            if method == "POST" and path == "/messages/send":
                kw.pop("sending", None)
                payload = kw.pop("json")
                data = await super()._request("POST", "/drafts", *a, json={"message": payload}, **kw)
                if data is None:
                    return None
                DraftProvider.last = {"draft": data.get("id", ""),
                                      "thread": (data.get("message") or {}).get("threadId", "")}
                return {"id": "", "threadId": DraftProvider.last["thread"]}
            return await super()._request(method, path, *a, **kw)

    return DraftProvider(cfg, env)


async def main(dry=False):
    os.chdir(ROOT); sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
    from harvey.config import load_config, load_env
    from harvey.state import StateManager
    from harvey.agents.sender import compliance_footer_lines

    cfg, env = load_config(), load_env()
    provider = make_draft_provider(cfg, env)
    gm = Gmail(provider)
    st = StateManager(str(ROOT / "data" / "harvey.db"))
    await st.init_db()
    tracked = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    footer = "\n".join(compliance_footer_lines(cfg))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    log = []

    async def rows(sql, args=()):
        import aiosqlite
        async with st._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                return [dict(r) for r in await cur.fetchall()]

    PENDING_SQL = (
        "SELECT o.*, COALESCE(p.first_name,'') fn, COALESCE(p.last_name,'') ln, COALESCE(p.title,'') title, "
        "COALESCE(p.personalization_notes,'') notes, COALESCE(p.email_status,'') email_status, "
        "COALESCE(p.company_id,'') company_id, "
        "COALESCE(NULLIF(p.company,''), co.name, '') company FROM outbox o "
        "LEFT JOIN prospects p ON p.id = o.prospect_id LEFT JOIN companies co ON co.id = p.company_id "
        "WHERE o.status = 'pending_review' ORDER BY o.created_at")
    pending = await rows(PENDING_SQL)
    dismissed = json.loads(DISMISSED_FILE.read_text()) if DISMISSED_FILE.exists() else {}

    # 1. take over drafts made by the first version of this script
    if not STATE_FILE.exists() and OLD_DONE.exists():
        old = set(json.loads(OLD_DONE.read_text()))
        # any status: drafts of emails rejected since must be found too, so step 2 removes them
        old_rows = [r for r in [await st.get_outbox_item(i) for i in old] if r]
        by_key = {(r["to_email"].lower(), r["subject"].strip()): r["id"] for r in old_rows}
        for d in await gm.all_drafts():
            to, subj, thread = await gm.draft_headers(d["id"])
            oid = next((v for (t, s), v in by_key.items() if t in to.lower() and s == subj.strip()), None)
            if oid and oid not in tracked:
                tracked[oid] = {"draft": d["id"], "thread": thread, "hash": ""}
        log.append(f"took over {len(tracked)} existing Gmail drafts")

    def save():
        if not dry:
            STATE_FILE.write_text(json.dumps(tracked, indent=1))
            DISMISSED_FILE.write_text(json.dumps(dismissed, indent=1))

    # 2. follow what happened to every draft we made
    alive_cache = {}
    for oid, entry in list(tracked.items()):
        row = await st.get_outbox_item(oid)
        if entry["draft"] not in alive_cache:
            alive_cache[entry["draft"]] = await gm.draft_alive(entry["draft"])
        alive = alive_cache[entry["draft"]]
        sent = None if alive else await gm.sent_in_thread(entry.get("thread"), (row or {}).get("to_email", ""))
        action = decide(row, entry, alive, sent)
        who = (row or {}).get("to_email", oid)
        if action == "keep":
            continue
        log.append(f"{action}: {who}")
        if dry:
            tracked.pop(oid)  # preview as if done; nothing is saved
            continue
        if action == "mark_sent":
            await st.update_outbox_item(oid, status="sent", sent_at=sent["sent_at"],
                                        message_id=sent["message_id"], thread_ref=sent["thread"],
                                        provider="gmail", error="")
            if row.get("prospect_id"):
                p = await st.get_prospect(row["prospect_id"])
                if p and p.status in ("new", "queued"):
                    await st.update_prospect_status(p.id, "contacted")
            await st.log_action("email_sent", "user", {"to": who, "step": row.get("step"),
                                                       "kind": row.get("kind"), "via": "gmail_draft"})
        elif action == "dismiss":
            # deleted in Gmail: never a rejection; this version is not put back
            dismissed[oid] = content_hash(row["subject"], row["body"])
        elif action in ("delete_draft", "replace_draft") and alive_cache.get(entry["draft"]):
            await gm.delete_draft(entry["draft"])
            alive_cache[entry["draft"]] = False
        tracked.pop(oid)
        save()

    # 3. Gershon campaign format: one email per company, two people
    if not dry or "--format-preview" in sys.argv:
        import gershon_h01_format as fmt
        from harvey.brain import Brain
        from harvey.untrusted import UNTRUSTED_RULE, fence
        from harvey.company_names import clean_company_name
        fstate = json.loads(FORMAT_FILE.read_text()) if FORMAT_FILE.exists() else {}
        brain = Brain(st, cfg.brain)
        groups = {}
        for r in pending:
            if r["kind"] == "sequence" and int(r["step"] or 1) == 1 and r["id"] not in tracked:
                groups.setdefault(fmt.company_key(r), []).append(r)
        limit = int(os.environ.get("FORMAT_LIMIT", "0") or 0)
        done_n = 0
        for key, grp in groups.items():
            keep, extra = fmt.choose_two(grp)
            ids = sorted(r["id"] for r in keep)
            if fstate.get(key, {}).get("ids") == ids and fstate[key].get("hash") == fmt.group_hash(keep):
                continue  # already in the campaign format
            if limit and done_n >= limit:
                break
            company_name = clean_company_name(keep[0]["company"] or keep[0]["to_email"].split("@")[-1])
            company = await st.get_company(keep[0]["company_id"]) if keep[0]["company_id"] else None
            prompt = fmt.build_prompt(company_name, company, keep, UNTRUSTED_RULE, fence)
            out = None
            for _ in range(2):
                res = await brain.think_json(prompt, session_id="gershon-h01", agent="writer", task="gershon_h01")
                if isinstance(res, dict):
                    subj, body = str(res.get("subject") or "").strip(), str(res.get("body") or "").strip()
                    problems = fmt.check_email(subj, body, keep, company_name)
                    if not problems:
                        out = (subj[:120], body)
                        break
                    log.append(f"format retry {company_name}: {problems}")
            done_n += 1
            if not out:
                log.append(f"FORMAT FAILED {company_name}: left out of Gmail until it works")
                fstate[key] = {"ids": [], "hash": "", "failed": True}
                continue
            if "--format-preview" in sys.argv:
                log.append(f"--- {company_name} -> {', '.join(r['to_email'] for r in keep)}\nSubject: {out[0]}\n{out[1]}")
                continue
            for r in keep:
                await st.edit_outbox_draft(r["id"], out[0], out[1])
            for r in extra:
                await st.reject_outbox_item(r["id"])
                log.append(f"3rd+ contact not used: {r['to_email']}")
            fresh = [await st.get_outbox_item(i) for i in ids]
            fstate[key] = {"ids": ids, "hash": fmt.group_hash([f for f in fresh if f])}
            FORMAT_FILE.write_text(json.dumps(fstate, indent=1))
            log.append(f"formatted: {company_name} ({len(keep)} people)")
        if "--format-preview" in sys.argv:
            print("\n".join(log)); return
        pending = await rows(PENDING_SQL)
        unformatted = {k for k, g in groups.items() if not fstate.get(k, {}).get("ids")}
    else:
        unformatted = set()

    # 4. new drafts (a company's two first emails share one draft)
    import gershon_h01_format as fmt
    made, batches = [], {}
    for r in pending:
        if r["id"] in tracked or dismissed.get(r["id"]) == content_hash(r["subject"], r["body"]):
            continue
        if r["kind"] == "sequence" and int(r["step"] or 1) == 1:
            if fmt.company_key(r) in unformatted:
                continue  # not in the campaign format yet
            bkey = ("s1", fmt.company_key(r), r["subject"], r["body"])
        else:
            bkey = ("one", r["id"])
        batches.setdefault(bkey, []).append(r)
    for bkey, grp in batches.items():
        r = grp[0]
        thread, reply_to = r.get("thread_ref") or "", r.get("in_reply_to") or ""
        if r["kind"] == "sequence" and int(r["step"] or 1) > 1:
            prev = await st.get_previous_step(r)
            delay = await st.get_step_delay_days(r.get("campaign_id"), r["step"])
            if not follow_up_ready(r, prev, delay, now):
                continue
            thread = thread or prev.get("thread_ref") or ""
            reply_to = reply_to or prev.get("message_id") or ""
        body = r["body"]
        if r["kind"] == "sequence" and footer and not body.rstrip().endswith(footer):
            body = body.rstrip() + "\n\n" + footer
        names = ", ".join((x["fn"] + " " + x["ln"]).strip() or x["to_email"] for x in grp)
        label = names + (f" — {r['company']}" if r["company"] else "")
        if dry:
            log.append(f"would draft: {label} | {r['subject']}")
            continue
        to = ", ".join(dict.fromkeys(x["to_email"] for x in grp))
        res = await provider.send_email(to, r["subject"], body, thread_ref=thread, in_reply_to=reply_to)
        if not res.ok:
            log.append(f"FAIL {to}: {res.error}")
            continue
        for x in grp:
            tracked[x["id"]] = {"draft": provider.last.get("draft", ""), "thread": provider.last.get("thread", ""),
                                "hash": content_hash(x["subject"], x["body"])}
        save()
        step = "reply" if r["kind"] == "reply" else f"email {r['step']}"
        made.append(f"- {label} ({step}): {r['subject']}")

    # 5. one short notice when there is something new to look at
    if made and not dry:
        sys.path.insert(0, str(ROOT / "scripts"))
        import gershon_reports as rep
        renv = rep.load_env()
        try:
            rep.send(renv, f"ℹ️ GC Harvey: {len(made)} new email(s) in your Gmail Drafts",
                     f"{len(made)} new Harvey email(s) are in the Drafts folder of {MAILBOX}:\n"
                     f"{DRAFTS_LINK}\n\n" + "\n".join(made) +
                     "\n\nSend = approved (Harvey records it and schedules the follow-up)."
                     "\nDelete = that version is dropped (nothing is rejected). Edit freely before sending."
                     "\nNothing is ever sent without you.")
        except Exception as e:  # noqa: BLE001
            log.append(f"notice email failed: {e}")
    if OLD_DONE.exists() and STATE_FILE.exists() and not dry:
        OLD_DONE.rename(OLD_DONE.with_suffix(".json.old"))
    log.append(f"new drafts: {len(made)} | waiting in Harvey: {len(pending)}")
    print("\n".join(log))


if __name__ == "__main__":
    asyncio.run(main(dry="--dry-run" in sys.argv))
