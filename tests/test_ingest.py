from pathlib import Path

import pytest

from labelagent import ingest as ingest_module
from labelagent.classify import Classification
from labelagent.config import Config
from labelagent.db import Database
from labelagent.ingest import (
    LAST_POLL_KEY,
    PROCESSED_LABEL,
    ImapFetcher,
    IngestError,
    make_fetcher,
    poll_once,
)
from labelagent.models import LabelStatus, Level, Stage

FIXTURES = Path(__file__).parent / "fixtures"

POSHMARK_ITEM = "Kate Spade Tinsel Small Dome Crossbody Bag Rose Gold Glitter Sparkle"
POSHMARK_ORDER = "6a6e119205255dd754e8b790"
POSHMARK_TRACKING = "9434650208104113715936"
VINTED_TRACKING = "9434636208303484184646"
VINTED_SHIP_BY = "2026-08-10T02:00:00"

ALL_FIXTURES = [
    "poshmark-direct",
    "vinted-direct",
    "poshmark-forwarded",
    "vinted-forwarded",
    "not-a-label",
    "unrelated",
]


def raw(name: str) -> bytes:
    return (FIXTURES / f"{name}.eml").read_bytes()


def messages(*names: str) -> list[tuple[str, bytes]]:
    return [(name, raw(name)) for name in (names or tuple(ALL_FIXTURES))]


class FakeFetcher:
    def __init__(self, items, fail: Exception | None = None):
        self.items = list(items)
        self.fail = fail
        self.processed: list[str] = []
        self.fetches = 0

    def fetch_candidates(self):
        self.fetches += 1
        if self.fail is not None:
            raise self.fail
        return list(self.items)

    def mark_processed(self, uid: str) -> None:
        self.processed.append(uid)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init()
    yield database
    database.close()


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "test.db"))


# --- poll_once ---------------------------------------------------------------


def test_poll_ingests_labels_and_skips_the_rest(db, config):
    fetcher = FakeFetcher(messages())
    result = poll_once(db, config, fetcher)

    assert result.checked == 6
    assert result.errors == []
    # the forwarded copies carry the same tracking numbers as the direct ones
    assert len(result.new_labels) == 2
    assert len(result.duplicates) == 2
    assert result.skipped == 2
    assert fetcher.processed == ALL_FIXTURES


def test_ingested_rows_have_the_expected_fields(db, config):
    poll_once(db, config, FakeFetcher(messages("poshmark-direct", "vinted-direct")))

    poshmark, vinted = sorted(db.list_labels(), key=lambda row: row.id)
    assert poshmark.platform == "poshmark"
    assert poshmark.item_title == POSHMARK_ITEM
    assert poshmark.order_ref == POSHMARK_ORDER
    assert poshmark.tracking_number == POSHMARK_TRACKING
    assert poshmark.ship_by is None
    assert poshmark.gmail_message_id == "<posh-direct-1@poshmark.com>"
    assert poshmark.email_received_at.startswith("2026-08-01T11:32:47")
    assert poshmark.status == LabelStatus.INGESTED
    assert poshmark.status_detail is None

    assert vinted.platform == "vinted"
    assert vinted.tracking_number == VINTED_TRACKING
    assert vinted.order_ref == "21272887347"
    assert vinted.ship_by == VINTED_SHIP_BY
    assert vinted.email_received_at.startswith("2026-07-31T23:28:35")


def test_original_pdfs_are_written_to_disk(db, config):
    poll_once(db, config, FakeFetcher(messages("poshmark-direct", "vinted-direct")))

    for label in db.list_labels():
        path = Path(label.original_path)
        assert path == Path(config.data_dir) / "labels" / str(label.id) / "original.pdf"
        assert path.read_bytes().startswith(b"%PDF")


