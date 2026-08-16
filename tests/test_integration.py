"""SPEC §11 end-to-end: six fixture emails in, two printed 4x6 labels out."""

import re
from datetime import date
from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from labelagent.config import Config
from labelagent.db import Database
from labelagent.models import LabelStatus
from labelagent.printing import FilePrinter
from labelagent.scheduler import POLL_JOB_ID, build_scheduler
from labelagent.service import AgentService
from labelagent.web.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date.today().isoformat()

POSHMARK_TRACKING = "9434650208104113715936"
VINTED_TRACKING = "9434636208303484184646"

ALL_FIXTURES = [
    "poshmark-direct",
    "vinted-direct",
    "poshmark-forwarded",
    "vinted-forwarded",
    "not-a-label",
    "unrelated",
]

DECODE_DPI = 150


class FakeFetcher:
    """Re-delivers every message on every poll, the worst case for dedupe."""

    def __init__(self, items):
        self.items = list(items)
        self.processed: list[str] = []
        self.fetches = 0

    def fetch_candidates(self):
        self.fetches += 1
        return list(self.items)

    def mark_processed(self, uid: str) -> None:
        self.processed.append(uid)


def decode(pdf_path: Path, dpi: int = DECODE_DPI) -> list[str]:
    import numpy as np
    from PIL import Image
    from pyzbar import pyzbar

    with fitz.open(pdf_path) as doc:
        pixmap = doc[0].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    data = np.frombuffer(pixmap.samples, dtype=np.uint8)
    gray = data.reshape(pixmap.height, pixmap.stride)[:, : pixmap.width]
    return [c.data.decode("utf-8", "replace") for c in pyzbar.decode(Image.fromarray(gray))]


def carries(payloads: list[str], tracking: str) -> bool:
    return any(tracking in re.sub(r"\D", "", payload) for payload in payloads)


def page_size(pdf_path: Path) -> tuple[float, float]:
    with fitz.open(pdf_path) as doc:
        assert doc.page_count == 1
        return doc[0].rect.width, doc[0].rect.height


@pytest.fixture
def env(tmp_path):
    """A whole agent: real db, real pipeline, fake mailbox, file printer."""
    config = Config(
        data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "data" / "labelagent.db")
    )
    db = Database(config.db_path)
    db.init()
    printer = FilePrinter(tmp_path / "printed")
    fetcher = FakeFetcher(
        [(name, (FIXTURES / f"{name}.eml").read_bytes()) for name in ALL_FIXTURES]
    )
    service = AgentService(
        db, config, printer=printer, fetcher_factory=lambda cfg: fetcher
    )
    yield service, db, config, printer, fetcher
    db.close()


def test_six_emails_produce_two_printed_labels(env):
    service, db, _config, printer, fetcher = env
    summary = service.check_now()

    assert summary["checked"] == 6
    assert summary["new"] == 2
    assert summary["duplicates"] == 2
    assert summary["printed_or_queued"] == 2
    assert summary["errors"] == []
    assert fetcher.processed == ALL_FIXTURES

    printed = db.list_labels(status=LabelStatus.PRINTED)
    duplicates = db.list_labels(status=LabelStatus.DUPLICATE)
    assert len(printed) == 2
    assert len(duplicates) == 2
    # the two non-label emails never became rows at all
    assert len(db.list_labels()) == 4

    assert {label.platform for label in printed} == {"poshmark", "vinted"}
    assert all(label.print_count == 1 for label in printed)
    assert len(list(printer.out_dir.glob("*.pdf"))) == 2


def test_printed_pdfs_are_4x6_and_carry_the_right_barcode(env):
    service, db, _config, printer, _fetcher = env
    service.check_now()

    printed = db.list_labels(status=LabelStatus.PRINTED)
    by_tracking = {label.tracking_number: label for label in printed}
    assert set(by_tracking) == {POSHMARK_TRACKING, VINTED_TRACKING}

    for tracking, label in by_tracking.items():
        path = Path(label.print_path)
        assert page_size(path) == (288.0, 432.0)
        assert carries(decode(path), tracking)

    # and what actually reached the printer is the same file
    for copy in printer.out_dir.glob("*.pdf"):
        assert page_size(copy) == (288.0, 432.0)
    printed_payloads = [
        payload for copy in printer.out_dir.glob("*.pdf") for payload in decode(copy)
    ]
    assert carries(printed_payloads, POSHMARK_TRACKING)
    assert carries(printed_payloads, VINTED_TRACKING)


