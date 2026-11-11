"""Gershon add-on: an HTML email signature (signature.html) in place of the
plain-text sign-off, and a blind copy of every email (channels.email.bcc)."""
import base64
import email
import email.policy
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from harvey.config import EmailChannelConfig, SignatureConfig
from harvey.integrations.signature import (
    SignatureError, bcc_addresses, load_signature, render_html, set_body, signature_problem, with_signature,
)

SIGN_OFF = "Best,\n\nSam Sender\nExample Co"
BLOCK = '<table><tr><td><b>Sam Sender</b> <a href="https://example.com">Example Co</a></td></tr></table>'


def cfg(tmp_path, html=BLOCK, bcc=None, **sig):
    path = tmp_path / "sig.html"
    if html is not None:
        path.write_text(html, encoding="utf-8")
    c = SimpleNamespace(
        persona=SimpleNamespace(name="Sam Sender", email="sam@sender.example", company="Example Co"),
        channels=SimpleNamespace(email=EmailChannelConfig(
            provider="gmail", bcc=bcc or [], signature=SignatureConfig(html=str(path), text=SIGN_OFF, **sig))),
    )
    return c


def test_html_block_replaces_the_text_sign_off_and_keeps_the_footer(tmp_path):
    c = cfg(tmp_path)
    sig = load_signature(c)
    body = with_signature("Hi Jane,\n\nOne question.", c) + "\n\nExample Co, 1 Example St\nReply STOP to opt out"
    out = render_html(body, sig)
    assert BLOCK in out and "Hi Jane," in out
    assert "Best," not in out                          # the plain sign-off is replaced, not doubled
    assert out.index(BLOCK) < out.index("Reply STOP to opt out")


def test_html_only_signature_has_no_image_part(tmp_path):
    sig = load_signature(cfg(tmp_path))
    msg = EmailMessage()
    set_body(msg, with_signature("Hi Jane.", cfg(tmp_path)), sig)
    parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
    assert parsed.get_content_type() == "multipart/alternative"
    plain, html_part = parsed.get_payload()
    assert "Sam Sender" in plain.get_content()          # plain text keeps the text sign-off
    assert html_part.get_content_type() == "text/html" and BLOCK in html_part.get_content()


@pytest.mark.parametrize("bad, why", [
    (None, "not found"), ("", "empty"), ("<script>x()</script>", "scripts"),
    ('<a href="#" onclick="x()">a</a>', "scripts"), ("x" * 100_001, "KB"),
])
def test_unusable_html_signature_holds_sending(tmp_path, bad, why):
    c = cfg(tmp_path, html=bad)
    with pytest.raises(SignatureError):
        load_signature(c)
    assert why in signature_problem(c)


def test_bcc_is_validated_and_tidied():
    assert EmailChannelConfig(bcc=" Team@Example.com ").bcc == ["team@example.com"]
    assert EmailChannelConfig(bcc=["a@example.com", "A@example.com"]).bcc == ["a@example.com"]
    assert EmailChannelConfig().bcc == []
    with pytest.raises(ValidationError):
        EmailChannelConfig(bcc=["not-an-address"])


@pytest.mark.asyncio
async def test_gmail_blind_copies_and_signs(tmp_path):
    from harvey.integrations import gmail
    c = cfg(tmp_path, bcc=["record@example.com"])
    p = gmail.GmailProvider(c, SimpleNamespace())
    p._request = AsyncMock(side_effect=[{"id": "g1", "threadId": "t1"}, None])
    result = await p.send_email("jane@acme.example", "hi", with_signature("Hi Jane.", c))
    assert result.ok
    raw = p._request.call_args_list[0].kwargs["json"]["raw"]
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
    assert parsed["Bcc"] == "record@example.com"
    assert BLOCK in parsed.get_payload()[1].get_content()


@pytest.mark.asyncio
async def test_smtp_blind_copies(tmp_path, monkeypatch):
    from harvey.integrations import smtp_mail
    sent = []

    async def fake_send(msg, **kwargs):
        sent.append(msg)
    monkeypatch.setattr(smtp_mail.aiosmtplib, "send", fake_send)
    c = cfg(tmp_path, bcc=["record@example.com"])
    env = SimpleNamespace(smtp_host="smtp.sender.example", smtp_port=587,
                          smtp_username="sam@sender.example", smtp_password="x")
    result = await smtp_mail.SmtpImapProvider(c, env).send_email("jane@acme.example", "hi", "Hi Jane.")
    assert result.ok and sent[0]["Bcc"] == "record@example.com"


def test_no_bcc_by_default(tmp_path):
    assert bcc_addresses(cfg(tmp_path)) == []


def test_gershon_config_has_the_signature_and_bcc():
    from harvey.config import load_config
    c = load_config()
    assert c.channels.email.bcc == ["harvey@gershonconsulting.com"]
    sig = load_signature(c)
    assert sig is not None and "Featured in".upper() in sig.html.upper() and sig.text.startswith("Speak with you soon,")
