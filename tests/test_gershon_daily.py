"""Gershon add-on: the daily report says what happened in plain words."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gershon_daily.py"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)  # 08:00 Sint Maarten


def load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gershon_daily", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "DB", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(mod, "GMAIL_STATE", tmp_path / "data" / "gmail_drafts.json")
    monkeypatch.setattr(mod, "GMAIL_DISMISSED", tmp_path / "data" / "gmail_drafts_dismissed.json")
    return mod


def make_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE companies (id TEXT, name TEXT, domain TEXT, website TEXT, location TEXT,
            created_at TEXT);
        CREATE TABLE prospects (id TEXT, company_id TEXT, first_name TEXT, last_name TEXT,
            title TEXT, email TEXT, email_status TEXT, company TEXT, status TEXT, created_at TEXT);
        CREATE TABLE outbox (id TEXT, prospect_id TEXT, to_email TEXT, step INTEGER, kind TEXT,
            status TEXT, error TEXT DEFAULT '', sent_at TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE conversations (id TEXT, prospect_id TEXT, intent TEXT, stage TEXT,
            updated_at TEXT);
        CREATE TABLE settings (key TEXT, value TEXT);
    """)
    # 03:30 UTC on the 14th is still the 13th in Sint Maarten: it counts as yesterday
    con.executemany("INSERT INTO companies VALUES (?,?,?,?,?,?)", [
        ("c1", "Acme GmbH", "acme.de", "https://acme.de", "Berlin", "2026-11-13 15:00:00"),
        ("c2", "Bolt SAS", "bolt.fr", "", "Paris", "2026-11-14 03:30:00"),
        ("c3", "Old Ltd", "old.uk", "", "London", "2026-11-09 10:00:00"),
    ])
    con.executemany("INSERT INTO prospects VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("p1", "c1", "Anna", "Berg", "CEO", "anna@acme.de", "verified", "", "queued", "2026-11-13 15:05:00"),
        ("p2", "c2", "", "", "", "info@bolt.fr", "guess", "", "new", "2026-11-14 03:31:00"),
        ("p3", "c3", "Tom", "Old", "CTO", "tom@old.uk", "verified", "", "write_failed", "2026-11-09 10:05:00"),
    ])
    con.executemany("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("o1", "p1", "anna@acme.de", 1, "sequence", "pending_review", "", None, "2026-11-13 16:00:00", "2026-11-13 16:00:00"),
        ("o2", "p1", "anna@acme.de", 2, "sequence", "pending_review", "", None, "2026-11-13 16:00:00", "2026-11-13 16:00:00"),
        ("o3", "p3", "tom@old.uk", 1, "sequence", "failed", "gate: email_status 'risky' is not deliverable",
         None, "2026-11-09 11:00:00", "2099-01-01 00:00:00"),
    ])
    con.commit()
    con.close()


def collect(mod, tmp_path, **over):
    make_db(tmp_path / "data" / "harvey.db")
    (tmp_path / "data" / "gmail_drafts.json").write_text(json.dumps({"o1": {"draft": "d1"}}))
    d = mod.collect({"DAILY_TARGET": "10"}, now=NOW, probe=False)
    d.update(over)
    return d


def test_days_are_counted_in_local_time_and_yesterday_is_listed(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    d = collect(mod, tmp_path)
    assert d["yesterday"] == "2026-11-13" and d["y"]["companies"] == 2
    assert [c["name"] for c in d["new_companies"]] == ["Acme GmbH", "Bolt SAS"]
    acme, bolt = d["new_companies"]
    assert acme["email_ready"] and acme["sendable"]
    assert not bolt["sendable"] and not bolt["email_ready"]
    assert len(d["days"]) == 7 and d["days"][2]["companies"] == 1  # the 9th


def test_only_drafts_really_in_gmail_are_counted_as_to_review(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    d = collect(mod, tmp_path)
    assert (d["pending_total"], d["drafts_in_gmail"], d["drafts_later"]) == (2, 1, 1)
    todo = mod.for_you(d)
    assert todo[0][0] == "Review 1 email(s) in Gmail Drafts" and todo[0][2] == mod.DRAFTS_LINK


def test_expired_login_is_explained_and_named_as_the_reason(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    d = collect(mod, tmp_path, brain_ready=False, brain_since="2026-11-04")
    probs = mod.problems(d)
    ok, line = mod.verdict(d, probs)
    assert not ok and "Main problem: Harvey's ChatGPT login has expired" in line
    heads = [p[0] for p in probs]
    assert "1 contact(s) have no email written" in heads
    assert any("could not be confirmed as real" in p[1] for p in probs)
    assert mod.subject(d, ok, probs).startswith("❌ Harvey H01 daily 2026-11-14: 2/10 new companies")


def test_good_day_gets_the_green_check(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    d = collect(mod, tmp_path, errors=[], write_failed=0)
    d["y"]["companies"] = 12
    probs = mod.problems(d)
    ok, line = mod.verdict(d, probs)
    assert ok and not probs and line.startswith("Target met: 12")
    assert mod.subject(d, ok, probs).startswith("✅")


def test_html_and_text_have_no_raw_dumps(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    d = collect(mod, tmp_path, brain_ready=False)
    page, text = mod.render_html(d), mod.render_text(d)
    for out in (page, text):
        assert "{'" not in out and "pending_review" not in out and "srv1511567" not in out
        assert "Acme GmbH" in out and "Campaign so far".lower() in out.lower()
    assert "<script" not in page


def test_report_goes_to_both_addresses_once(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    assert mod.recipients({"REPORT_TO": "a@x.com", "STATS_TO": "b@x.com, A@x.com"}) == ["a@x.com", "b@x.com"]
