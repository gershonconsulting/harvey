"""Gershon campaign format for Harvey's first emails (campaign H01).

Olivier's campaigns send ONE first email per company to TWO people
("Hi Barry, Colm,"), in a fixed 7-part shape (see FORMAT below). Harvey
writes one email per person in its own shape, so before an email reaches
Gmail Drafts this rewrites it at company level:

  * waiting first emails (outbox status pending_review, step 1) are grouped
    by company; the two best contacts are kept (named people before a
    general inbox, decision-makers first), any others are rejected
  * one email is written for the company with Harvey's brain, from the
    research Harvey has on the company and both people (nothing invented)
  * both outbox rows get that same subject and body; the Gmail sync then
    makes ONE draft addressed to both.

State: data/gershon_h01_format.json {company_key: {"ids": [...], "hash": h}}
"""
import hashlib, json, re

BOOKING = "https://www.gershonconsulting.com/calendar"

# Field tags are for choosing; only the names go into the email.
REFERENCE_CLIENTS = [
    ("Centreon", "IT monitoring software"),
    ("Wallix", "cybersecurity software"),
    ("Contentsquare", "digital-experience analytics SaaS"),
    ("DAB Embedded", "embedded systems, electronics"),
    ("Ducrocq Engineering", "engineering services"),
    ("Altavia", "retail marketing, agencies"),
    ("MAbSilico", "AI drug discovery, deep tech"),
    ("Enzymicals", "biotech, biocatalysis"),
    ("Amphasys", "life-science instruments"),
    ("PhiTech Bioinformatics", "bioinformatics software"),
    ("Meteor Biotech", "biotech"),
]

FORMAT = f"""THE EMAIL, plain text, exactly these parts in this order, nothing else:

1. Greeting line: "Hi <first name>, <first name>," for two people
   ("Hi Barry, Colm,"), "Hi <first name>," for one, "Hi <Company> team,"
   for a general inbox with no person.
2. One short paragraph that opens with "Congratulations on" followed by ONE
   specific, checkable achievement of the company taken from the FACTS (a
   launch, a release, an award, a funding round, a new office or market, a
   customer win, a milestone, an anniversary), then one sentence on why it
   matters. Name the date or source when the FACTS give it. If the FACTS hold
   no achievement, open with "I saw that <Company>" and the most specific fact
   about what they do, and do NOT write "Congratulations". Never invent an
   event, a date, a number, a customer or a source.
3. One short paragraph on their US expansion: who would buy what they sell
   in the US and why being visible to those US decision-makers matters. Only
   from the FACTS; if the FACTS say nothing about the US, speak about the US
   as their next market, never as something they already did.
4. This paragraph, word for word:
   "Gershon Consulting is a US-focused digital marketing company. Since 2013
   we have supported the US expansion of international companies with our
   Hunter method (Promote, Network, Engage), building their visibility with
   the right US decision-makers."
5. "Similar companies worked with us: " followed by 2 or 3 names from the
   REFERENCE CLIENTS list, the closest to this company's field, separated by
   commas, ending with a full stop. Never a name that is not on the list.
6. This paragraph, word for word:
   "If you think an organization that has helped more than 200 companies at
   the same stage as yours grow their business in the US over the past 13
   years could help, today or in the near future, would it make sense to
   introduce ourselves?"
   then on its own line, word for word:
   "My calendar is open at {BOOKING}"
7. Last line, word for word:
   "I also sent you a similar message on LinkedIn, in case my email gets
   lost in your inbox."

Do NOT write a sign-off, a name or a signature: "Speak with you soon," and
Olivier's signature block are added under the email automatically.
Paragraphs are separated by one blank line. No other link, no other number,
no P.S., no bullet points.

SUBJECT: "<Company> in the US", with the company's short name as people
write it (no Ltd, GmbH, B.V., Inc)."""

GENERIC_LOCAL = {"info", "contact", "hello", "support", "team", "sales", "office",
                 "admin", "mail", "enquiries", "inquiries", "hr", "careers", "press"}
TITLE_RANK = ["ceo", "chief executive", "founder", "co-founder", "managing director",
              "president", "owner", "geschäftsführer", "directeur", "cto", "coo",
              "chief", "vp", "head", "director"]


def company_key(row):
    if row.get("company_id"):
        return "c:" + row["company_id"]
    return "d:" + (row.get("to_email", "").split("@")[-1].lower())


