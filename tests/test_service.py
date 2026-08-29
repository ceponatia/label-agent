from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pymupdf as fitz
import pytest

from labelagent.config import Config
from labelagent.db import Database
from labelagent.ingest import LAST_POLL_KEY
from labelagent.models import LabelStatus, Level, Stage
from labelagent.printing import FilePrinter, JobStatus, PrinterUnavailable
from labelagent.scheduler import (
    POLL_JOB_ID,
    PRUNE_INTERVAL_HOURS,
    RETRY_INTERVAL_MIN,
    build_scheduler,
    poll_interval_min,
    retry_and_reap,
)
from labelagent.service import (
    AGENT_STATE_KEY,
    AUTO_PRINT_KEY,
    JOB_PREFIX,
    POLL_INTERVAL_KEY,
    AgentService,
    write_calibration_pdf,
)
from labelagent.web.app import StubController

FIXTURES = Path(__file__).parent / "fixtures"

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


class OfflinePrinter:
    """A printer that is off until `available_flag` is flipped."""

    def __init__(self, available_flag: bool = False):
        self.available_flag = available_flag
        self.submitted: list[str] = []

    def submit(self, pdf_path: str) -> str:
        if not self.available_flag:
            raise PrinterUnavailable("printer is off")
        self.submitted.append(pdf_path)
        return f"job-{len(self.submitted)}"

    def job_status(self, job_id: str) -> JobStatus:
        return JobStatus.COMPLETED if self.available_flag else JobStatus.UNREACHABLE

    def available(self) -> bool:
        return self.available_flag


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init()
    yield database
    database.close()


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "test.db"))


@pytest.fixture
def printer(tmp_path) -> FilePrinter:
    return FilePrinter(tmp_path / "printed")


def make_service(db, config, printer, *names, fetcher=None) -> AgentService:
    fetcher = fetcher if fetcher is not None else FakeFetcher(messages(*names))
    return AgentService(db, config, printer=printer, fetcher_factory=lambda cfg: fetcher)


def printed_files(printer: FilePrinter) -> list[Path]:
    return sorted(printer.out_dir.glob("*.pdf"))


# --- happy path --------------------------------------------------------------


