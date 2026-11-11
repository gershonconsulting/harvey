"""Gershon add-on: Harvey's leads are copied into Streak GC Campaigns as H01, deduped."""
import importlib.util
import json
import sqlite3
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(mod, "DB", tmp_path / "data" / "harvey.db")
    return mod


def make_db(path, convs=()):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE companies (id TEXT, name TEXT, domain TEXT, website TEXT, location TEXT,
            industry TEXT, source TEXT, source_url TEXT);
        CREATE TABLE prospects (id TEXT, company_id TEXT, first_name TEXT, last_name TEXT,
            email TEXT, linkedin_url TEXT, title TEXT, source TEXT, source_url TEXT,
            status TEXT, company TEXT, industry TEXT, created_at TEXT);
        CREATE TABLE conversations (id TEXT, prospect_id TEXT, intent TEXT, stage TEXT,
            thread_json TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE outbox (id TEXT, status TEXT, to_email TEXT, subject TEXT, error TEXT,
            sent_at TEXT, updated_at TEXT);
        CREATE TABLE processed_replies (reply_id TEXT);
        INSERT INTO companies VALUES ('c1', 'GoodCore Ltd', 'goodcore.co.uk', '', 'London',
            'Software', 'web_search', 'https://example.com/list');
        INSERT INTO companies VALUES ('c2', 'NewCo', 'newco.io', '', 'Paris', '', '', '');
        INSERT INTO companies VALUES ('c3', 'Junk', 'junk.io', '', '', '', '', '');
        INSERT INTO prospects VALUES ('p1', 'c1', 'Sam', 'Lee', 'sam@goodcore.co.uk', '',
            'CEO', '', '', 'queued', '', '', '2026-11-09 10:00:00');
        INSERT INTO prospects VALUES ('p2', 'c2', 'Ana', 'Ruiz', 'ana@newco.io',
            'https://linkedin.com/in/ana', 'Founder', '', '', 'new', '', '', '2026-11-10 09:00:00');
        INSERT INTO prospects VALUES ('p3', 'c3', 'Bot', '', 'support@junk.io', '', '', '', '',
            'unfit', '', '', '2026-11-10 09:00:00');
        INSERT INTO processed_replies VALUES ('m1'), ('m2'), ('m3');
    """)
    for c in convs:
        con.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?)", c)
    con.commit()
    con.close()


class FakeStreak:
    def __init__(self, existing):
        self.existing = existing
        self.calls = []
        self.n = 0

    def boxes(self, pipeline):
        self.calls.append(("boxes", pipeline))
        return self.existing

    def create(self, pipeline, name, stage):
        self.n += 1
        self.calls.append(("create", name, stage))
        return {"boxKey": f"new{self.n}"}

    def update(self, box, **fields):
        self.calls.append(("update", box, fields))

    def set_field(self, box, field, value):
        self.calls.append(("field", box, field, value))

    def comment(self, box, text):
        self.calls.append(("comment", box, text))


def run(mod, monkeypatch, fake, tmp_path):
    monkeypatch.setattr(mod, "STATE", tmp_path / "data" / "gershon_streak.json")
    monkeypatch.setattr(mod, "Streak", lambda key, dry=False: fake)
    return mod.sync({"STREAK_API_KEY": "k"})


def test_new_leads_created_existing_reused_unfit_skipped(tmp_path, monkeypatch):
    mod = load("gershon_streak", tmp_path, monkeypatch)
    make_db(mod.DB)
    # GoodCore already in the pipeline as H01 (the manual backfill)
    fake = FakeStreak([{"boxKey": "gc", "name": "GoodCore", "fields": {"1002": "9213"}}])
    done = run(mod, monkeypatch, fake, tmp_path)
    assert done == {"created": 1, "adopted": 1, "updated": 2, "responded": 0}
    creates = [c for c in fake.calls if c[0] == "create"]
    assert creates == [("create", "NewCo", "5001")]
    assert ("field", "new1", "1002", "9213") in fake.calls
    notes = {c[1]: c[2]["notes"] for c in fake.calls if c[0] == "update"}
    assert "Campaign H01" in notes["new1"] and "ana@newco.io" in notes["new1"]
    assert "https://linkedin.com/in/ana" in notes["new1"]
    assert "sam@goodcore.co.uk" in notes["gc"]
    assert not any("Junk" in str(c) for c in fake.calls)


def test_second_run_changes_nothing(tmp_path, monkeypatch):
    mod = load("gershon_streak", tmp_path, monkeypatch)
    make_db(mod.DB)
    run(mod, monkeypatch, FakeStreak([]), tmp_path)
    fake = FakeStreak([])
    done = run(mod, monkeypatch, fake, tmp_path)
    assert done == {"created": 0, "adopted": 0, "updated": 0, "responded": 0}
    assert fake.calls == []


def test_other_campaign_box_left_alone(tmp_path, monkeypatch):
    mod = load("gershon_streak", tmp_path, monkeypatch)
    make_db(mod.DB)
    fake = FakeStreak([{"boxKey": "cc", "name": "Goodcore", "fields": {"1002": "9212"}}])
    run(mod, monkeypatch, fake, tmp_path)
    assert not any(c[0] in ("update", "field") and c[1] == "cc" for c in fake.calls)
    assert any(c[0] == "comment" and c[1] == "cc" for c in fake.calls)


def test_interested_reply_moves_box_to_responded(tmp_path, monkeypatch):
    mod = load("gershon_streak", tmp_path, monkeypatch)
    make_db(mod.DB)
    run(mod, monkeypatch, FakeStreak([]), tmp_path)
    con = sqlite3.connect(mod.DB)
    con.execute("INSERT INTO conversations VALUES ('v1','p2','interested','presenting','[]','open','')")
    con.commit()
    con.close()
    fake = FakeStreak([])
    done = run(mod, monkeypatch, fake, tmp_path)
    assert done["responded"] == 1
    assert ("update", "new2", {"stageKey": "5002"}) in fake.calls


def test_no_key_does_nothing(tmp_path, monkeypatch):
    mod = load("gershon_streak", tmp_path, monkeypatch)
    make_db(mod.DB)
    assert mod.sync({}) == {"error": "no key"}


def test_alert_counts_real_prospect_replies_not_inbox(tmp_path, monkeypatch):
    rep = load("gershon_reports", tmp_path, monkeypatch)
    monkeypatch.setattr(rep, "STATE", tmp_path / "data" / "gershon_alerts.json")
    monkeypatch.setattr(rep, "ROOT", tmp_path)
    monkeypatch.setattr(rep, "sh", lambda *a, **k: "active")
    monkeypatch.setattr(rep, "load_env", dict)
    monkeypatch.setattr(rep.sys, "argv", ["x", "alerts"])
    sent = []
    monkeypatch.setattr(rep, "send", lambda env, subj, text: sent.append((subj, text)) or True)
    make_db(rep.DB, convs=[("v0", "p1", "", "initial_outreach",
                            json.dumps([{"sender": "harvey", "content": "hi"}]), "open", "")])
    rep.main()  # first run only records the baseline
    con = sqlite3.connect(rep.DB)
    con.execute("INSERT INTO processed_replies VALUES ('m4'), ('m5')")  # newsletters
    con.commit()
    rep.main()
    assert sent == []  # inbox mail is not a prospect reply
    con.execute("INSERT INTO conversations VALUES ('v1','p2','question','initial_outreach',?,"
                "'open','')", (json.dumps([{"sender": "prospect", "content": "tell me more"}]),))
    con.commit()
    con.close()
    rep.main()
    assert len(sent) == 1 and "1 new prospect reply" in sent[0][0]
    assert "Inbox messages checked" in sent[0][1]
