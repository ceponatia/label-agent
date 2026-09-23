import sqlite3

import pytest

from labelagent.db import Database, now_iso
from labelagent.models import Label, LabelStatus, Level, Platform, Stage


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init()
    yield database
    database.close()


def make_label(**kwargs) -> Label:
    defaults = dict(
        platform=Platform.POSHMARK,
        item_title="Blue Sweater",
        order_ref="ORD-1",
        tracking_number="9400111",
        gmail_message_id="msg-1",
    )
    defaults.update(kwargs)
    return Label(**defaults)


def test_init_is_idempotent(tmp_path):
    database = Database(tmp_path / "x.db")
    database.init()
    database.init()
    assert database.get_label(1) is None
    database.close()


def test_wal_mode_enabled(db):
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_insert_fills_id_and_created_at(db):
    label = db.insert_label(make_label())
    assert label.id == 1
    assert label.created_at is not None
    assert label.status == LabelStatus.INGESTED
    assert label.print_count == 0

    fetched = db.get_label(1)
    assert fetched.item_title == "Blue Sweater"
    assert fetched.tracking_number == "9400111"


def test_insert_preserves_supplied_created_at(db):
    stamp = "2026-07-30T09:15:00"
    label = db.insert_label(make_label(created_at=stamp))
    assert db.get_label(label.id).created_at == stamp


def test_gmail_message_id_is_unique(db):
    db.insert_label(make_label())
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_label(make_label(tracking_number="9400222"))


