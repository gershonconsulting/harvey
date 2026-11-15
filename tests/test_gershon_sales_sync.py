"""Gershon add-on: scripts/gershon_sales_sync.py (Harvey -> sales.gershon.ai)."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from harvey.state import StateManager

SPEC = importlib.util.spec_from_file_location(
    "gershon_sales_sync", Path(__file__).resolve().parent.parent / "scripts" / "gershon_sales_sync.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "harvey.db"
    monkeypatch.setattr(sync, "DB", path)
    monkeypatch.setattr(sync, "DONE", tmp_path / "done.json")
    state = StateManager(str(path))

    async def seed():
        await state.init_db()
        async with state._connect() as c:
            await c.execute("INSERT INTO companies (id, name, domain) VALUES ('c1', 'Example Co', 'example.com')")
            await c.execute("INSERT INTO prospects (id, company_id, first_name, email) "
                            "VALUES ('p1', 'c1', 'Ana', 'ana@example.com')")
            await c.commit()
        draft = await state.add_outbox_item(prospect_id="p1", to_email="ana@example.com", subject="s",
                                            body="b", send_at="2026-01-01T10:00:00", campaign_id="k1", step=1)
        sent = await state.add_outbox_item(prospect_id="p1", to_email="ana@example.com", subject="first",
                                           body="first body", send_at="2026-01-01T09:00:00", campaign_id="k0",
                                           step=1, status="sent")
        await state.update_outbox_item(sent, message_id="<m1@example.com>", thread_ref="t1",
                                       sent_at="2026-01-01T09:00:00")
        return draft
    draft = asyncio.run(seed())
    return state, draft


def status(state, oid):
    return asyncio.run(state.get_outbox_item(oid))


def test_snapshot_reads_everything(db):
    snap = sync.snapshot()
    assert [c["name"] for c in snap["companies"]] == ["Example Co"]
    assert snap["prospects"][0]["email"] == "ana@example.com"
    assert len(snap["outbox"]) == 2 and snap["full"] is True


def test_edit_then_approve_one_email(db):
    state, draft = db
    res = asyncio.run(sync.apply_actions([
        {"id": 1, "type": "edit", "outbox_id": draft, "subject": "new s", "body": "new b"},
        {"id": 2, "type": "approve", "outbox_id": draft},
    ]))
    assert [r["ok"] for r in res] == [True, True]
    item = status(state, draft)
    assert (item["subject"], item["body"], item["status"]) == ("new s", "new b", "approved")


def test_approve_without_id_never_approves_all(db):
    state, draft = db
    res = asyncio.run(sync.apply_actions([{"id": 3, "type": "approve", "outbox_id": ""}]))
    assert res[0]["ok"] is False
    assert status(state, draft)["status"] == "pending_review"


def test_edit_after_approval_is_refused(db):
    state, draft = db
    asyncio.run(state.approve_outbox(draft))
    res = asyncio.run(sync.apply_actions([{"id": 4, "type": "edit", "outbox_id": draft, "subject": "x", "body": "y"}]))
    assert res[0]["ok"] is False and status(state, draft)["body"] == "b"


def test_followup_waits_for_approval_threaded_and_once(db):
    state, _ = db
    act = {"id": 5, "type": "followup", "prospect_id": "p1", "subject": "Re: first", "body": "hello again"}
    assert asyncio.run(sync.apply_actions([act]))[0]["ok"] is True
    again = asyncio.run(sync.apply_actions([act]))[0]
    assert again["detail"] == "already applied"
    items = [r for r in sync.rows("SELECT * FROM outbox WHERE kind = 'reply'")]
    assert len(items) == 1
    assert items[0]["status"] == "pending_review" and items[0]["in_reply_to"] == "<m1@example.com>"
    assert items[0]["thread_ref"] == "t1"
    assert json.loads(sync.DONE.read_text()) == [5]


def test_reject(db):
    state, draft = db
    assert asyncio.run(sync.apply_actions([{"id": 6, "type": "reject", "outbox_id": draft}]))[0]["ok"]
    assert status(state, draft)["status"] == "rejected"


def test_no_token_pushes_nothing(db, monkeypatch, capsys):
    monkeypatch.setattr(sync, "load_env", lambda: {})
    monkeypatch.setattr(sync, "post", lambda *a: pytest.fail("must not post"))
    assert sync.main(["x", "sync"]) == 1
