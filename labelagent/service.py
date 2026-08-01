"""The agent runtime: poll Gmail, normalize labels, drive the printer.

This is the object the web app talks to (it implements `AgentController`) and
the object the scheduler ticks. Runtime state - running/paused and auto-print -
lives in the settings table, not in memory, so the web app and the background
jobs always agree and a restart keeps whatever Elaine last chose.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import fitz

from . import storage
from .config import Config
from .db import Database
from .ingest import LAST_POLL_KEY, Fetcher, make_fetcher, poll_once
from .models import Label, LabelStatus, Level, Stage
from .pipeline import TARGET_HEIGHT, TARGET_WIDTH, process_label_pdf
from .printing import JobStatus, Printer, PrinterUnavailable, make_printer
from .verify import verify_print_pdf

AGENT_STATE_KEY = "agent_state"
AUTO_PRINT_KEY = "auto_print"
PRINTER_NAME_KEY = "printer_name"
POLL_INTERVAL_KEY = "poll_interval_min"

RUNNING = "running"
PAUSED = "paused"
TRUTHY = {"1", "true", "yes", "on"}

# CUPS job ids have no column of their own; they ride along in status_detail
# while a label is in flight and are cleared once it prints.
JOB_PREFIX = "job:"
IN_FLIGHT = (LabelStatus.QUEUED, LabelStatus.PRINTING)

# Rows the pipeline still owes work to. `processing` is in here because a crash
# or a restart mid-pipeline leaves the row there forever otherwise: nothing else
# ever looks at it again and the UI has no action that can move it.
UNPROCESSED = (LabelStatus.INGESTED, LabelStatus.PROCESSING)

# Only labels that are done with can lose their PDFs; anything still printable
# or still waiting for a human keeps its files however old it is.
PRUNABLE = (LabelStatus.PRINTED, LabelStatus.FAILED, LabelStatus.DUPLICATE)
# list_labels() returns newest first, so a small limit would hide exactly the
# rows pruning cares about.
PRUNE_LIMIT = 100_000

TEST_PRINT_FILENAME = "test-print.pdf"
CALIBRATION_TITLE = "label-agent calibration 4×6"
CALIBRATION_SUBTITLE = "288 × 432 pt - border sits 4 pt inside the page edge"
BORDER_INSET = 4.0
TICK_SPACING = 36.0
TICK_LENGTH = 8.0
CROSSHAIR_INSET = 22.0
CROSSHAIR_ARM = 12.0


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def _job_id(status_detail: str | None) -> str | None:
    if status_detail and status_detail.startswith(JOB_PREFIX):
        return status_detail[len(JOB_PREFIX) :].strip() or None
    return None


class AgentService:
    """Everything the agent can be asked to do, from the UI or on a timer."""

    def __init__(
        self,
        db: Database,
        config: Config,
        printer: Printer | None = None,
        fetcher_factory: Callable[[Config], Fetcher] | None = None,
    ):
        self.db = db
        self.config = config
        self.printer = printer if printer is not None else make_printer(
            _printer_config(db, config)
        )
        self.fetcher_factory = fetcher_factory or make_fetcher
        self._fetcher = None
        # Set by whoever owns the timers; see set_poll_rescheduler.
        self._poll_rescheduler: Callable[[], int] | None = None
        # The scheduler and the web app both drive this object; one cycle at a
        # time is what keeps a label from being submitted to CUPS twice.
        self._lock = threading.RLock()

    # --- runtime state ---------------------------------------------------

    def agent_state(self) -> str:
        stored = self.db.get_setting(AGENT_STATE_KEY, RUNNING)
        return PAUSED if stored == PAUSED else RUNNING

    def is_running(self) -> bool:
        return self.agent_state() == RUNNING

    def auto_print(self) -> bool:
        return _truthy(
            self.db.get_setting(AUTO_PRINT_KEY, "on" if self.config.auto_print else "off")
        )

    def printer_name(self) -> str:
        return getattr(self.printer, "printer_name", "") or "file printer"

    def printer_available(self) -> bool:
        try:
            return bool(self.printer.available())
        except Exception:
            return False

    def state(self) -> dict:
        return {
            "agent_state": self.agent_state(),
            "auto_print": self.auto_print(),
            "printer_available": self.printer_available(),
            "printer_name": self.printer_name(),
            "last_poll_at": self.db.get_setting(LAST_POLL_KEY),
        }

    def pause(self) -> None:
        self.db.set_setting(AGENT_STATE_KEY, PAUSED)
        self.db.add_event(
            Stage.SYSTEM, Level.INFO, "agent paused; nothing will be sent to the printer"
        )

    def resume(self) -> None:
        self.db.set_setting(AGENT_STATE_KEY, RUNNING)
        self.db.add_event(Stage.SYSTEM, Level.INFO, "agent resumed")

    def set_auto_print(self, on: bool) -> None:
        self.db.set_setting(AUTO_PRINT_KEY, "on" if on else "off")
        self.db.add_event(
            Stage.SYSTEM, Level.INFO, f"auto-print turned {'on' if on else 'off'}"
        )

    def set_printer_name(self, name: str) -> None:
        """Point the print worker at a different CUPS queue, effective now.

        Without this a printer fixed in Settings only took effect after a
        restart, leaving labels piling up in `waiting_for_printer` while the UI
        showed the corrected name.
        """
        name = (name or "").strip()
        with self._lock:
            self.db.set_setting(PRINTER_NAME_KEY, name)
            self.printer = make_printer(replace(self.config, printer_name=name))
        self.db.add_event(
            Stage.SYSTEM, Level.INFO, f"printer set to {name or 'file printer'}"
        )

    def set_poll_rescheduler(self, reschedule: Callable[[], int] | None) -> None:
        """Accept the timer owner's retiming hook (scheduler.attach_poll_rescheduler).

        The service has no scheduler of its own - `check_now` runs on demand and
        the timers belong to whoever started them - so this callback is the only
        route a Settings change has to the running poll job.
        """
        self._poll_rescheduler = reschedule

    def reschedule_poll(self) -> int | None:
        """Re-time the poll job to the interval now in Settings, effective now.

        Returns the minutes applied, or None where no scheduler is attached (the
        one-shot CLI commands, tests): there is simply no timer to retime.
        The scheduler has its own lock, and taking `_lock` here would make saving
        Settings wait out an in-flight poll for no reason.
        """
        if self._poll_rescheduler is None:
            return None
        minutes = self._poll_rescheduler()
        self.db.add_event(
            Stage.SYSTEM, Level.INFO, f"polling the mailbox every {minutes} min"
        )
        return minutes

    # --- one full cycle --------------------------------------------------

    def check_now(self) -> dict:
        """Poll, normalize, print, collect job results. Never raises."""
        with self._lock:
            return self._check_now()

    def _check_now(self) -> dict:
        errors: list[str] = []
        checked = new = duplicates = 0

        try:
            fetcher = self._get_fetcher()
        except Exception as exc:
            message = f"cannot open the mailbox: {exc}"
            self.db.add_event(Stage.SYSTEM, Level.ERROR, message)
            errors.append(message)
        else:
            result = poll_once(self.db, self.config, fetcher)
            checked = result.checked
            new = len(result.new_labels)
            duplicates = len(result.duplicates)
            errors += result.errors
            if result.errors:
                self._drop_fetcher()

        processed = self.process_new()
        needs_review = sum(
            1 for label in processed if label.status == LabelStatus.NEEDS_REVIEW
        )
        errors += [
            f"label {label.id}: {label.status_detail or 'failed'}"
            for label in processed
            if label.status == LabelStatus.FAILED
        ]

        submitted = self.dispatch_prints()
        self.reap_jobs()

        return {
            "checked": checked,
            "new": new,
            "duplicates": duplicates,
            "printed_or_queued": len(submitted),
            "needs_review": needs_review,
            "errors": errors,
            "detail": _check_detail(new, len(submitted), needs_review, errors),
        }

    # --- pipeline --------------------------------------------------------

    def process_new(self) -> list[Label]:
        """Normalize and verify every label the pipeline has not finished with.

        Rows left in `processing` by an interrupted run are picked back up: one
        process owns the pipeline and a cycle holds the lock throughout, so a
        `processing` row seen here is always an orphan, never live work.
        """
        with self._lock:
            pending = [
                label
                for status in UNPROCESSED
                for label in self.db.list_labels(status=status)
            ]
            return [self._process_label(label) for label in pending]

    def _process_label(self, label: Label) -> Label:
        self.db.update_status(label.id, LabelStatus.PROCESSING)

        original = label.original_path
        if not original or not Path(original).is_file():
            return self._fail(label.id, "no original pdf on disk", Stage.CROP)

        output = storage.print_pdf_path(self.config.data_dir, label.id)
        try:
            result = process_label_pdf(original, output)
        except Exception as exc:
            return self._fail(label.id, f"pipeline failed: {exc}", Stage.CROP)

        if not result.ok:
            return self._fail(
                label.id, "; ".join(result.problems) or "pipeline failed", Stage.CROP
            )

        self.db.update_label(label.id, print_path=result.print_path or str(output))
        self.db.add_event(
            Stage.CROP, Level.INFO, f"normalized to 4x6 ({result.method})", label.id
        )

        problems = list(result.problems)
        try:
            verdict = verify_print_pdf(
                result.print_path,
                expected_tracking=label.tracking_number,
                config=self.config,
            )
        except Exception as exc:
            problems.append(f"verification failed: {exc}")
            source = "deterministic"
            barcodes: list[str] = []
        else:
            problems += verdict.problems
            source = verdict.source
            barcodes = verdict.barcodes

        if problems or result.needs_review:
            detail = "; ".join(dict.fromkeys(problems)) or "needs a look before printing"
            self.db.update_status(label.id, LabelStatus.NEEDS_REVIEW, detail)
            self.db.add_event(Stage.VERIFY, Level.WARN, f"needs review: {detail}", label.id)
        else:
            self.db.update_status(label.id, LabelStatus.READY)
            self.db.add_event(
                Stage.VERIFY,
                Level.INFO,
                f"verified ({source}); barcode {', '.join(barcodes) or 'not read'}",
                label.id,
            )
        return self.db.get_label(label.id)

    def _fail(self, label_id: int, detail: str, stage: str) -> Label:
        self.db.update_status(label_id, LabelStatus.FAILED, detail)
        self.db.add_event(stage, Level.ERROR, detail, label_id)
        return self.db.get_label(label_id)

    # --- printing --------------------------------------------------------

    def dispatch_prints(self) -> list[int]:
        """Send ready labels to the printer. Duplicates are never ready."""
        with self._lock:
            if not (self.is_running() and self.auto_print()):
                return []
            return [
                label.id
                for label in self.db.list_labels(status=LabelStatus.READY)
                if self._submit(label)["ok"]
            ]

    def retry_waiting(self) -> list[int]:
        """Re-submit anything stranded by an offline printer."""
        with self._lock:
            waiting = self.db.list_labels(status=LabelStatus.WAITING_FOR_PRINTER)
            if not waiting or not self.is_running() or not self.printer_available():
                return []
            return [label.id for label in waiting if self._submit(label)["ok"]]

    def reap_jobs(self) -> list[int]:
        """Advance in-flight jobs; returns the labels that finished printing."""
        with self._lock:
            printed: list[int] = []
            for status in IN_FLIGHT:
                for label in self.db.list_labels(status=status):
                    if self._reap(label):
                        printed.append(label.id)
            return printed

    def print_label(self, label_id: int) -> dict:
        """Explicit user intent: print regardless of pause or auto-print."""
        return self._manual_print(label_id, "print")

    def print_original(self, label_id: int) -> dict:
        """Print the untouched attachment, for labels the crop mangled."""
        return self._manual_print(label_id, "original")

    def _manual_print(self, label_id: int, which: str) -> dict:
        with self._lock:
            label = self.db.get_label(label_id)
            if label is None:
                return {"ok": False, "detail": f"Label {label_id} not found."}

            # Every list in the UI offers Print, including for labels already at
            # the printer, so a double tap used to buy a second physical label
            # (and orphan the first job id). Settle the outstanding job first;
            # only refuse while it is genuinely still in the queue.
            if label.status in IN_FLIGHT and _job_id(label.status_detail):
                self._reap(label)
                label = self.db.get_label(label_id)
                if label.status in IN_FLIGHT:
                    return {
                        "ok": False,
                        "detail": (
                            f"Label {label_id} is already at the printer "
                            f"(job {_job_id(label.status_detail)}); not sending it twice."
                        ),
                    }

            result = self._submit(label, which)
            if result["ok"]:
                self._reap(self.db.get_label(label_id))
            return result

    def _submit(self, label: Label, which: str = "print") -> dict:
        path = label.print_path if which == "print" else label.original_path
        if not path or not Path(path).is_file():
            detail = f"no {which} pdf on disk for label {label.id}"
            # Fail it out of the automatic paths so we don't retry forever.
            if label.status in (LabelStatus.READY, LabelStatus.WAITING_FOR_PRINTER):
                self.db.update_status(label.id, LabelStatus.FAILED, detail)
            self.db.add_event(Stage.PRINT, Level.ERROR, detail, label.id)
            return {"ok": False, "detail": detail}

        try:
            job_id = self.printer.submit(str(path))
        except PrinterUnavailable as exc:
            return self._wait_for_printer(label.id, str(exc), Level.WARN)
        except Exception as exc:
            return self._wait_for_printer(label.id, f"print failed: {exc}", Level.ERROR)

        self.db.update_status(label.id, LabelStatus.QUEUED, f"{JOB_PREFIX}{job_id}")
        self.db.add_event(
            Stage.PRINT, Level.INFO, f"submitted {which} pdf as job {job_id}", label.id
        )
        return {
            "ok": True,
            "detail": f"Label {label.id} sent to the printer (job {job_id}).",
            "job_id": job_id,
        }

    def _wait_for_printer(self, label_id: int, detail: str, level: str) -> dict:
        self.db.update_status(label_id, LabelStatus.WAITING_FOR_PRINTER, detail)
        self.db.add_event(Stage.PRINT, level, f"waiting for printer: {detail}", label_id)
        return {"ok": False, "detail": f"Printer unavailable: {detail}"}

    def _reap(self, label: Label | None) -> bool:
        job_id = _job_id(label.status_detail) if label else None
        if not job_id:
            return False

        try:
            state = self.printer.job_status(job_id)
        except Exception:
            state = JobStatus.UNREACHABLE

        if state == JobStatus.COMPLETED:
            return self._mark_printed(label.id, f"printed (job {job_id})")
        if state == JobStatus.UNKNOWN:
            return self._reap_forgotten(label, job_id)
        if state == JobStatus.CANCELLED:
            # Nothing came out, and somebody already made a decision about this
            # job at the printer. Fail it rather than parking it in
            # `waiting_for_printer`, where retry_waiting would quietly send it
            # back and undo the cancel. The row keeps its Print button.
            self._fail(
                label.id, f"print job {job_id} was cancelled at the printer", Stage.PRINT
            )
            return False
        if state == JobStatus.FAILED:
            self._wait_for_printer(label.id, f"print job {job_id} failed", Level.WARN)
            return False
        if state == JobStatus.UNREACHABLE:
            self._wait_for_printer(
                label.id, f"printer unreachable with job {job_id} queued", Level.WARN
            )
            return False
        if label.status != LabelStatus.PRINTING:
            self.db.update_status(label.id, LabelStatus.PRINTING, label.status_detail)
        return False

    def _mark_printed(self, label_id: int, message: str) -> bool:
        self.db.mark_printed(label_id)
        self.db.update_label(label_id, status_detail=None)
        self.db.add_event(Stage.PRINT, Level.INFO, message, label_id)
        return True

    def _reap_forgotten(self, label: Label, job_id: str) -> bool:
        """Settle a job CUPS has no record of.

        CUPS keeps finished jobs only as long as MaxJobs and PreserveJobHistory
        allow, so a job we watched go through the queue and can no longer find
        printed and then aged out. Calling that a failure sent the label to
        `waiting_for_printer`, where retry_waiting bought a second physical
        label for a parcel that already had one.

        A label still sitting in `queued` is the other story: reaping follows
        submission closely enough that a real job is always on a queue by the
        time we first look, so one that was never seen there never printed.
        """
        if label.status == LabelStatus.PRINTING:
            return self._mark_printed(
                label.id, f"printed (job {job_id}, since dropped from the CUPS history)"
            )
        self._wait_for_printer(
            label.id, f"print job {job_id} never reached the queue", Level.WARN
        )
        return False

    def test_print(self) -> dict:
        """Print a calibration page so the tray and margins can be checked."""
        path = Path(self.config.data_dir) / TEST_PRINT_FILENAME
        try:
            write_calibration_pdf(path)
        except Exception as exc:
            detail = f"could not build the calibration page: {exc}"
            self.db.add_event(Stage.PRINT, Level.ERROR, detail)
            return {"ok": False, "detail": detail}

        try:
            job_id = self.printer.submit(str(path))
        except Exception as exc:
            detail = f"Printer unavailable: {exc}"
            self.db.add_event(Stage.PRINT, Level.WARN, f"test print failed: {exc}")
            return {"ok": False, "detail": detail}

        self.db.add_event(Stage.PRINT, Level.INFO, f"test print submitted as job {job_id}")
        return {
            "ok": True,
            "detail": f"Calibration page sent to the printer (job {job_id}).",
        }

    # --- housekeeping ----------------------------------------------------

    def prune_old_files(self) -> dict:
        """Delete label PDFs past the retention window.

        Rows are kept forever (SPEC §9) - history and metrics survive, only the
        bytes go. A retention of 0 or less turns pruning off.
        """
        days = self.config.label_retention_days
        if days <= 0:
            return {"pruned": 0, "cutoff": None}

        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            pruned = [
                label.id
                for status in PRUNABLE
                for label in self.db.list_labels(status=status, limit=PRUNE_LIMIT)
                if (label.created_at or "") < cutoff and self._remove_label_files(label)
            ]
            if pruned:
                self.db.add_event(
                    Stage.SYSTEM,
                    Level.INFO,
                    f"pruned the pdfs of {len(pruned)} label(s) older than {days} days",
                )
        return {"pruned": len(pruned), "cutoff": cutoff}

    def _remove_label_files(self, label: Label) -> bool:
        directory = storage.label_dir(self.config.data_dir, label.id)
        if not directory.is_dir() and not (label.original_path or label.print_path):
            return False

        if directory.is_dir():
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                self.db.add_event(
                    Stage.SYSTEM,
                    Level.WARN,
                    f"could not delete {directory}: {exc}",
                    label.id,
                )
                return False

        self.db.update_label(label.id, original_path=None, print_path=None)
        return True

    # --- mailbox ---------------------------------------------------------

    def _get_fetcher(self):
        if self._fetcher is None:
            self._fetcher = self.fetcher_factory(self.config)
        return self._fetcher

    def _drop_fetcher(self) -> None:
        """Forget a fetcher that just failed, so the next poll reconnects."""
        fetcher, self._fetcher = self._fetcher, None
        close = getattr(fetcher, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass

    def close(self) -> None:
        self._drop_fetcher()


def _printer_config(db: Database, config: Config) -> Config:
    """Config with the printer name from Settings, which wins after a restart."""
    name = db.get_setting(PRINTER_NAME_KEY, config.printer_name) or ""
    return config if name == config.printer_name else replace(config, printer_name=name)


def _check_detail(new: int, submitted: int, needs_review: int, errors: list[str]) -> str:
    parts = []
    if new:
        parts.append(f"{new} new label{'s' if new != 1 else ''}")
    if submitted:
        parts.append(f"{submitted} sent to the printer")
    if needs_review:
        parts.append(f"{needs_review} needs review")
    if errors:
        parts.append(f"{len(errors)} error{'s' if len(errors) != 1 else ''}")
    return ", ".join(parts) + "." if parts else "No new label emails."


def write_calibration_pdf(path: str | Path) -> Path:
    """A 4x6 ruler page: border, corner marks, half-inch ticks, centered text."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    black = (0, 0, 0)
    doc = fitz.open()
    try:
        _draw_calibration_page(doc, black)
        doc.save(str(destination), garbage=3, deflate=True)
    finally:
        doc.close()
    return destination