def test_forwarded_copies_with_new_tracking_are_new_labels(db, config):
    items = messages("poshmark-direct", "vinted-direct")
    items.append(
        (
            "poshmark-forwarded",
            raw("poshmark-forwarded").replace(
                POSHMARK_TRACKING.encode(), b"9400111899223197428490"
            ),
        )
    )
    items.append(
        (
            "vinted-forwarded",
            raw("vinted-forwarded").replace(
                VINTED_TRACKING.encode(), b"9400122899223197428491"
            ),
        )
    )
    result = poll_once(db, config, FakeFetcher(items))

    assert len(result.new_labels) == 4
    assert result.duplicates == []
    assert [label.platform for label in sorted(db.list_labels(), key=lambda r: r.id)] == [
        "poshmark",
        "vinted",
        "poshmark",
        "vinted",
    ]
    assert db.get_label(3).item_title == POSHMARK_ITEM
    assert db.get_label(3).tracking_number == "9400111899223197428490"


def test_second_poll_of_the_same_messages_changes_nothing(db, config):
    poll_once(db, config, FakeFetcher(messages()))
    before = len(db.list_labels())

    second = poll_once(db, config, FakeFetcher(messages()))
    assert second.new_labels == []
    assert second.duplicates == []
    assert second.skipped == 6
    assert len(db.list_labels()) == before


def test_same_tracking_from_a_different_message_is_a_duplicate(db, config):
    resent = raw("poshmark-direct").replace(
        b"<posh-direct-1@poshmark.com>", b"<posh-direct-2@poshmark.com>"
    )
    result = poll_once(
        db,
        config,
        FakeFetcher([("1", raw("poshmark-direct")), ("2", resent)]),
    )

    assert len(result.new_labels) == 1
    assert len(result.duplicates) == 1

    duplicate = db.get_label(result.duplicates[0])
    assert duplicate.status == LabelStatus.DUPLICATE
    assert duplicate.tracking_number == POSHMARK_TRACKING
    assert "label 1" in duplicate.status_detail
    assert Path(duplicate.original_path).read_bytes().startswith(b"%PDF")

    warnings = db.list_events(level=Level.WARN, stage=Stage.INGEST)
    assert len(warnings) == 1
    assert warnings[0].label_id == duplicate.id

    # a third copy dedupes against the original, not against the duplicate row
    third = raw("poshmark-direct").replace(
        b"<posh-direct-1@poshmark.com>", b"<posh-direct-3@poshmark.com>"
    )
    again = poll_once(db, config, FakeFetcher([("3", third)]))
    assert len(again.duplicates) == 1
    assert db.get_label(again.duplicates[0]).status_detail.endswith("label 1")


def test_ingest_events_are_recorded(db, config):
    result = poll_once(db, config, FakeFetcher(messages("poshmark-direct")))

    events = db.list_events(stage=Stage.INGEST, level=Level.INFO)
    assert len(events) == 1
    assert events[0].label_id == result.new_labels[0]
    assert "poshmark" in events[0].message
    assert "heuristic" in events[0].message


def test_non_label_classification_skips_but_marks_processed(db, config, monkeypatch):
    monkeypatch.setattr(
        ingest_module,
        "classify_email",
        lambda c, cfg: Classification(
            is_label_email=False, confidence=0.42, source="llm"
        ),
    )
    fetcher = FakeFetcher(messages("poshmark-direct"))
    result = poll_once(db, config, fetcher)

    assert result.skipped == 1
    assert result.new_labels == []
    assert db.list_labels() == []
    assert fetcher.processed == ["poshmark-direct"]
    events = db.list_events(stage=Stage.INGEST, level=Level.INFO)
    assert "not a label email" in events[0].message


def test_fetcher_failure_is_reported_not_raised(db, config):
    fetcher = FakeFetcher([], fail=OSError("connection reset"))
    result = poll_once(db, config, fetcher)

    assert result.checked == 0
    assert result.new_labels == []
    assert len(result.errors) == 1
    assert "connection reset" in result.errors[0]

    errors = db.list_events(level=Level.ERROR, stage=Stage.SYSTEM)
    assert len(errors) == 1
    assert "connection reset" in errors[0].message
    assert db.get_setting(LAST_POLL_KEY) is not None


