"""Gershon add-on: full drafts waiting for approval are emailed once each."""
import importlib.util
import json
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gershon_reports.py"


def load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gershon_reports", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(mod, "DB", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(mod, "STATE", tmp_path / "data" / "gershon_alerts.json")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE companies (id TEXT, name TEXT, website TEXT);
        CREATE TABLE prospects (id TEXT, first_name TEXT, last_name TEXT, title TEXT,
            email TEXT, linkedin_url TEXT, company TEXT, company_id TEXT, status TEXT);
        CREATE TABLE outbox (id TEXT, prospect_id TEXT, to_email TEXT, subject TEXT,
            body TEXT, step INTEGER, kind TEXT, status TEXT, error TEXT DEFAULT '',
            sent_at TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE conversations (id TEXT, prospect_id TEXT, intent TEXT, stage TEXT,
            thread_json TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE processed_replies (id TEXT);
        INSERT INTO companies VALUES ('c1', 'Acme GmbH', 'acme.de');
        INSERT INTO prospects VALUES ('p1', 'Anna', 'Berg', 'CEO', 'anna@acme.de', '', '', 'c1', 'new');
    """)
    for r in rows:
        con.execute("INSERT INTO outbox (id, prospect_id, to_email, subject, body, step, kind,"
                    " status, created_at) VALUES (?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()


def test_drafts_emailed_in_full_once(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    body = "Hi Anna,\n\nLong body line one.\nLine two with the whole pitch."
    make_db(mod.DB, [("o1", "p1", "anna@acme.de", "US sales for Acme", body, 1, "sequence",
                      "pending_review", "2026-11-09 10:00:00"),
                     ("o2", "p1", "anna@acme.de", "Sent one", "x", 2, "sequence", "sent",
                      "2026-11-09 09:00:00")])
    sent = []
    monkeypatch.setattr(mod, "send", lambda env, subj, text: sent.append((subj, text)) or True)
    monkeypatch.setattr(mod, "sh", lambda cmd, timeout=120: "active")
    monkeypatch.setattr(mod, "load_env", lambda: {"GMAIL_DRAFTS": "0"})
    monkeypatch.setattr(mod.sys, "argv", ["x", "alerts"])

    mod.main()
    drafts = [m for m in sent if "draft" in m[0]]
    assert len(drafts) == 1
    subj, text = drafts[0]
    assert "1 new draft" in subj and "Acme GmbH" in subj
    assert "To: Anna Berg <anna@acme.de>" in text
    assert "Company: Acme GmbH acme.de" in text
    assert "Subject: US sales for Acme" in text
    assert body in text
    assert "Sent one" not in text
    assert json.loads(mod.STATE.read_text())["drafts_notified"] == ["o1"]

    sent.clear()
    mod.main()  # same draft still pending: not emailed again
    assert not [m for m in sent if "draft" in m[0]]


def test_drafts_mode_sends_all_waiting(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    make_db(mod.DB, [(f"o{i}", "p1", "anna@acme.de", f"S{i}", f"B{i}", 1, "sequence",
                      "pending_review", f"2026-11-09 10:0{i}:00") for i in range(1, 4)])
    sent = []
    monkeypatch.setattr(mod, "send", lambda env, subj, text: sent.append((subj, text)) or True)
    monkeypatch.setattr(mod, "sh", lambda cmd, timeout=120: "active")
    monkeypatch.setattr(mod, "load_env", lambda: {"GMAIL_DRAFTS": "0"})
    monkeypatch.setattr(mod.sys, "argv", ["x", "drafts"])
    mod.main()
    assert len(sent) == 1
    assert "3 waiting draft(s)" in sent[0][0]
    assert "DRAFT 3 of 3" in sent[0][1] and "B2" in sent[0][1]


def test_with_gmail_review_no_draft_email_and_no_waiting_alert(tmp_path, monkeypatch):
    """Default: review happens in Gmail Drafts, so the alerts run sends neither."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gr", Path(__file__).parent.parent / "scripts" / "gershon_reports.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.gmail_review({}) and not mod.gmail_review({"GMAIL_DRAFTS": "0"})
    s = {"pending_review": 3, "service": "active", "brain_ready": True, "sent_24h": 0,
         "open_conversations": 0, "replies_total": 0, "outbox": {}, "prospects": {},
         "errors": [], "status_text": ""}
    assert mod.GMAIL_DRAFTS_LINK in mod.fmt(s, {})
    assert mod.GMAIL_DRAFTS_LINK not in mod.fmt(s, {"GMAIL_DRAFTS": "0"})
