"""Gershon add-on: the weekly report covers Monday-Sunday and draws charts."""
import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gershon_weekly.py"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)  # Monday 08:00 Sint Maarten


def load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gershon_weekly", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "DB", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(mod, "GMAIL_STATE", tmp_path / "data" / "gmail_drafts.json")
    con = sqlite3.connect(tmp_path / "data" / "harvey.db")
    con.executescript("""
        CREATE TABLE companies (id TEXT, name TEXT, domain TEXT, website TEXT, location TEXT, created_at TEXT);
        CREATE TABLE prospects (id TEXT, company_id TEXT, first_name TEXT, last_name TEXT, title TEXT,
            email TEXT, email_status TEXT, company TEXT, status TEXT, created_at TEXT);
        CREATE TABLE outbox (id TEXT, prospect_id TEXT, to_email TEXT, step INTEGER, kind TEXT,
            status TEXT, error TEXT DEFAULT '', sent_at TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE conversations (id TEXT, prospect_id TEXT, intent TEXT, stage TEXT,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE settings (key TEXT, value TEXT);
    """)
    con.executemany("INSERT INTO companies VALUES (?,?,?,?,?,?)", [
        ("c1", "Acme GmbH", "acme.de", "", "Berlin", "2026-11-17 15:00:00"),
        ("c2", "Bolt SAS", "bolt.fr", "", "Paris", "2026-11-23 03:30:00"),   # Sun 22 in SXM
        ("c3", "Old Ltd", "old.uk", "", "London", "2026-11-10 10:00:00"),    # week before
        ("c4", "Today Co", "today.io", "", "", "2026-11-23 11:00:00"),       # this week, excluded
    ])
    con.executemany("INSERT INTO prospects VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("p1", "c1", "Anna", "Berg", "CEO", "anna@acme.de", "verified", "", "queued", "2026-11-17 15:05:00"),
        ("p2", "c2", "Bo", "Li", "CTO", "bo@bolt.fr", "risky", "", "new", "2026-11-18 03:31:00"),
    ])
    con.executemany("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("o1", "p1", "anna@acme.de", 1, "sequence", "sent", "", "2026-11-19 14:00:00",
         "2026-11-18 16:00:00", "2026-11-19 14:00:00"),
    ])
    con.execute("INSERT INTO conversations VALUES ('v1','p1','interested','presenting',"
                "'2026-11-20 10:00:00','2026-11-20 10:00:00')")
    con.commit()
    con.close()
    return mod


def test_week_window_and_kpis(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    d = mod.collect({}, now=NOW, probe=False)
    assert (d["start"], d["end"]) == ("2026-11-16", "2026-11-22")
    assert d["kpi"]["companies"] == (2, 1)
    assert d["kpi"]["sent"] == (1, 0) and d["kpi"]["replies"] == (1, 0)
    assert [c["name"] for c in d["companies"]] == ["Acme GmbH", "Bolt SAS"]
    assert d["companies"][0]["sent"] and d["target"] == 70
    assert d["quality"] == {"verified": 1, "risky": 1}


def test_html_has_charts_and_design(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    d = mod.collect({}, now=NOW, probe=False)
    page = mod.render_html(d)
    assert page.count("https://quickchart.io/chart?") == 4
    assert "Harvey weekly report" in page and mod.RED in page
    assert "Target missed" in page and "Anna Berg" in page
    assert '"horizontalBar"' in unquote(mod.chart_funnel(d))
    subj = mod.subject(d, False)
    assert subj.startswith("❌ Harvey H01 weekly · 16–22 Nov 2026") and "2/70 companies" in subj
    assert "Acme GmbH" in mod.render_text(d)