def test_platform_check_constraint(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_label(make_label(platform="depop"))


def test_status_check_constraint(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_label(make_label(status="teleported"))


def test_status_transitions(db):
    label = db.insert_label(make_label())

    updated = db.update_status(label.id, LabelStatus.PROCESSING)
    assert updated.status == LabelStatus.PROCESSING
    assert updated.status_detail is None

    updated = db.update_status(label.id, LabelStatus.NEEDS_REVIEW, "barcode cut off")
    assert updated.status == LabelStatus.NEEDS_REVIEW
    assert updated.status_detail == "barcode cut off"

    updated = db.update_status(label.id, LabelStatus.READY)
    assert updated.status_detail is None


def test_update_label_whitelists_columns(db):
    label = db.insert_label(make_label())
    updated = db.update_label(label.id, print_path="/tmp/print.pdf", bogus="nope")
    assert updated.print_path == "/tmp/print.pdf"
    assert not hasattr(updated, "bogus")


def test_update_label_with_no_known_fields_is_a_noop(db):
    label = db.insert_label(make_label())
    assert db.update_label(label.id, nonsense=1).item_title == "Blue Sweater"


def test_mark_printed(db):
    label = db.insert_label(make_label())

    printed = db.mark_printed(label.id)
    assert printed.status == LabelStatus.PRINTED
    assert printed.printed_at is not None
    assert printed.print_count == 1

    reprinted = db.mark_printed(label.id)
    assert reprinted.print_count == 2


def test_find_by_tracking_returns_duplicates_in_order(db):
    first = db.insert_label(make_label())
    second = db.insert_label(
        make_label(gmail_message_id="msg-2", status=LabelStatus.DUPLICATE)
    )

    found = db.find_by_tracking("9400111")
    assert [label.id for label in found] == [first.id, second.id]
    assert found[1].status == LabelStatus.DUPLICATE
    assert db.find_by_tracking("nope") == []


def test_find_by_gmail_message_id(db):
    db.insert_label(make_label())
    assert db.find_by_gmail_message_id("msg-1").item_title == "Blue Sweater"
    assert db.find_by_gmail_message_id("unknown") is None


def test_list_labels_filters(db):
    today = now_iso()[:10]
    db.insert_label(make_label(gmail_message_id="a", tracking_number="a"))
    db.insert_label(
        make_label(
            gmail_message_id="b",
            tracking_number="b",
            platform=Platform.VINTED,
            status=LabelStatus.NEEDS_REVIEW,
        )
    )
    old = db.insert_label(
        make_label(
            gmail_message_id="c", tracking_number="c", created_at="2020-01-01T00:00:00"
        )
    )

    assert len(db.list_labels()) == 3
    assert len(db.list_labels(date=today)) == 2
    assert [label.id for label in db.list_labels(date="2020-01-01")] == [old.id]
    assert [label.status for label in db.list_labels(status=LabelStatus.NEEDS_REVIEW)] == [
        LabelStatus.NEEDS_REVIEW
    ]
    assert len(db.list_labels(limit=1)) == 1


def test_list_labels_by_date_matches_printed_at(db):
    label = db.insert_label(make_label(created_at="2020-01-01T00:00:00"))
    db.update_label(label.id, printed_at="2026-03-04T10:00:00")

    assert [entry.id for entry in db.list_labels(date="2026-03-04")] == [label.id]
    assert [entry.id for entry in db.list_labels(date="2020-01-01")] == [label.id]


def test_events(db):
    label = db.insert_label(make_label())

    db.add_event(Stage.INGEST, Level.INFO, "downloaded attachment", label.id)
    db.add_event(Stage.CROP, Level.ERROR, "no bounding box", label.id)
    system = db.add_event(Stage.SYSTEM, Level.ERROR, "gmail unreachable")

    assert system.id is not None
    assert system.label_id is None
    assert system.created_at is not None

    assert len(db.list_events()) == 3
    assert len(db.list_events(level=Level.ERROR)) == 2
    assert len(db.list_events(stage=Stage.CROP)) == 1
    assert len(db.list_events(label_id=label.id)) == 2
    assert len(db.list_events(level=Level.ERROR, label_id=label.id)) == 1
    assert len(db.list_events(limit=1)) == 1
    assert db.list_events()[0].message == "gmail unreachable"


def test_settings(db):
    assert db.get_setting("agent_state") is None
    assert db.get_setting("agent_state", "running") == "running"

    db.set_setting("agent_state", "paused")
    assert db.get_setting("agent_state") == "paused"

    db.set_setting("agent_state", "running")
    assert db.get_setting("agent_state") == "running"

    db.set_setting("poll_interval_min", 5)
    assert db.get_setting("poll_interval_min") == "5"


def test_daily_metrics(db):
    a = db.insert_label(
        make_label(gmail_message_id="a", tracking_number="a", created_at="2026-03-01T09:00:00")
    )
    db.update_label(a.id, status=LabelStatus.PRINTED, printed_at="2026-03-01T09:05:00")

    b = db.insert_label(
        make_label(gmail_message_id="b", tracking_number="b", created_at="2026-03-01T11:00:00")
    )
    db.update_status(b.id, LabelStatus.NEEDS_REVIEW, "barcode cut off")

    c = db.insert_label(
        make_label(gmail_message_id="c", tracking_number="c", created_at="2026-03-02T08:00:00")
    )
    db.update_status(c.id, LabelStatus.FAILED, "cups error")

    # created one day, printed the next
    d = db.insert_label(
        make_label(gmail_message_id="d", tracking_number="d", created_at="2026-03-02T23:50:00")
    )
    db.update_label(d.id, status=LabelStatus.PRINTED, printed_at="2026-03-03T00:10:00")

    rows = db.daily_metrics("2026-03-01", "2026-03-03")
    by_date = {row["date"]: row for row in rows}

    assert [row["date"] for row in rows] == ["2026-03-01", "2026-03-02", "2026-03-03"]
    assert by_date["2026-03-01"] == {
        "date": "2026-03-01",
        "printed": 1,
        "failed": 0,
        "needs_review": 1,
        "created": 2,
    }
    assert by_date["2026-03-02"]["created"] == 2
    assert by_date["2026-03-02"]["failed"] == 1
    assert by_date["2026-03-02"]["printed"] == 0
    assert by_date["2026-03-03"]["printed"] == 1
    assert by_date["2026-03-03"]["created"] == 0


def test_daily_metrics_excludes_days_outside_range(db):
    db.insert_label(make_label(created_at="2026-02-01T09:00:00"))
    assert db.daily_metrics("2026-03-01", "2026-03-03") == []


def test_now_iso_is_second_precision_local_time():
    stamp = now_iso()
    assert len(stamp) == 19
    assert stamp[10] == "T"
