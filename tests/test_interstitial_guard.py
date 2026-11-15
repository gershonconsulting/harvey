"""A bot check or error page title is never a company.

Audit 2026-11-15: Harvey drafted outreach to a "company" named "Checking your
browser" — a bot-protection page was scraped and its title taken as a name.
"""

import pytest

from harvey.company_names import (
    interstitial_kind, is_interstitial_title, is_placeholder_name, page_title,
)


@pytest.mark.parametrize("title, kind", [
    ("Checking your browser", "bot_check"),
    ("Checking your browser before accessing acme.io", "bot_check"),
    ("Just a moment...", "bot_check"),
    ("Attention Required! | Cloudflare", "bot_check"),
    ("Access denied", "bot_check"),
    ("Sucuri WebSite Firewall - Access Denied", "bot_check"),
    ("Verify you are human", "bot_check"),
    ("Please verify you are a human", "bot_check"),
    ("Captcha", "bot_check"),
    ("DDoS protection by Cloudflare", "bot_check"),
    ("DDoS-Guard", "bot_check"),
    ("403 Forbidden", "error"),
    ("404 Not Found", "error"),
    ("Error 404 - Page not found", "error"),
    ("Page not found | Acme", "error"),
    ("503 Service Unavailable", "error"),
    ("502 Bad Gateway", "error"),
])
def test_interstitial_titles(title, kind):
    assert interstitial_kind(title) == kind
    assert is_interstitial_title(title)
    assert is_placeholder_name(title)       # never written into an email either


@pytest.mark.parametrize("title", [
    "", "Acme Software | London", "Home", "Welcome to Acme", "GoodCore Software",
    "404 Creative Studio", "Error Free Accounting", "Browserstack", "Moment Health",
])
def test_real_titles_are_not_interstitials(title):
    assert interstitial_kind(title) == ""
    assert not is_interstitial_title(title)


def test_page_title_reads_the_first_title():
    assert page_title("<html><head><title>\n  Checking your\n browser </title></head>") == \
        "Checking your browser"
    assert page_title("<p>no title</p>") == ""
    assert page_title(None) == ""
