"""Gmail Drafts as Harvey's review place: what each sync run decides."""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "gershon_gmail_drafts", Path(__file__).parent.parent / "scripts" / "gershon_gmail_drafts.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

ROW = {"status": "pending_review", "subject": "Hello", "body": "Hi Jane", "to_email": "j@x.com"}
ENTRY = {"draft": "d1", "thread": "t1", "hash": g.content_hash("Hello", "Hi Jane")}
SENT = {"id": "m1", "message_id": "<m1@x>", "thread": "t1", "sent_at": "2026-11-13T10:00:00"}


def test_waiting_draft_still_in_gmail_is_kept():
    assert g.decide(ROW, ENTRY, True, None) == "keep"


def test_sent_from_gmail_is_recorded_as_sent():
    assert g.decide(ROW, ENTRY, False, SENT) == "mark_sent"


def test_deleted_in_gmail_is_rejected():
    assert g.decide(ROW, ENTRY, False, None) == "dismiss"


def test_edited_in_harvey_replaces_the_draft():
    assert g.decide(dict(ROW, body="Hi Jane, new"), ENTRY, True, None) == "replace_draft"


def test_taken_over_draft_without_hash_is_not_replaced():
    assert g.decide(dict(ROW, body="other"), dict(ENTRY, hash=""), True, None) == "keep"


def test_rejected_or_approved_elsewhere_removes_the_gmail_draft():
    for status in ("rejected", "approved", "cancelled", "sent"):
        assert g.decide(dict(ROW, status=status), ENTRY, True, None) == "delete_draft"
    assert g.decide(None, ENTRY, True, None) == "delete_draft"


def test_gone_everywhere_is_forgotten():
    assert g.decide(dict(ROW, status="rejected"), ENTRY, False, None) == "forget"


def test_follow_up_waits_for_the_previous_send_and_its_delay():
    now = datetime(2026, 11, 20)
    prev = {"status": "sent", "sent_at": (now - timedelta(days=3)).isoformat()}
    assert g.follow_up_ready({}, prev, 3, now)
    assert not g.follow_up_ready({}, prev, 4, now)
    assert not g.follow_up_ready({}, dict(prev, status="pending_review"), 3, now)
    assert not g.follow_up_ready({}, None, 3, now)
    # never sooner than the minimum gap, even with a 0-day plan
    assert not g.follow_up_ready({}, {"status": "sent", "sent_at": (now - timedelta(days=1)).isoformat()}, 0, now)
