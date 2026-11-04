import asyncio
from harvey.integrations import email_finder as ef


class Env:
    reoon_api_key = ""
    zerobounce_api_key = ""
    millionverifier_api_key = "k"


def _run(monkeypatch, verdict, mx="google"):
    async def fake(email, key):
        return verdict
    monkeypatch.setattr(ef, "verify_millionverifier", fake)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    return asyncio.run(ef._verify_candidate("a@b.com", mx, Env()))


def test_ok_is_verified(monkeypatch):
    assert _run(monkeypatch, {"status": "valid", "catch_all": False})[0] == "verified"


def test_invalid(monkeypatch):
    assert _run(monkeypatch, {"status": "invalid", "catch_all": False})[0] == "invalid"


def test_catch_all_is_risky(monkeypatch):
    assert _run(monkeypatch, {"status": "catch_all", "catch_all": True}) == ("risky", True)
