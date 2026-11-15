"""Gershon campaign format: two people per company, fixed parts checked."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "gershon_h01_format", Path(__file__).parent.parent / "scripts" / "gershon_h01_format.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def row(i, fn, email, title="", status=""):
    return {"id": i, "fn": fn, "ln": "", "to_email": email, "title": title,
            "email_status": status, "company_id": "co1", "created_at": i, "subject": "s", "body": "b"}


def test_two_named_people_before_the_general_inbox():
    rows = [row("1", "", "info@example.com"), row("2", "Ann", "ann@example.com", "Engineer"),
            row("3", "Bob", "bob@example.com", "CEO"), row("4", "Cy", "cy@example.com", "Founder")]
    keep, extra = f.choose_two(rows)
    assert [r["fn"] for r in keep] == ["Bob", "Cy"]
    assert {r["id"] for r in extra} == {"1", "2"}
    assert f.greeting(keep, "Example") == "Hi Bob, Cy,"


def test_general_inbox_greeting():
    keep, _ = f.choose_two([row("1", "", "info@example.com")])
    assert f.greeting(keep, "Example") == "Hi Example team,"


GOOD = """Hi Bob, Cy,

Congratulations on the launch of your new platform in March. It opens a bigger market.

The US is a natural next market for a platform like yours.

Gershon Consulting is a US-focused digital marketing company. Since 2013 we have supported the US expansion of international companies with our Hunter method (Promote, Network, Engage), building their visibility with the right US decision-makers.

Similar companies worked with us: Centreon, Wallix.

If you think an organization that has helped more than 200 companies at the same stage as yours grow their business in the US over the past 13 years could help, today or in the near future, would it make sense to introduce ourselves?

My calendar is open at https://www.gershonconsulting.com/calendar

I also sent you a similar message on LinkedIn, in case my email gets lost in your inbox."""


def test_a_correct_email_passes():
    keep, _ = f.choose_two([row("3", "Bob", "bob@example.com", "CEO"), row("4", "Cy", "cy@example.com", "Founder")])
    assert f.check_email("Example in the US", GOOD, keep, "Example") == []


def test_unknown_reference_client_or_sign_off_is_refused():
    keep, _ = f.choose_two([row("3", "Bob", "bob@example.com", "CEO"), row("4", "Cy", "cy@example.com", "Founder")])
    assert "reference clients" in f.check_email("x", GOOD.replace("Wallix", "Acme Corp"), keep, "Example")
    assert "sign-off in body" in f.check_email("x", GOOD + "\n\nSpeak with you soon,", keep, "Example")
    assert "greeting" in f.check_email("x", GOOD.replace("Hi Bob, Cy,", "Hi Bob,"), keep, "Example")
