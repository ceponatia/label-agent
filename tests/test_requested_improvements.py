import sqlite3
from email.message import EmailMessage
from pathlib import Path

import pymupdf as fitz
from fastapi.testclient import TestClient

from labelagent import ingest as ingest_module
from labelagent.config import Config
from labelagent.db import Database
from labelagent.ingest import ImapFetcher, poll_once
from labelagent.models import Label, LabelStatus, Level, Stage
from labelagent.web.app import StubController, create_app

FIXTURES = Path(__file__).parent / "fixtures"


class FakeFetcher:
    def __init__(self, items):
        self.items = list(items)
        self.processed: list[str] = []

    def fetch_candidates(self):
        return list(self.items)

    def mark_processed(self, uid: str) -> None:
        self.processed.append(uid)


def _database(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init()
    return db


def _outage_email() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Poshmark <orders@poshmark.com>"
    msg["To"] = "elaine@example.com"
    msg["Subject"] = '"Blue Sweater" just sold to @buyer on Poshmark!'
    msg["Message-ID"] = "<posh-delay-1@poshmark.com>"
    msg.set_content(
        "Currently our shipping label system is experiencing delays. "
        "Your pre-paid, pre-addressed shipping label will be automatically mailed "
        "to you when the service is available. You do not have to do anything to "
        "receive the label."
    )
    return bytes(msg)


def test_poshmark_label_delay_becomes_a_non_actionable_error_event(tmp_path):
    db = _database(tmp_path)
    config = Config(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "test.db"))
    fetcher = FakeFetcher([("delay", _outage_email())])
    try:
        result = poll_once(db, config, fetcher)

        assert result.checked == 1
        assert result.skipped == 1
        assert result.new_labels == []
        assert result.errors == []
        assert db.list_labels() == []
        assert fetcher.processed == ["delay"]

        events = db.list_events(level=Level.ERROR, stage=Stage.INGEST)
        assert len(events) == 1
        assert "Poshmark could not generate the shipping label" in events[0].message
        assert "No action is needed" in events[0].message
        assert "automatically" in events[0].message
    finally:
        db.close()


def test_real_poshmark_fixture_saves_buyer_name(tmp_path):
    db = _database(tmp_path)
    config = Config(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "test.db"))
    raw = (FIXTURES / "poshmark-direct.eml").read_bytes()
    try:
        result = poll_once(db, config, FakeFetcher([("posh", raw)]))
        assert result.new_labels == [1]
        assert db.get_label(1).buyer_name == "Vanessa Chavez"
    finally:
        db.close()


def test_recipient_name_can_fall_back_to_pdf_text(tmp_path):
    path = tmp_path / "label.pdf"
    doc = fitz.open()
    page = doc.new_page(width=288, height=432)
    page.insert_text((24, 40), "SHIP TO:\nJANE DOE\n123 MAIN ST", fontsize=12)
    doc.save(str(path))
    doc.close()

    assert ingest_module._buyer_from_pdf(path) == "Jane Doe"


def test_database_round_trips_buyer_name(tmp_path):
    db = _database(tmp_path)
    try:
        label = db.insert_label(
            Label(
                item_title="Blue Sweater",
                buyer_name="Vanessa Chavez",
                gmail_message_id="buyer-test",
                status=LabelStatus.PRINTED,
            )
        )
        assert db.get_label(label.id).buyer_name == "Vanessa Chavez"
    finally:
        db.close()