def _draw_calibration_page(doc: fitz.Document, black: tuple) -> None:
    page = doc.new_page(width=TARGET_WIDTH, height=TARGET_HEIGHT)

    border = fitz.Rect(
        BORDER_INSET,
        BORDER_INSET,
        TARGET_WIDTH - BORDER_INSET,
        TARGET_HEIGHT - BORDER_INSET,
    )
    page.draw_rect(border, color=black, width=1)

    def line(start, end) -> None:
        page.draw_line(start, end, color=black, width=0.6)

    for x in (border.x0 + CROSSHAIR_INSET, border.x1 - CROSSHAIR_INSET):
        for y in (border.y0 + CROSSHAIR_INSET, border.y1 - CROSSHAIR_INSET):
            line((x - CROSSHAIR_ARM, y), (x + CROSSHAIR_ARM, y))
            line((x, y - CROSSHAIR_ARM), (x, y + CROSSHAIR_ARM))

    for x in _ticks(TARGET_WIDTH):
        line((x, border.y0), (x, border.y0 + TICK_LENGTH))
        line((x, border.y1), (x, border.y1 - TICK_LENGTH))
    for y in _ticks(TARGET_HEIGHT):
        line((border.x0, y), (border.x0 + TICK_LENGTH, y))
        line((border.x1, y), (border.x1 - TICK_LENGTH, y))

    middle = TARGET_HEIGHT / 2
    page.insert_textbox(
        fitz.Rect(BORDER_INSET, middle - 24, TARGET_WIDTH - BORDER_INSET, middle + 24),
        f"{CALIBRATION_TITLE}\n{CALIBRATION_SUBTITLE}",
        fontsize=11,
        align=fitz.TEXT_ALIGN_CENTER,
    )


def _ticks(length: float) -> list[float]:
    """Tick positions every half inch, excluding the page edges."""
    return [TICK_SPACING * step for step in range(1, int((length - 1) // TICK_SPACING) + 1)]


__all__ = [
    "AgentService",
    "write_calibration_pdf",
    "PRUNABLE",
    "AGENT_STATE_KEY",
    "AUTO_PRINT_KEY",
    "POLL_INTERVAL_KEY",
    "PRINTER_NAME_KEY",
    "JOB_PREFIX",
    "RUNNING",
    "PAUSED",
]