def test_one_bad_message_does_not_abort_the_batch(db, config, monkeypatch):
    real_parse = ingest_module.parse_email

    def flaky(data, uid=""):
        if uid == "boom":
            raise ValueError("malformed MIME")
        return real_parse(data, uid)

    monkeypatch.setattr(ingest_module, "parse_email", flaky)
    fetcher = FakeFetcher([("boom", raw("poshmark-direct")), *messages("vinted-direct")])
    result = poll_once(db, config, fetcher)

    assert result.checked == 2
    assert len(result.new_labels) == 1
    assert len(result.errors) == 1
    assert "malformed MIME" in result.errors[0]
    assert "boom" not in fetcher.processed
    assert fetcher.processed == ["vinted-direct"]
    assert len(db.list_events(level=Level.ERROR, stage=Stage.INGEST)) == 1


def test_attachment_failure_leaves_the_message_unprocessed(db, config, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ingest_module.storage, "original_pdf_path", boom)
    fetcher = FakeFetcher(messages("poshmark-direct"))
    result = poll_once(db, config, fetcher)

    assert result.new_labels == []
    assert len(result.errors) == 1
    assert fetcher.processed == []

    label = db.get_label(1)
    assert label.status == LabelStatus.FAILED
    assert "disk full" in label.status_detail
    assert label.original_path is None
    assert len(db.list_events(level=Level.ERROR, stage=Stage.INGEST)) == 1


def test_last_poll_at_is_updated(db, config):
    assert db.get_setting(LAST_POLL_KEY) is None
    poll_once(db, config, FakeFetcher(messages("poshmark-direct")))
    stamp = db.get_setting(LAST_POLL_KEY)
    assert stamp is not None and stamp[10] == "T"


# --- ImapFetcher -------------------------------------------------------------


class StubClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls: list[tuple] = []

    def uid(self, command, *args):
        self.calls.append((command, *args))
        return self.responses.get(command, ("OK", [b""]))


class StubMailbox:
    def __init__(self, client):
        self.client = client
        self.logged_out = False

    def logout(self):
        self.logged_out = True


@pytest.fixture
def imap_config() -> Config:
    return Config(
        data_dir="unused",
        imap_host="imap.gmail.com",
        imap_user="elaineamy.g2010@gmail.com",
        imap_password="app-password",
        imap_folder="INBOX",
    )


def test_search_query_is_built_from_config(imap_config):
    fetcher = ImapFetcher(imap_config, mailbox=StubMailbox(StubClient()))
    assert fetcher.search_query() == (
        "(from:poshmark.com OR from:vinted.com) has:attachment "
        "-label:label-agent/processed"
    )


def test_search_uids_uses_gmail_raw(imap_config):
    client = StubClient({"SEARCH": ("OK", [b"101 102 103"])})
    fetcher = ImapFetcher(imap_config, mailbox=StubMailbox(client))

    assert fetcher.search_uids() == ["101", "102", "103"]
    assert client.calls == [
        (
            "SEARCH",
            "X-GM-RAW",
            '"(from:poshmark.com OR from:vinted.com) has:attachment '
            '-label:label-agent/processed"',
        )
    ]


def test_search_uids_handles_an_empty_mailbox(imap_config):
    client = StubClient({"SEARCH": ("OK", [b""])})
    assert ImapFetcher(imap_config, mailbox=StubMailbox(client)).search_uids() == []


def test_fetch_raw_peeks_at_the_body(imap_config):
    body = raw("poshmark-direct")
    client = StubClient(
        {"FETCH": ("OK", [(b"1 (UID 101 BODY[] {123}", body), b")"])}
    )
    fetcher = ImapFetcher(imap_config, mailbox=StubMailbox(client))

    assert fetcher.fetch_raw("101") == body
    assert client.calls == [("FETCH", "101", "(BODY.PEEK[])")]


def test_fetch_candidates_pairs_uids_with_raw_messages(imap_config):
    body = raw("vinted-direct")
    client = StubClient(
        {
            "SEARCH": ("OK", [b"7"]),
            "FETCH": ("OK", [(b"1 (UID 7 BODY[] {1}", body), b")"]),
        }
    )
    fetcher = ImapFetcher(imap_config, mailbox=StubMailbox(client))
    assert fetcher.fetch_candidates() == [("7", body)]