def is_inbox(row):
    return not (row.get("fn") or "").strip() or row["to_email"].split("@")[0].lower() in GENERIC_LOCAL


def person_rank(row):
    title = (row.get("title") or "").lower()
    rank = next((i for i, t in enumerate(TITLE_RANK) if t in title), len(TITLE_RANK))
    verified = 0 if row.get("email_status") == "verified" else 1
    return (1 if is_inbox(row) else 0, rank, verified, row.get("created_at") or "")


def choose_two(rows):
    """The (at most) two contacts to write to, and the rest."""
    ordered = sorted(rows, key=person_rank)
    seen, keep = set(), []
    for r in ordered:
        addr = r["to_email"].lower()
        if addr in seen:
            continue
        seen.add(addr)
        if len(keep) < 2:
            keep.append(r)
    keep_ids = {r["id"] for r in keep}
    return keep, [r for r in rows if r["id"] not in keep_ids]


def greeting(keep, company):
    names = [(r.get("fn") or "").strip() for r in keep if not is_inbox(r)]
    names = [n for n in names if n]
    if not names:
        return f"Hi {company} team,"
    return "Hi " + ", ".join(dict.fromkeys(names)) + ","


def group_hash(rows):
    return hashlib.sha256(json.dumps(sorted((r["id"], r["subject"], r["body"]) for r in rows)).encode()).hexdigest()[:16]


def check_email(subject, body, keep, company):
    """Hard checks before an email replaces Harvey's: the fixed parts are
    there, one link, the right greeting. Returns a list of problems."""
    problems = []
    if not body.startswith(greeting(keep, company)):
        problems.append("greeting")
    for must in ("Gershon Consulting is a US-focused digital marketing company.",
                 "Hunter method (Promote, Network, Engage)",
                 "Similar companies worked with us:",
                 "more than 200 companies",
                 f"My calendar is open at {BOOKING}",
                 "I also sent you a similar message on LinkedIn"):
        if must not in re.sub(r"\s+", " ", body):
            problems.append(f"missing: {must[:40]}")
    if len(re.findall(r"https?://", body)) != 1:
        problems.append("links")
    names_used = re.search(r"Similar companies worked with us:([^\n]*)", body)
    if names_used:
        allowed = {n for n, _ in REFERENCE_CLIENTS}
        used = [n.strip(" .") for n in re.split(r",| and ", names_used.group(1)) if n.strip(" .")]
        if not used or any(n not in allowed for n in used):
            problems.append("reference clients")
    if re.search(r"speak with you soon|best regards|olivier", body, re.I):
        problems.append("sign-off in body")
    if not subject.strip() or len(body.split()) > 220:
        problems.append("length/subject")
    return problems


def build_prompt(company_name, company, keep, untrusted_rule, fence):
    facts = [f"- Company: {company_name}"]
    if company is not None:
        for label, val in (("Website", company.website or company.domain), ("Industry", company.industry),
                           ("Location", company.location), ("Size", company.company_size),
                           ("What the company says about itself", company.description),
                           ("Notes", company.notes)):
            if val:
                facts.append(f"- {label}: {val}")
        for s in (company.signals or [])[:6]:
            facts.append(f"- Signal ({s.get('type', 'signal')}): {s.get('detail', '')}")
    for r in keep:
        who = "General inbox" if is_inbox(r) else f"{(r.get('fn') or '').strip()} {(r.get('ln') or '').strip()}".strip()
        line = f"- Recipient: {who}" + (f", {r['title']}" if r.get("title") else "")
        if r.get("notes"):
            line += f". Research notes: {r['notes']}"
        facts.append(line)
        # Harvey's own draft holds what its research found; reuse its facts only
        facts.append(f"- Harvey's earlier draft to {who} (facts only, not its style): {r['body'][:900]}")
    clients = "\n".join(f"- {n} ({tag})" for n, tag in REFERENCE_CLIENTS)
    return f"""You write ONE cold email from Olivier Attia, founder of Gershon Consulting,
to {len(keep)} {'people' if len(keep) > 1 else 'person'} at {company_name}, in his campaign format.

FACTS — everything known about the company and the recipients. Every claim
about them must come from here; if it isn't here, you don't know it:
{fence(chr(10).join(facts), "prospect research")}

{untrusted_rule}

REFERENCE CLIENTS (choose from these only):
{clients}

{FORMAT}

The greeting line must be exactly: {greeting(keep, company_name)}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""
