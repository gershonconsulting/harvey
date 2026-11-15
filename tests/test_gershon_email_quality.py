"""Gershon: Olivier's signature, clean company names, unfit leads."""

from types import SimpleNamespace

import pytest

from harvey.agents.sender import Sender
from harvey.company_names import clean_company_name, is_placeholder_name, unfit_lead_reason
from harvey.config import ComplianceConfig, SignatureConfig, load_config
from harvey.gate import pre_send_check
from harvey.integrations.signature import signature_text, with_signature
from harvey.paths import PROJECT_ROOT

SIG = ("Best regards,\n\nOlivier Attia\nManaging Director\n"
       "Gershon Consulting - https://www.gershonconsulting.com/\n"
       "2 minutes to understand what we do: https://www.youtube.com/watch?v=RBxLxacezYI")


def _config(text=SIG, address="1 Main St, New York, NY 10019", opt_out="Reply no thanks."):
    email = SimpleNamespace(signature=SignatureConfig(text=text))
    return SimpleNamespace(
        channels=SimpleNamespace(email=email),
        compliance=ComplianceConfig(postal_address=address, opt_out_line=opt_out),
        persona=SimpleNamespace(company="Gershon Consulting", email="o@gershonconsulting.net"),
    )


@pytest.mark.parametrize("raw, clean", [
    ("GoodCore Software London, UK", "GoodCore Software"),
    ("RevenueBase, Inc.", "RevenueBase"),
    ("Acme Ltd", "Acme"),
    ("Berlin Ventures GmbH, Berlin, Germany", "Berlin Ventures"),
    ("Munro Agency", "Munro Agency"),
    ("Bristol Robotics", "Bristol Robotics"),
    ("fibionic", "fibionic"),
    ("London", "London"),          # never cleaned down to nothing
])
def test_clean_company_name(raw, clean):
    assert clean_company_name(raw) == clean


@pytest.mark.parametrize("name, placeholder", [
    ("Checking your browser", True),
    ("Just a moment...", True),
    ("Attention Required! | Cloudflare", True),
    ("403 Forbidden", True),
    ("Home", True),
    ("Homebase", False),
    ("Munro Agency", False),
    ("Error Labs", True),
    ("Errorless", False),
])
def test_placeholder_names(name, placeholder):
    assert is_placeholder_name(name) is placeholder


def test_unfit_leads():
    lead = lambda company, email: SimpleNamespace(company=company, email=email)
    assert "web page message" in unfit_lead_reason(lead("Checking your browser", "support@f6s.com"))
    assert "inbox" in unfit_lead_reason(lead("F6S", "support@f6s.com"))
    assert "inbox" in unfit_lead_reason(lead("Acme", "noreply@acme.com"))
    assert unfit_lead_reason(lead("Munro Agency", "hello@munro.agency")) == ""
    assert unfit_lead_reason(lead("iCreativez", "mehboob@icreativez.com")) == ""


def test_signature_text_is_tidied():
    assert SignatureConfig(text="Best,  \r\nOlivier  \n").text == "Best,\nOlivier"
    assert SignatureConfig(text=None).text == ""


def test_with_signature_adds_once():
    config = _config()
    once = with_signature("Hi Alex,\n\nHow did the US go?", config)
    assert once.endswith(SIG) and "How did the US go?\n\nBest regards," in once
    assert with_signature(once, config) == once
    assert with_signature("Hi", _config(text="")) == "Hi"
    assert signature_text(_config(text="")) == ""


def _sender(config):
    sender = Sender.__new__(Sender)
    sender.config = config
    return sender


def test_outgoing_body_order_and_gate():
    config = _config()
    sender = _sender(config)
    item = {"kind": "sequence", "body": "Hi Alex,\n\nPick a time here: https://www.gershonconsulting.com/calendar"}
    out = sender._outgoing_body(item)
    # body, then the sign-off, then the compliance footer
    assert out.index("Pick a time") < out.index("Best regards,") < out.index("1 Main St") < out.index("Reply no thanks.")
    # The gate reads the body without the sign-off: its links don't count
    # against the one-link limit.
    checked = sender._outgoing_body(item, signed=False)
    assert "Best regards," not in checked and "1 Main St" in checked
    assert pre_send_check("alex@zibra.ai", "Zibra AI in the US", checked,
                          postal_address="1 Main St, New York, NY 10019").ok
    assert not pre_send_check("alex@zibra.ai", "Zibra AI in the US", out).ok  # 3 links


def test_replies_get_the_signature_but_no_footer():
    out = _sender(_config())._outgoing_body({"kind": "reply", "body": "Thanks, Tuesday works."})
    assert out.endswith(SIG) and "1 Main St" not in out


def test_render_uses_the_clean_company_name():
    prospect = SimpleNamespace(first_name="", last_name="", company="GoodCore Software London, UK",
                               title="", personalization_notes="", is_company_inbox=True)
    text = _sender(_config())._render("Hi {{first_name}}, {{company}} in the US", prospect)
    assert text == "Hi there, GoodCore Software in the US"


def test_gershon_config_has_signature_and_rules():
    path = PROJECT_ROOT / "harvey.local.yaml"
    if not path.exists():
        pytest.skip("no Gershon config here")
    config = load_config(str(path))
    assert config.channels.email.signature.text.startswith("Speak with you soon,\n\nOlivier Attia")
    assert config.writer.email_rules == "skills/gershon_email_rules.md"
    assert (PROJECT_ROOT / config.writer.email_rules).exists()