def test_mark_processed_adds_the_gmail_label(imap_config):
    client = StubClient({"STORE": ("OK", [b"1 (UID 101)"])})
    ImapFetcher(imap_config, mailbox=StubMailbox(client)).mark_processed("101")
    assert client.calls == [
        ("STORE", "101", "+X-GM-LABELS", f"({PROCESSED_LABEL})")
    ]


@pytest.mark.parametrize(
    "command, action",
    [
        ("SEARCH", lambda f: f.search_uids()),
        ("FETCH", lambda f: f.fetch_raw("1")),
        ("STORE", lambda f: f.mark_processed("1")),
    ],
)
def test_imap_errors_raise(command, action, imap_config):
    client = StubClient({command: ("NO", [b"nope"])})
    with pytest.raises(IngestError, match=command):
        action(ImapFetcher(imap_config, mailbox=StubMailbox(client)))


def test_fetch_raw_without_a_body_raises(imap_config):
    client = StubClient({"FETCH": ("OK", [b")"])})
    fetcher = ImapFetcher(imap_config, mailbox=StubMailbox(client))
    with pytest.raises(IngestError, match="no message body"):
        fetcher.fetch_raw("9")


def test_close_logs_out(imap_config):
    mailbox = StubMailbox(StubClient())
    fetcher = ImapFetcher(imap_config, mailbox=mailbox)
    fetcher.close()
    assert mailbox.logged_out is True
    fetcher.close()  # idempotent


def test_make_fetcher_does_not_connect(imap_config):
    fetcher = make_fetcher(imap_config)
    assert isinstance(fetcher, ImapFetcher)
    assert fetcher._mailbox is None


def test_poll_once_drives_an_imap_fetcher(db, config, imap_config):
    body = raw("poshmark-direct")
    client = StubClient(
        {
            "SEARCH": ("OK", [b"101"]),
            "FETCH": ("OK", [(b"1 (UID 101 BODY[] {1}", body), b")"]),
            "STORE": ("OK", [b"1 (UID 101)"]),
        }
    )
    fetcher = ImapFetcher(imap_config, mailbox=StubMailbox(client))
    result = poll_once(db, config, fetcher)

    assert len(result.new_labels) == 1
    assert ("STORE", "101", "+X-GM-LABELS", f"({PROCESSED_LABEL})") in client.calls


# --- regressions -------------------------------------------------------------


def test_a_label_whose_attachment_never_saved_is_recovered_next_poll(
    db, config, monkeypatch
):
    """The message must not be marked processed while its PDF is missing."""
    fetcher = FakeFetcher(messages("poshmark-direct"))

    real_write = Path.write_bytes
    monkeypatch.setattr(
        Path,
        "write_bytes",
        lambda self, data: (_ for _ in ()).throw(OSError("No space left on device")),
    )
    first = poll_once(db, config, fetcher)

    assert first.errors and "No space left" in first.errors[0]
    assert db.get_label(1).status == LabelStatus.FAILED
    assert db.get_label(1).original_path is None
    # crucially: Gmail must still hand us this message again
    assert fetcher.processed == []

    monkeypatch.setattr(Path, "write_bytes", real_write)
    second = poll_once(db, config, fetcher)

    assert second.new_labels == [1]
    label = db.get_label(1)
    assert label.status == LabelStatus.INGESTED
    assert Path(label.original_path).is_file()
    assert fetcher.processed == ["poshmark-direct"]
    assert db.list_events(stage=Stage.INGEST, level=Level.WARN, label_id=1)


def test_a_pruned_printed_label_is_not_resurrected(db, config):
    """Retention deletes the PDF of a printed label; that is not a failure."""
    fetcher = FakeFetcher(messages("poshmark-direct"))
    poll_once(db, config, fetcher)

    Path(db.get_label(1).original_path).unlink()
    db.update_label(1, original_path=None, print_path=None)
    db.update_status(1, LabelStatus.PRINTED)

    again = poll_once(db, config, FakeFetcher(messages("poshmark-direct")))

    assert again.new_labels == []
    assert again.skipped == 1
    assert db.get_label(1).status == LabelStatus.PRINTED
    assert db.get_label(1).original_path is None
