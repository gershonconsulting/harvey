"""Company names fit to put in an email, and leads that aren't.

A homepage's <title> is not always the business's name: a bot check
("Checking your browser"), an error page or a location tacked on
("GoodCore Software London, UK") all end up in the prospect record and from
there in "Hi, ... at Checking your browser". These helpers clean the name
for writing and say when a lead isn't worth an email at all.
"""

import re

# Titles that name a page state, not a business.
_PLACEHOLDER = re.compile(
    r"(?i)^\W*(checking your browser|just a moment|attention required|access denied"
    r"|forbidden|40[0-9]\b|50[0-9]\b|page not found|not found|site not found|error\b"
    r"|domain (?:is )?for sale|this domain|parked|untitled|home(?: ?page)?$|welcome\b"
    r"|index of\b|cloudflare|ddos|captcha|are you (?:a )?(?:human|robot)|robot or human"
    r"|verify (?:you are|that you are) (?:a )?human|security check|one more step"
    r"|please wait|loading\b|coming soon|under construction|maintenance\b"
    r"|log ?in\b|sign ?in\b)")

# Inboxes that answer customers or machines, never a founder.
_UNFIT_INBOXES = frozenset("""
    support help helpdesk service customerservice customer-service care noreply no-reply
    donotreply do-not-reply billing invoices accounts accounting abuse privacy gdpr dpo
    legal security jobs careers hr recruiting recruitment press media webmaster postmaster
    unsubscribe bounce mailer-daemon
""".split())

_COUNTRIES = (
    r"UK|U\.K\.|United Kingdom|England|Scotland|Wales|Northern Ireland|Ireland|GB"
    r"|Germany|Deutschland|DE|Netherlands|The Netherlands|Nederland|Holland|NL"
    r"|USA|U\.S\.A\.|US|United States|Belgium|France|Switzerland|Austria|Spain|Italy"
    r"|Portugal|Sweden|Denmark|Norway|Finland|Poland|Israel|India|Pakistan|Canada|Australia"
)
_CITIES = (
    r"London|Manchester|Birmingham|Leeds|Bristol|Liverpool|Sheffield|Newcastle|Nottingham"
    r"|Cambridge|Oxford|Reading|Brighton|Edinburgh|Glasgow|Cardiff|Belfast|Dublin"
    r"|Berlin|Munich|München|Hamburg|Frankfurt|Cologne|Köln|Stuttgart|Düsseldorf|Dusseldorf"
    r"|Leipzig|Dresden|Hannover|Amsterdam|Rotterdam|Utrecht|Eindhoven|The Hague|Den Haag"
    r"|Delft|Groningen|Leiden|Paris|Lyon|Brussels|Antwerp|Zurich|Zürich|Geneva|Vienna"
    r"|Madrid|Barcelona|Lisbon|Stockholm|Copenhagen|Oslo|Helsinki|Warsaw|Tel Aviv"
    r"|Karachi|Lahore|Islamabad|Bangalore|Bengaluru|Mumbai|Delhi|New York|San Francisco"
)
_SEP = r"(?:\s*[,|/·•\-–—]\s*|\s+(?:in|from)\s+|\s+)"
_TRAILING_COUNTRY = re.compile(rf"(?i){_SEP}\(?(?:{_COUNTRIES})\)?\.?\s*$")
_TRAILING_CITY = re.compile(rf"(?i){_SEP}\(?(?:{_CITIES})\)?\s*$")
_TRAILING_LEGAL = re.compile(
    r"(?i)[\s,]+(?:ltd\.?|limited|llc|l\.l\.c\.|inc\.?|incorporated|gmbh|ag|b\.?v\.?|n\.?v\.?"
    r"|plc|s\.?a\.?r?\.?l?\.?|ug(?: \(haftungsbeschränkt\))?|co\.?|corp\.?|pvt\.?(?: ltd\.?)?)\s*$")


def is_placeholder_name(name: str) -> bool:
    """True when ``name`` is a page state (bot check, error, parked domain)."""
    return bool(_PLACEHOLDER.search((name or "").strip()))


def clean_company_name(name: str) -> str:
    """The name as a person would write it in an email: "GoodCore Software
    London, UK" -> "GoodCore Software", "Acme Ltd" -> "Acme". Never returns
    less than a word: when cleaning would leave nothing, the name stays."""
    original = re.sub(r"\s+", " ", (name or "")).strip(" ,.-|")
    out = original
    for _ in range(3):
        before = out
        out = _TRAILING_COUNTRY.sub("", out).strip(" ,.-|")
        out = _TRAILING_CITY.sub("", out).strip(" ,.-|")
        out = _TRAILING_LEGAL.sub("", out).strip(" ,.-|")
        if out == before:
            break
    return out if re.search(r"\w", out) else original


def unfit_lead_reason(prospect) -> str:
    """Why this lead should get no cold email, or "" when it's fine."""
    company = (getattr(prospect, "company", "") or "").strip()
    if is_placeholder_name(company):
        return f"company name is a web page message ('{company[:40]}'), not a business"
    email = (getattr(prospect, "email", "") or "").strip().lower()
    local = email.split("@", 1)[0] if "@" in email else ""
    if local in _UNFIT_INBOXES or local.split("+", 1)[0] in _UNFIT_INBOXES:
        return f"{local}@ is a customer-service or system inbox, not a decision maker"
    return ""
