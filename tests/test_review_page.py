"""Gershon add-on: the private review page for emails waiting for approval.

It must let the reviewer edit, approve or reject ONE email at a time, only
with the private key, only from its own page, and never send anything.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
import harvey.review_app as review
from harvey.models import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager

LOCAL = "http://127.0.0.1:5556"
PUBLIC = "https://review.example.com"


async def _seed(db_path):
    state = StateManager(str(db_path))
    await state.init_db()
    company_id = await state.add_company(Company(
        name="Northwind Example", domain="northwind.example",
        website="https://northwind.example", description="Example widgets"))
    ids = []
    for n in (1, 2):
        pid = await state.add_prospect(Prospect(
            email=f"person{n}@northwind.example", email_status="verified", title="CEO",
            first_name=f"Person{n}", last_name="Example", company="Northwind Example",
            company_id=company_id, score=80))
        ids.append(await state.add_outbox_item(
            prospect_id=pid, to_email=f"person{n}@northwind.example",
            subject=f"hello {n}", body=f"Hi Person{n},\n\nA short note.\n\nOlivier",
            send_at="2026-01-01T00:00:00"))
    return ids


def _status(db, item_id):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT status, sent_at FROM outbox WHERE id = ?", (item_id,)).fetchone()
    finally:
        con.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "data" / "harvey.db"
    db.parent.mkdir()
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_harvey_can_send", lambda: False)
    monkeypatch.delenv("HARVEY_REVIEW_URL", raising=False)
    review.token_store.configure(tmp_path / "data" / "review-token")
    ids = asyncio.run(_seed(db))
    return {"db": db, "ids": ids, "key": review.token_store.get()}


def _client(base=LOCAL):
    return TestClient(review.app, base_url=base, headers={"Origin": base})


def _signed_in(env, base=LOCAL):
    c = _client(base)
    r = c.post("/review/session", json={"key": env["key"]})
    assert r.status_code == 200, r.text
    return c


def test_page_shell_is_public_but_data_needs_the_key(env):
    c = _client()
    assert c.get("/review").status_code == 200
    assert c.get("/review/api/pending").status_code == 401
    assert c.post("/review/session", json={"key": "x" * 43}).status_code == 401
    assert "harvey_review" not in c.cookies


def test_the_key_lists_every_waiting_email_in_full(env):
    data = _signed_in(env).get("/review/api/pending").json()
    assert [i["subject"] for i in data["items"]] == ["hello 1", "hello 2"]
    first = data["items"][0]
    assert first["to_email"] == "person1@northwind.example"
    assert first["body"].startswith("Hi Person1")
    assert first["context"]["company"] == "Northwind Example"
    assert first["context"]["contact"] == "Person1 Example"
    assert data["can_send"] is False


def test_approve_marks_only_that_one_email_and_sends_nothing(env):
    c = _signed_in(env)
    a, b = env["ids"]
    assert c.post(f"/review/api/{a}/approve", json={}).json()["success"] is True
    assert _status(env["db"], a) == ("approved", None)
    assert _status(env["db"], b) == ("pending_review", None)
    # Twice is refused, and there is no approve-all.
    assert c.post(f"/review/api/{a}/approve", json={}).status_code == 409
    assert c.post("/review/api/approve-all", json={}).status_code in (404, 405)
    assert _status(env["db"], b) == ("pending_review", None)


def test_reject_and_edit(env):
    c = _signed_in(env)
    a, b = env["ids"]
    r = c.post(f"/review/api/{b}/edit", json={"subject": "new subject",
                                               "body": "Hi Person2,\n\nRewritten.\n\nOlivier"})
    assert r.json()["success"] is True, r.text
    item = next(i for i in c.get("/review/api/pending").json()["items"] if i["id"] == b)
    assert item["subject"] == "new subject"
    assert _status(env["db"], b) == ("pending_review", None)  # an edit never approves
    assert c.post(f"/review/api/{a}/reject", json={}).json()["success"] is True
    assert _status(env["db"], a)[0] == "rejected"
    assert c.post(f"/review/api/{a}/approve", json={}).status_code == 409


def test_cross_site_requests_are_refused(env):
    c = _signed_in(env)
    a = env["ids"][0]
    assert c.post(f"/review/api/{a}/approve", json={},
                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post(f"/review/api/{a}/approve", json={},
                  headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post(f"/review/api/{a}/approve", content=b"x",
                  headers={"Content-Type": "text/plain"}).status_code == 403
    no_origin = TestClient(review.app, base_url=LOCAL, cookies=dict(c.cookies))
    assert no_origin.post(f"/review/api/{a}/approve", json={}).status_code == 403
    assert _status(env["db"], a)[0] == "pending_review"


def test_unknown_host_is_refused(env):
    assert _client("http://attacker.example").get("/review").status_code == 400


def test_public_name_needs_https_and_gets_a_secure_cookie(env, monkeypatch):
    monkeypatch.setenv("HARVEY_REVIEW_URL", PUBLIC)
    c = _client(PUBLIC)
    r = c.post("/review/session", json={"key": env["key"]})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "strict-transport-security" in r.headers
    plain = TestClient(review.app, base_url=PUBLIC, headers={"Origin": "http://review.example.com"})
    assert plain.post("/review/session", json={"key": env["key"]}).status_code == 403


def test_rotating_the_key_ends_old_sessions(env, tmp_path):
    c = _signed_in(env)
    (tmp_path / "data" / "review-token").unlink()
    review.token_store.configure(tmp_path / "data" / "review-token")
    assert c.get("/review/api/pending").status_code == 401


def test_link_keeps_the_key_in_the_fragment(env):
    link = review.review_link(PUBLIC)
    assert link == f"{PUBLIC}/review#k={env['key']}"


# ── The alert emails carry the link ──

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gershon_reports.py"


def _reports(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("gershon_reports_review", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "REVIEW_TOKEN", tmp_path / "data" / "review-token")
    return mod


def test_alert_emails_link_to_the_page(env, tmp_path, monkeypatch):
    mod = _reports(tmp_path, monkeypatch)
    e = {"HARVEY_REVIEW_URL": PUBLIC + "/"}
    assert mod.review_link(e) == f"{PUBLIC}/review#k={env['key']}"
    draft = ("o1", "a@example.com", "s", "b", 1, "sequence", "now", "A", "B", "CEO", "Co", "")
    assert f"{PUBLIC}/review#k=" in mod.fmt_drafts([draft], e)
    snap = {"service": "active", "brain_ready": True, "pending_review": 3, "sent_24h": 0,
            "open_conversations": 0, "replies_total": 0, "outbox": {}, "prospects": {},
            "errors": [], "status_text": ""}
    assert f"{PUBLIC}/review#k=" in mod.fmt(snap, e)
    # No link without an https address, and none when nothing waits.
    assert mod.review_link({"HARVEY_REVIEW_URL": "http://review.example.com"}) == ""
    assert mod.review_link({}) == ""
    assert "/review#k=" not in mod.fmt({**snap, "pending_review": 0}, e)