def test_database_migrates_existing_labels_table(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE labels (
          id INTEGER PRIMARY KEY,
          platform TEXT,
          item_title TEXT,
          order_ref TEXT,
          tracking_number TEXT,
          ship_by TEXT,
          gmail_message_id TEXT UNIQUE,
          email_received_at TEXT,
          status TEXT,
          status_detail TEXT,
          original_path TEXT,
          print_path TEXT,
          print_count INTEGER DEFAULT 0,
          created_at TEXT,
          printed_at TEXT
        );
        """
    )
    legacy.close()

    db = Database(path)
    try:
        db.init()
        columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(labels)")}
        assert "buyer_name" in columns
    finally:
        db.close()


def test_dashboard_live_refreshes_counts_and_shows_buyer(tmp_path):
    config = Config(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "test.db"))
    db = Database(config.db_path)
    db.init()
    try:
        db.insert_label(
            Label(
                item_title="Blue Sweater",
                buyer_name="Vanessa Chavez",
                gmail_message_id="dashboard-buyer",
                status=LabelStatus.PRINTED,
            )
        )
        client = TestClient(create_app(db, config, StubController()))
        body = client.get("/").text

        assert 'id="today-counts"' in body
        assert 'hx-trigger="every 3s"' in body
        assert 'hx-get="/api/dashboard/counts"' in body
        # Rows read "{buyer}: {item}", split over two lines: the buyer leads
        # and the item sits under it in the smaller row-item style.
        assert "Vanessa Chavez:" in body
        assert '<span class="row-item">Blue Sweater</span>' in body

        fragment = client.get("/api/dashboard/counts")
        assert fragment.status_code == 200
        assert 'id="today-counts"' in fragment.text
        assert "printed today" in fragment.text
    finally:
        db.close()


def _vision_stub(name):
    return lambda png, config: {"ok": True, "problems": [], "ship_to_name": name}


def test_pipeline_reads_buyer_name_off_the_label_via_vision(tmp_path, monkeypatch):
    """Vinted's email never names the buyer; the label's ship-to line does."""
    from labelagent import verify as verify_module
    from labelagent.printing import FilePrinter
    from labelagent.service import AgentService

    config = Config(
        data_dir=str(tmp_path / "data"),
        db_path=str(tmp_path / "test.db"),
        anthropic_api_key="k",
    )
    db = _database(tmp_path)
    monkeypatch.setattr(verify_module, "call_vision_api", _vision_stub("SARA EXAMPLE"))
    raw = (FIXTURES / "vinted-direct.eml").read_bytes()
    service = AgentService(
        db,
        config,
        printer=FilePrinter(tmp_path / "printed"),
        fetcher_factory=lambda cfg: FakeFetcher([("vinted", raw)]),
    )
    try:
        service.check_now()
        assert db.get_label(1).buyer_name == "Sara Example"
    finally:
        db.close()


def test_backfill_fills_existing_labels_without_printing(tmp_path, monkeypatch):
    from labelagent import verify as verify_module
    from labelagent.printing import FilePrinter
    from labelagent.service import AgentService

    config = Config(
        data_dir=str(tmp_path / "data"),
        db_path=str(tmp_path / "test.db"),
        anthropic_api_key="k",
    )
    db = _database(tmp_path)
    pdf = tmp_path / "print.pdf"
    doc = fitz.open()
    doc.new_page(width=288, height=432)
    doc.save(str(pdf))
    doc.close()
    db.insert_label(
        Label(
            item_title="Blue Sweater",
            gmail_message_id="backfill-1",
            status=LabelStatus.PRINTED,
            print_path=str(pdf),
        )
    )
    monkeypatch.setattr(verify_module, "call_vision_api", _vision_stub("Amy Buyer"))
    service = AgentService(
        db,
        config,
        printer=FilePrinter(tmp_path / "printed"),
        fetcher_factory=lambda cfg: FakeFetcher([]),
    )
    try:
        result = service.backfill_buyer_names()

        assert result["checked"] == 1
        assert result["filled"] == 1
        assert result["problems"] == []
        label = db.get_label(1)
        assert label.buyer_name == "Amy Buyer"
        assert label.status == LabelStatus.PRINTED
        assert list((tmp_path / "printed").glob("*")) == []
    finally:
        db.close()


def test_backfill_reports_why_each_label_was_not_filled(tmp_path, monkeypatch):
    """"0 of 5 filled" with no reasons is undebuggable; every miss must say why."""
    from labelagent import verify as verify_module
    from labelagent.printing import FilePrinter
    from labelagent.service import AgentService

    config = Config(
        data_dir=str(tmp_path / "data"),
        db_path=str(tmp_path / "test.db"),
        anthropic_api_key="k",
    )
    db = _database(tmp_path)
    pdf = tmp_path / "print.pdf"
    doc = fitz.open()
    doc.new_page(width=288, height=432)
    doc.save(str(pdf))
    doc.close()
    db.insert_label(
        Label(gmail_message_id="miss-1", status=LabelStatus.PRINTED)
    )
    db.insert_label(
        Label(gmail_message_id="miss-2", status=LabelStatus.PRINTED, print_path=str(pdf))
    )

    def exploding(png, config):
        raise RuntimeError("invalid x-api-key")

    monkeypatch.setattr(verify_module, "call_vision_api", exploding)
    service = AgentService(
        db,
        config,
        printer=FilePrinter(tmp_path / "printed"),
        fetcher_factory=lambda cfg: FakeFetcher([]),
    )
    try:
        result = service.backfill_buyer_names()

        assert result["filled"] == 0
        assert result["checked"] == 2
        assert sorted(result["problems"]) == [
            "label 1: no pdf on disk to read",
            "label 2: vision call failed: invalid x-api-key",
        ]
        warnings = db.list_events(level=Level.WARN)
        assert any("buyer-name backfill" in event.message for event in warnings)
    finally:
        db.close()


def test_backfill_without_api_key_says_why_it_cannot_run(tmp_path, monkeypatch):
    from labelagent import verify as verify_module
    from labelagent.printing import FilePrinter
    from labelagent.service import AgentService

    def must_not_run(png, config):
        raise AssertionError("the vision call ran without an API key")

    monkeypatch.setattr(verify_module, "call_vision_api", must_not_run)
    config = Config(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "test.db"))
    db = _database(tmp_path)
    service = AgentService(
        db,
        config,
        printer=FilePrinter(tmp_path / "printed"),
        fetcher_factory=lambda cfg: FakeFetcher([]),
    )
    try:
        result = service.backfill_buyer_names()
        assert result["filled"] == 0
        assert "API key" in result["detail"]
    finally:
        db.close()


def test_real_poll_query_includes_outage_notices_without_changing_default_query():
    config = Config(
        data_dir="unused",
        imap_user="elaine@example.com",
        imap_password="app-password",
    )
    fetcher = ImapFetcher(config, mailbox=object())

    assert "has:attachment" in fetcher.search_query()
    expanded = fetcher.search_query(include_label_delays=True)
    assert "has:attachment" in expanded
    assert '"shipping label system"' in expanded
    assert '"label service"' in expanded
