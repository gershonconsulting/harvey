"""Gershon add-on: the "N waiting" alert is rate limited; red cross only for failures."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gershon_reports.py"
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_mod():
    spec = importlib.util.spec_from_file_location("gershon_reports_once", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def add_pending(con, i):
    con.execute("INSERT INTO outbox (id, prospect_id, to_email, subject, body, step, kind, status,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"o{i}", "p1", "anna@acme.de", f"S{i}", f"B{i}", 1, "sequence",
                 "pending_review", f"2026-11-09 10:{i:02d}:00"))


def setup(tmp_path, monkeypatch, n, state, brain_ok=True, mode="alerts"):
    mod = load_mod()
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(mod, "DB", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(mod, "STATE", tmp_path / "data" / "gershon_alerts.json")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    con = sqlite3.connect(mod.DB)
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
        INSERT INTO prospects VALUES ('p1', 'Anna', 'Berg', 'CEO', 'anna@acme.de', '', 'Acme', '', 'new');
    """)
    for i in range(n):
        add_pending(con, i)
    con.commit()
    con.close()
    base = {"service": "active", "brain_ready": True, "pending_review": 0, "replies_total": 0,
            "errors": [], "leads_notified": [], "drafts_notified": [f"o{i}" for i in range(n)]}
    base.update(state)
    mod.STATE.write_text(json.dumps(base))
    sent = []
    monkeypatch.setattr(mod, "send", lambda env, subj, text: sent.append(subj) or True)
    monkeypatch.setattr(mod, "sh", lambda cmd, timeout=120:
                        "active" if cmd[0] == "systemctl" else ("ok" if brain_ok else "NOT READY"))
    monkeypatch.setattr(mod, "load_env", lambda: {"GMAIL_DRAFTS": "0"})
    monkeypatch.setattr(mod.sys, "argv", ["x", mode])
    return mod, sent


def test_due_logic():
    f = load_mod().waiting_alert_due
    assert f(33, 30, {}, "d1")                                                      # first of the day
    assert not f(33, 32, {"waiting_sent_date": "d1", "waiting_sent_n": 33}, "d1")   # flicker
    assert f(34, 33, {"waiting_sent_date": "d1", "waiting_sent_n": 33}, "d1")       # grew
    assert f(33, 32, {"waiting_sent_date": "d0", "waiting_sent_n": 33}, "d1")       # new day
    assert not f(33, 33, {}, "d1")                                                  # no growth


def test_waiting_alert_once_per_day_then_on_growth(tmp_path, monkeypatch):
    mod, sent = setup(tmp_path, monkeypatch, 33, {"pending_review": 30})
    mod.main()
    assert len(sent) == 1 and sent[0].startswith("ℹ️") and "❌" not in sent[0]
    st = json.loads(mod.STATE.read_text())
    assert (st["waiting_sent_date"], st["waiting_sent_n"]) == (TODAY, 33)

    # count dipped to 32 in the last run and is back at 33: no second mail
    st["pending_review"] = 32
    mod.STATE.write_text(json.dumps(st))
    sent.clear()
    mod.main()
    assert sent == []

    # real growth to 34 the same day is allowed
    con = sqlite3.connect(mod.DB)
    add_pending(con, 33)
    con.commit()
    con.close()
    st = json.loads(mod.STATE.read_text())
    st["drafts_notified"].append("o33")
    st["pending_review"] = 33
    mod.STATE.write_text(json.dumps(st))
    sent.clear()
    mod.main()
    assert len(sent) == 1 and "34" in sent[0]


def test_new_day_allows_one_more(tmp_path, monkeypatch):
    mod, sent = setup(tmp_path, monkeypatch, 33, {"pending_review": 32,
                      "waiting_sent_date": "2000-01-01", "waiting_sent_n": 33})
    mod.main()
    assert len(sent) == 1


def test_brain_failure_gets_red_cross(tmp_path, monkeypatch):
    mod, sent = setup(tmp_path, monkeypatch, 2, {"pending_review": 2}, brain_ok=False)
    mod.main()
    assert len(sent) == 1 and sent[0].startswith("❌") and "brain" in sent[0]


def test_info_alert_has_no_red_cross(tmp_path, monkeypatch):
    mod, sent = setup(tmp_path, monkeypatch, 0, {})
    con = sqlite3.connect(mod.DB)
    con.execute("INSERT INTO processed_replies VALUES ('r1')")
    con.commit()
    con.close()
    mod.main()
    assert len(sent) == 1 and sent[0].startswith("ℹ️") and "❌" not in sent[0]


def test_daily_flag(tmp_path, monkeypatch):
    for name, n, ok, mark in (("a", 5, True, "ℹ️"), ("b", 5, False, "❌"), ("c", 0, True, "✅")):
        mod, sent = setup(tmp_path / name, monkeypatch, n, {}, brain_ok=ok, mode="daily") \
            if (tmp_path / name).mkdir() is None else (None, None)
        mod.main()
        assert sent[0].startswith(mark), (name, sent)