def test_daily_metrics_report_todays_prints(env):
    service, db, _config, _printer, _fetcher = env
    service.check_now()

    metrics = db.daily_metrics(TODAY, TODAY)
    assert metrics[0]["date"] == TODAY
    assert metrics[0]["printed"] == 2
    assert metrics[0]["created"] == 4
    assert metrics[0]["failed"] == 0
    assert metrics[0]["needs_review"] == 0


def test_a_second_check_changes_nothing(env):
    service, db, _config, printer, _fetcher = env
    service.check_now()
    before = [(label.id, label.status, label.print_count) for label in db.list_labels()]
    events_before = len(db.list_events(limit=1000))

    second = service.check_now()
    assert second["checked"] == 6  # the mailbox handed us the same six messages
    assert second["new"] == 0
    assert second["duplicates"] == 0
    assert second["printed_or_queued"] == 0
    assert second["errors"] == []

    assert [(l.id, l.status, l.print_count) for l in db.list_labels()] == before
    assert len(db.list_events(limit=1000)) == events_before
    assert len(list(printer.out_dir.glob("*.pdf"))) == 2


# --- the web app on top of the real service ----------------------------------


def test_the_dashboard_shows_the_printed_labels(env):
    service, db, config, _printer, _fetcher = env
    service.check_now()

    with TestClient(create_app(db, config, service)) as client:
        body = client.get("/").text

    for label in db.list_labels(status=LabelStatus.PRINTED):
        assert label.item_title in body
    assert "Reprint" in body
    assert "file printer" in body


def test_reprinting_from_the_api_prints_again(env):
    service, db, config, printer, _fetcher = env
    service.check_now()
    label = db.list_labels(status=LabelStatus.PRINTED)[0]

    with TestClient(create_app(db, config, service)) as client:
        response = client.post(f"/api/labels/{label.id}/print")
        assert response.status_code == 200
        assert response.json()["ok"] is True

        assert db.get_label(label.id).print_count == 2
        assert len(list(printer.out_dir.glob("*.pdf"))) == 3

        status = client.get("/api/status").json()
        assert status["agent_state"] == "running"
        assert status["auto_print"] is True
        assert status["printer_available"] is True
        assert status["today"]["printed"] == 2


def test_pausing_from_the_api_stops_printing(env):
    service, db, config, printer, fetcher = env

    with TestClient(create_app(db, config, service)) as client:
        assert client.post("/api/agent/pause").json()["agent_state"] == "paused"
        client.post("/api/agent/check-now")

        assert db.list_labels(status=LabelStatus.PRINTED) == []
        assert len(db.list_labels(status=LabelStatus.READY)) == 2
        assert list(printer.out_dir.glob("*.pdf")) == []

        client.post("/api/agent/resume")
        client.post("/api/agent/check-now")
        assert len(db.list_labels(status=LabelStatus.PRINTED)) == 2
        assert len(list(printer.out_dir.glob("*.pdf"))) == 2


def test_previews_and_pdfs_are_served_for_a_real_label(env):
    service, db, config, _printer, _fetcher = env
    service.check_now()
    label = db.list_labels(status=LabelStatus.PRINTED)[0]

    with TestClient(create_app(db, config, service)) as client:
        assert client.get(f"/api/labels/{label.id}/print.pdf").status_code == 200
        assert client.get(f"/api/labels/{label.id}/original.pdf").status_code == 200
        preview = client.get(f"/api/labels/{label.id}/preview.png")
        assert preview.status_code == 200
        assert preview.content[:4] == b"\x89PNG"
        assert client.get(f"/labels/{label.id}").status_code == 200


def test_saving_the_interval_retimes_the_live_poll_job(env):
    """Settings -> db -> the running scheduler, with no restart in between."""
    service, db, config, _printer, _fetcher = env
    scheduler = build_scheduler(service, config)
    scheduler.start()

    def poll_minutes() -> float:
        return scheduler.get_job(POLL_JOB_ID).trigger.interval.total_seconds() / 60

    try:
        assert poll_minutes() == config.poll_interval_min

        with TestClient(create_app(db, config, service)) as client:
            response = client.post(
                "/api/settings",
                data={"poll_interval_min": "8"},
                follow_redirects=False,
            )
            assert response.status_code == 303

        assert db.get_setting("poll_interval_min") == "8"
        assert poll_minutes() == 8
    finally:
        scheduler.shutdown(wait=False)


def test_the_test_print_button_works_end_to_end(env):
    service, db, config, printer, _fetcher = env

    with TestClient(create_app(db, config, service)) as client:
        result = client.post("/api/agent/test-print").json()

    assert result["ok"] is True
    calibration = [p for p in printer.out_dir.glob("*test-print.pdf")]
    assert len(calibration) == 1
    assert page_size(calibration[0]) == (288.0, 432.0)
