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


def test_general_inbox_is_never_used():
    keep, extra = f.choose_two([row("1", "", "contact@example.com"), row("2", "", "sales@example.com")])
    assert keep == [] and len(extra) == 2
    assert f.missing_contacts([row("1", "", "contact@example.com")]) == "no named CEO"


def test_ceo_alone_or_no_ceo_is_held():
    keep, _ = f.choose_two([row("3", "Bob", "bob@example.com", "CEO"), row("1", "", "contact@example.com")])
    assert keep == []
    assert f.missing_contacts([row("3", "Bob", "bob@example.com", "CEO")]) == "no +1 next to the CEO"
    keep, _ = f.choose_two([row("2", "Ann", "ann@example.com", "Engineer"), row("5", "Dee", "dee@example.com", "Sales")])
    assert keep == []


def test_ceo_plus_one_any_role():
    keep, _ = f.choose_two([row("2", "Ann", "ann@example.com", "Engineer"),
                            row("3", "Bob", "bob@example.com", "Chief Executive Officer"),
                            row("1", "", "contact@example.com")])
    assert [r["fn"] for r in keep] == ["Bob", "Ann"]


GOOD = """Hi Bob, Cy,

Congratulations on the launch of your new platform in March. It opens a bigger market.

US logistics operators are automating warehouses fast because of labour shortages, which makes the US a natural next market for a platform like yours.

Gershon Consulting is a US-focused digital marketing company. Since 2013 we have supported the US expansion of international companies with our Hunter method (Promote, Network, Engage), building their visibility with the right US decision-makers.

In US companies this is chosen by operations leaders, not IT alone, and they are hard to reach from outside the US. We know this challenge well, and we learned it working with similar companies: Centreon, Wallix.

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


def test_old_references_line_is_refused():
    keep, _ = f.choose_two([row("3", "Bob", "bob@example.com", "CEO"), row("4", "Cy", "cy@example.com", "Founder")])
    old = GOOD.replace("In US companies this is chosen by operations leaders, not IT alone, and they are hard to reach from outside the US. We know this challenge well, and we learned it working with similar companies:", "Similar companies worked with us:")
    assert any(p.startswith("missing: we learned it") for p in f.check_email("x", old, keep, "Example"))


def test_format_asks_for_industry_reasons_and_icp_challenge():
    assert "very\n   focused industry" in f.FORMAT and "ICP in the\n   US" in f.FORMAT