def test_check_now_takes_a_label_all_the_way_to_printed(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    summary = service.check_now()

    assert summary["checked"] == 1
    assert summary["new"] == 1
    assert summary["printed_or_queued"] == 1
    assert summary["needs_review"] == 0
    assert summary["errors"] == []

    label = db.get_label(1)
    assert label.status == LabelStatus.PRINTED
    assert label.print_count == 1
    assert label.printed_at is not None
    assert label.status_detail is None
    assert Path(label.print_path).is_file()
    assert len(printed_files(printer)) == 1
    assert db.get_setting(LAST_POLL_KEY) is not None


def test_the_whole_pipeline_is_recorded_in_events(db, config, printer):
    make_service(db, config, printer, "vinted-direct").check_now()

    stages = {event.stage for event in db.list_events(label_id=1)}
    assert stages == {Stage.INGEST, Stage.CROP, Stage.VERIFY, Stage.PRINT}
    assert db.list_events(level=Level.ERROR) == []

    verify_event = db.list_events(stage=Stage.VERIFY, label_id=1)[0]
    assert VINTED_TRACKING in verify_event.message


def test_check_now_is_idempotent(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct", "vinted-direct")
    service.check_now()
    second = service.check_now()

    assert second["new"] == 0
    assert second["printed_or_queued"] == 0
    assert second["detail"] == "No new label emails."
    assert len(db.list_labels()) == 2
    assert len(printed_files(printer)) == 2


# --- pause / auto-print ------------------------------------------------------


def test_paused_agent_prepares_labels_but_prints_nothing(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    summary = service.check_now()

    assert summary["new"] == 1
    assert summary["printed_or_queued"] == 0
    assert db.get_label(1).status == LabelStatus.READY
    assert printed_files(printer) == []

    service.resume()
    service.check_now()
    assert db.get_label(1).status == LabelStatus.PRINTED
    assert len(printed_files(printer)) == 1


def test_pause_and_resume_persist_and_log(db, config, printer):
    service = make_service(db, config, printer)

    service.pause()
    assert db.get_setting(AGENT_STATE_KEY) == "paused"
    assert service.state()["agent_state"] == "paused"

    service.resume()
    assert db.get_setting(AGENT_STATE_KEY) == "running"
    assert service.state()["agent_state"] == "running"

    messages_logged = [e.message for e in db.list_events(stage=Stage.SYSTEM)]
    assert any("paused" in m for m in messages_logged)
    assert any("resumed" in m for m in messages_logged)


def test_auto_print_off_holds_labels_until_asked(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.set_auto_print(False)

    service.check_now()
    assert db.get_setting(AUTO_PRINT_KEY) == "off"
    assert service.state()["auto_print"] is False
    assert db.get_label(1).status == LabelStatus.READY
    assert printed_files(printer) == []

    result = service.print_label(1)
    assert result["ok"] is True
    assert db.get_label(1).status == LabelStatus.PRINTED
    assert len(printed_files(printer)) == 1


def test_runtime_state_is_read_live_from_settings(db, config, printer):
    service = make_service(db, config, printer)
    assert service.state()["agent_state"] == "running"

    # the web layer writes the same keys directly
    db.set_setting(AGENT_STATE_KEY, "paused")
    db.set_setting(AUTO_PRINT_KEY, "off")
    assert service.state()["agent_state"] == "paused"
    assert service.state()["auto_print"] is False


def test_manual_print_works_while_paused(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()

    assert service.print_label(1)["ok"] is True
    assert db.get_label(1).status == LabelStatus.PRINTED


# --- needs review ------------------------------------------------------------


def blank_label_eml(tmp_path: Path) -> bytes:
    """A Poshmark label email whose attachment is an empty letter page."""
    pdf_path = tmp_path / "blank-label.pdf"
    doc = fitz.open()
    doc.new_page(width=792, height=612)
    doc.save(str(pdf_path))
    doc.close()

    msg = EmailMessage()
    msg["From"] = "Poshmark <orders@poshmark.com>"
    msg["To"] = "gelaine@umich.edu"
    msg["Subject"] = '"Empty Box" just sold to @nobody on Poshmark!'
    msg["Date"] = "Sat, 1 Aug 2026 12:00:00 -0400"
    msg["Message-ID"] = "<posh-blank-1@poshmark.com>"
    msg.set_content("Tracking Number\n9400111899223197428999\n\nOrder ID\nabc123def456\n")
    msg.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename="pre-paid mailing label 4x6.pdf",
    )
    return bytes(msg)


def test_a_blank_label_needs_review_and_is_not_printed(db, config, printer, tmp_path):
    fetcher = FakeFetcher([("blank", blank_label_eml(tmp_path))])
    service = make_service(db, config, printer, fetcher=fetcher)
    summary = service.check_now()

    assert summary["needs_review"] == 1
    assert summary["printed_or_queued"] == 0

    label = db.get_label(1)
    assert label.status == LabelStatus.NEEDS_REVIEW
    assert label.status_detail
    assert label.print_path and Path(label.print_path).is_file()
    assert printed_files(printer) == []

    warnings = db.list_events(stage=Stage.VERIFY, level=Level.WARN, label_id=1)
    assert len(warnings) == 1


def test_a_missing_original_fails_the_label(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()

    db.update_status(1, LabelStatus.INGESTED)
    Path(db.get_label(1).original_path).unlink()
    service.process_new()

    label = db.get_label(1)
    assert label.status == LabelStatus.FAILED
    assert "no original pdf" in label.status_detail
    assert db.list_events(level=Level.ERROR, stage=Stage.CROP)


# --- printer off -------------------------------------------------------------


def test_printer_off_parks_the_label_then_prints_on_return(db, config):
    printer = OfflinePrinter(available_flag=False)
    db_service = make_service(db, config, printer, "poshmark-direct")
    db_service.check_now()

    label = db.get_label(1)
    assert label.status == LabelStatus.WAITING_FOR_PRINTER
    assert "printer is off" in label.status_detail
    assert db.list_events(stage=Stage.PRINT, level=Level.WARN, label_id=1)
    assert db_service.state()["printer_available"] is False

    # nothing happens while it is still off
    assert db_service.retry_waiting() == []

    printer.available_flag = True
    assert db_service.retry_waiting() == [1]
    assert db.get_label(1).status == LabelStatus.QUEUED
    assert db_service.reap_jobs() == [1]
    assert db.get_label(1).status == LabelStatus.PRINTED
    assert printer.submitted


def test_a_failed_job_goes_back_to_waiting(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()

    service.printer = OfflinePrinter(available_flag=True)
    service.printer.job_status = lambda job_id: JobStatus.FAILED

    result = service.print_label(1)
    assert result["ok"] is True  # accepted by CUPS, then rejected by the printer
    assert db.get_label(1).status == LabelStatus.WAITING_FOR_PRINTER
    assert db.get_label(1).print_count == 0


def test_a_cancelled_job_is_never_counted_as_printed(db, config, printer):
    """Killing the job in CUPS used to read as a clean finish and mark it printed."""
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()

    service.printer = OfflinePrinter(available_flag=True)
    service.printer.job_status = lambda job_id: JobStatus.CANCELLED

    assert service.print_label(1)["ok"] is True  # CUPS took it, then it was killed
    label = db.get_label(1)
    assert label.status == LabelStatus.FAILED
    assert label.print_count == 0
    assert "cancelled at the printer" in label.status_detail
    assert db.list_events(level=Level.ERROR, stage=Stage.PRINT, label_id=1)

    # a cancel is a decision; retrying would send the label back behind her back
    assert service.retry_waiting() == []


def test_a_job_that_aged_out_of_cups_counts_as_printed(db, config, printer):
    """CUPS forgets finished jobs, which must not buy the parcel a second label."""
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()
    db.update_status(1, LabelStatus.PRINTING, f"{JOB_PREFIX}job-9")

    service.printer = OfflinePrinter(available_flag=True)
    service.printer.job_status = lambda job_id: JobStatus.UNKNOWN

    assert service.reap_jobs() == [1]
    label = db.get_label(1)
    assert label.status == LabelStatus.PRINTED
    assert label.print_count == 1
    assert label.status_detail is None
    assert service.retry_waiting() == []


def test_a_job_cups_never_queued_goes_back_to_waiting(db, config, printer):
    """Same missing job, but never seen printing: that one really did not print."""
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()
    db.update_status(1, LabelStatus.QUEUED, f"{JOB_PREFIX}job-9")

    service.printer = OfflinePrinter(available_flag=True)
    service.printer.job_status = lambda job_id: JobStatus.UNKNOWN

    assert service.reap_jobs() == []
    label = db.get_label(1)
    assert label.status == LabelStatus.WAITING_FOR_PRINTER
    assert label.print_count == 0


def test_a_pending_job_becomes_printing(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()
    db.update_status(1, LabelStatus.QUEUED, f"{JOB_PREFIX}job-9")

    service.printer = OfflinePrinter(available_flag=True)
    service.printer.job_status = lambda job_id: JobStatus.PENDING
    assert service.reap_jobs() == []

    label = db.get_label(1)
    assert label.status == LabelStatus.PRINTING
    assert label.status_detail == f"{JOB_PREFIX}job-9"


# --- duplicates and reprints -------------------------------------------------


def test_duplicates_are_never_auto_printed(db, config, printer):
    service = make_service(db, config, printer)
    summary = service.check_now()

    assert summary["duplicates"] == 2
    duplicates = db.list_labels(status=LabelStatus.DUPLICATE)
    assert len(duplicates) == 2
    assert all(label.print_path is None for label in duplicates)
    assert len(printed_files(printer)) == 2

    # ... but Elaine can still force one through
    result = service.print_original(duplicates[0].id)
    assert result["ok"] is True
    assert len(printed_files(printer)) == 3


def test_reprinting_a_printed_label_increments_the_counter(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.check_now()
    assert db.get_label(1).print_count == 1

    assert service.print_label(1)["ok"] is True
    label = db.get_label(1)
    assert label.status == LabelStatus.PRINTED
    assert label.print_count == 2
    assert len(printed_files(printer)) == 2


def test_a_vanished_print_pdf_fails_instead_of_retrying_forever(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()

    Path(db.get_label(1).print_path).unlink()
    service.resume()
    assert service.dispatch_prints() == []

    label = db.get_label(1)
    assert label.status == LabelStatus.FAILED
    assert "no print pdf" in label.status_detail
    assert service.dispatch_prints() == []
    assert len(db.list_events(level=Level.ERROR, stage=Stage.PRINT)) == 1


def test_printing_an_unknown_label_is_reported_not_raised(db, config, printer):
    result = make_service(db, config, printer).print_label(404)
    assert result["ok"] is False
    assert "not found" in result["detail"]


# --- mailbox errors ----------------------------------------------------------


def test_a_fetcher_that_cannot_be_built_is_an_error_not_a_crash(db, config, printer):
    def boom(cfg):
        raise OSError("no imap password")

    service = AgentService(db, config, printer=printer, fetcher_factory=boom)
    summary = service.check_now()

    assert len(summary["errors"]) == 1
    assert "no imap password" in summary["errors"][0]
    assert db.list_events(level=Level.ERROR, stage=Stage.SYSTEM)


def test_a_failing_poll_drops_the_cached_fetcher(db, config, printer):
    first = FakeFetcher([], fail=OSError("connection reset"))
    second = FakeFetcher(messages("poshmark-direct"))
    built = [first, second]
    service = AgentService(
        db, config, printer=printer, fetcher_factory=lambda cfg: built.pop(0)
    )

    assert service.check_now()["errors"]
    assert service.check_now()["new"] == 1
    assert built == []


# --- test print --------------------------------------------------------------


def test_test_print_makes_a_4x6_calibration_page(db, config, printer):
    result = make_service(db, config, printer).test_print()

    assert result["ok"] is True
    printed = printed_files(printer)
    assert len(printed) == 1
    with fitz.open(printed[0]) as doc:
        assert doc.page_count == 1
        assert (doc[0].rect.width, doc[0].rect.height) == (288.0, 432.0)
        assert "calibration" in doc[0].get_text()
    assert db.list_events(stage=Stage.PRINT, level=Level.INFO)


def test_test_print_reports_an_offline_printer(db, config):
    result = make_service(db, config, OfflinePrinter(available_flag=False)).test_print()
    assert result["ok"] is False
    assert "printer is off" in result["detail"]


def test_calibration_page_has_ticks_and_a_border(tmp_path):
    path = write_calibration_pdf(tmp_path / "cal.pdf")
    with fitz.open(path) as doc:
        drawings = doc[0].get_drawings()
    # one border rectangle, 16 crosshair arms, and half-inch ticks on 4 edges
    assert len(drawings) > 30


# --- controller contract -----------------------------------------------------


def test_state_matches_the_web_controller_shape(db, config, printer):
    state = make_service(db, config, printer).state()

    assert state.keys() == StubController().state().keys()
    assert state["agent_state"] == "running"
    assert state["auto_print"] is True
    assert state["printer_available"] is True
    assert state["printer_name"] == "file printer"
    assert state["last_poll_at"] is None


class CountingPrinter(OfflinePrinter):
    """An OfflinePrinter that records how often it was asked if it is up."""

    def __init__(self, available_flag: bool = True):
        super().__init__(available_flag=available_flag)
        self.checks = 0

    def available(self) -> bool:
        self.checks += 1
        return self.available_flag


def test_the_dashboard_reads_a_cached_printer_state(db, config):
    """state() runs every few seconds now; asking the printer shells out."""
    printer = CountingPrinter(available_flag=True)
    service = make_service(db, config, printer)

    for _ in range(5):
        assert service.state()["printer_available"] is True

    assert printer.checks == 1


def test_deciding_whether_to_print_never_uses_the_cached_state(db, config):
    """A stale yes here would send a label at a printer that is off."""
    printer = CountingPrinter(available_flag=True)
    service = make_service(db, config, printer)
    service.state()

    printer.available_flag = False

    assert service.printer_available() is False
    assert printer.checks == 2
    # and the display follows the fresh answer rather than the old one
    assert service.state()["printer_available"] is False


def test_changing_the_printer_drops_the_cached_state(db, config):
    service = make_service(db, config, CountingPrinter(available_flag=True))
    assert service.state()["printer_available"] is True

    service.set_printer_name("Some_Other_Queue")

    assert service._printer_check is None


def test_service_implements_every_controller_method(db, config, printer):
    service = make_service(db, config, printer)
    for name in ("state", "pause", "resume", "set_auto_print", "check_now",
                 "print_label", "print_original", "test_print"):
        assert callable(getattr(service, name))


# --- retention ---------------------------------------------------------------


def backdate(db, label_id: int, days: int) -> None:
    stamp = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    db.update_label(label_id, created_at=stamp)


def label_files(config, label_id: int) -> Path:
    return Path(config.data_dir) / "labels" / str(label_id)


def test_old_printed_labels_lose_their_pdfs_but_keep_their_row(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.check_now()
    assert label_files(config, 1).is_dir()
    backdate(db, 1, config.label_retention_days + 1)

    assert service.prune_old_files()["pruned"] == 1

    assert not label_files(config, 1).exists()
    label = db.get_label(1)
    assert label is not None
    assert label.status == LabelStatus.PRINTED
    assert label.print_count == 1
    assert label.original_path is None
    assert label.print_path is None
    assert any("pruned" in e.message for e in db.list_events(stage=Stage.SYSTEM))


def test_recent_labels_are_left_alone(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.check_now()

    assert service.prune_old_files()["pruned"] == 0
    assert label_files(config, 1).is_dir()
    assert db.get_label(1).print_path is not None


def test_labels_awaiting_a_human_are_never_pruned(db, config, printer, tmp_path):
    fetcher = FakeFetcher([("blank", blank_label_eml(tmp_path))])
    service = make_service(db, config, printer, fetcher=fetcher)
    service.check_now()
    assert db.get_label(1).status == LabelStatus.NEEDS_REVIEW
    backdate(db, 1, config.label_retention_days * 5)

    assert service.prune_old_files()["pruned"] == 0
    assert label_files(config, 1).is_dir()
    assert db.get_label(1).print_path is not None


def test_retention_of_zero_disables_pruning(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.check_now()
    backdate(db, 1, 5000)

    config.label_retention_days = 0
    result = service.prune_old_files()

    assert result == {"pruned": 0, "cutoff": None}
    assert label_files(config, 1).is_dir()


def test_pruning_twice_prunes_nothing_the_second_time(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct", "vinted-direct")
    service.check_now()
    backdate(db, 1, config.label_retention_days + 1)
    backdate(db, 2, config.label_retention_days + 1)

    assert service.prune_old_files()["pruned"] == 2
    events = len(db.list_events(stage=Stage.SYSTEM))

    assert service.prune_old_files()["pruned"] == 0
    assert len(db.list_events(stage=Stage.SYSTEM)) == events
    assert len(db.list_labels()) == 2


# --- scheduler ---------------------------------------------------------------


def test_scheduler_wires_the_poll_retry_and_prune_jobs(db, config, printer):
    scheduler = build_scheduler(make_service(db, config, printer), config)
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {"poll", "retry", "prune"}
    assert jobs["poll"].trigger.interval.total_seconds() == config.poll_interval_min * 60
    assert jobs["retry"].trigger.interval.total_seconds() == RETRY_INTERVAL_MIN * 60
    assert jobs["prune"].trigger.interval.total_seconds() == PRUNE_INTERVAL_HOURS * 3600
    assert all(job.coalesce and job.max_instances == 1 for job in jobs.values())


def test_the_poll_interval_can_be_overridden_in_settings(db, config, printer):
    service = make_service(db, config, printer)
    assert poll_interval_min(service, config) == config.poll_interval_min

    db.set_setting("poll_interval_min", "7")
    assert poll_interval_min(service, config) == 7

    db.set_setting("poll_interval_min", "not a number")
    assert poll_interval_min(service, config) == config.poll_interval_min


def poll_job_minutes(scheduler) -> float:
    """The interval the poll job is actually running on, in minutes."""
    job = {j.id: j for j in scheduler.get_jobs()}[POLL_JOB_ID]
    return job.trigger.interval.total_seconds() / 60


def test_a_new_interval_retimes_the_poll_job_without_a_restart(db, config, printer):
    """Saving an interval used to change a row and nothing else until a restart."""
    service = make_service(db, config, printer)
    scheduler = build_scheduler(service, config)
    assert poll_job_minutes(scheduler) == config.poll_interval_min

    db.set_setting(POLL_INTERVAL_KEY, "11")
    assert service.reschedule_poll() == 11
    assert poll_job_minutes(scheduler) == 11

    messages = [event.message for event in db.list_events(stage=Stage.SYSTEM)]
    assert any("every 11 min" in message for message in messages)


def test_retiming_reuses_the_settings_fallbacks(db, config, printer):
    service = make_service(db, config, printer)
    scheduler = build_scheduler(service, config)

    db.set_setting(POLL_INTERVAL_KEY, "0")  # clamped, never a hot loop
    assert service.reschedule_poll() == 1
    assert poll_job_minutes(scheduler) == 1

    db.set_setting(POLL_INTERVAL_KEY, "not a number")
    assert service.reschedule_poll() == config.poll_interval_min
    assert poll_job_minutes(scheduler) == config.poll_interval_min


def test_the_poll_job_is_retimed_while_the_scheduler_runs(db, config, printer):
    """The case that matters: the timers are live when Settings is saved."""
    service = make_service(db, config, printer)
    scheduler = build_scheduler(service, config)
    scheduler.start()
    try:
        db.set_setting(POLL_INTERVAL_KEY, "5")
        assert service.reschedule_poll() == 5
        assert poll_job_minutes(scheduler) == 5
        # and it is scheduled to fire on the new interval, not the old one
        job = scheduler.get_job(POLL_JOB_ID)
        assert job.next_run_time is not None
        assert (job.next_run_time - datetime.now(job.next_run_time.tzinfo)) <= timedelta(
            minutes=5
        )
    finally:
        scheduler.shutdown(wait=False)


def test_rescheduling_without_a_scheduler_is_a_no_op(db, config, printer):
    """`check-now` and the tests build a service with no timers behind it."""
    service = make_service(db, config, printer)
    assert service.reschedule_poll() is None
    assert db.list_events(stage=Stage.SYSTEM) == []


def test_retry_and_reap_recovers_a_stranded_label(db, config):
    printer = OfflinePrinter(available_flag=False)
    service = make_service(db, config, printer, "poshmark-direct")
    service.check_now()
    assert db.get_label(1).status == LabelStatus.WAITING_FOR_PRINTER

    printer.available_flag = True
    retry_and_reap(service)
    assert db.get_label(1).status == LabelStatus.PRINTED


def test_check_now_summary_renders_as_key_value_pairs(db, config, printer):
    summary = make_service(db, config, printer, "poshmark-direct").check_now()
    assert list(summary) == [
        "checked",
        "new",
        "duplicates",
        "printed_or_queued",
        "needs_review",
        "errors",
        "detail",
    ]
    assert summary["detail"] == "1 new label, 1 sent to the printer."


# --- regressions -------------------------------------------------------------


class PendingPrinter:
    """Behaves like real CUPS: a submitted job sits in the queue for a while."""

    def __init__(self):
        self.submitted: list[str] = []
        self.status = JobStatus.PENDING

    def submit(self, pdf_path: str) -> str:
        self.submitted.append(pdf_path)
        return f"Canon-{len(self.submitted)}"

    def job_status(self, job_id: str) -> JobStatus:
        return self.status

    def available(self) -> bool:
        return True


def test_a_label_stuck_in_processing_is_picked_back_up(db, config, printer):
    """A crash mid-pipeline used to leave the row in `processing` forever."""
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()
    assert db.get_label(1).status == LabelStatus.READY

    # what a killed process leaves behind: status set, pipeline never finished
    db.update_status(1, LabelStatus.PROCESSING)
    db.update_label(1, print_path=None)

    service.check_now()
    assert db.get_label(1).status == LabelStatus.READY
    assert Path(db.get_label(1).print_path).is_file()


def test_printing_a_label_twice_does_not_buy_two_labels(db, config, printer):
    """Double-tapping Print while the job is still queued must submit once."""
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()

    service.printer = PendingPrinter()
    first = service.print_label(1)
    assert first["ok"] is True
    assert db.get_label(1).status_detail == f"{JOB_PREFIX}Canon-1"

    second = service.print_label(1)
    assert second["ok"] is False
    assert "already at the printer" in second["detail"]

    assert len(service.printer.submitted) == 1
    # the first job id survives, so reaping still finds it
    assert db.get_label(1).status_detail == f"{JOB_PREFIX}Canon-1"


def test_reprinting_is_still_allowed_once_the_job_finished(db, config, printer):
    service = make_service(db, config, printer, "poshmark-direct")
    service.pause()
    service.check_now()

    service.printer = PendingPrinter()
    service.print_label(1)
    service.printer.status = JobStatus.COMPLETED

    assert service.print_label(1)["ok"] is True
    assert len(service.printer.submitted) == 2
    assert db.get_label(1).print_count == 2


def test_setting_the_printer_name_swaps_the_live_printer(db, config, printer):
    """Fixing the printer in Settings has to reach the running print worker."""
    service = make_service(db, config, printer, "poshmark-direct")
    assert service.printer is printer

    service.set_printer_name("Canon_TS9521")
    assert service.printer is not printer
    assert service.printer_name() == "Canon_TS9521"
    assert db.get_setting("printer_name") == "Canon_TS9521"


def test_a_failed_calibration_save_closes_the_document(tmp_path, monkeypatch):
    opened = []
    real_open = fitz.open

    def spy(*args, **kwargs):
        doc = real_open(*args, **kwargs)
        opened.append(doc)
        return doc

    monkeypatch.setattr(fitz, "open", spy)
    monkeypatch.setattr(
        fitz.Document, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError):
        write_calibration_pdf(tmp_path / "cal.pdf")

    assert opened and all(doc.is_closed for doc in opened)
