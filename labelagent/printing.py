import os
import re
import shutil
import subprocess
import threading
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

TIMEOUT = 10.0
REQUEST_ID_RE = re.compile(r"request id is (\S+)")

# `lpstat -l` labels the job-state-reasons line; IPP spells it "canceled", but
# match both spellings so a differently-worded CUPS build cannot read as a
# successful print.
ALERTS_PREFIX = "alerts:"
UNPRINTED_RE = re.compile(r"cancell?ed|aborted", re.IGNORECASE)


class JobStatus(StrEnum):
    PENDING = "pending"
    PRINTING = "printing"
    COMPLETED = "completed"
    # Cancelled by a human or aborted by CUPS: the job is finished and nothing
    # came out of the printer.
    CANCELLED = "cancelled"
    FAILED = "failed"
    # CUPS has never heard of this job - either it dropped out of the history
    # or it never made it onto a queue. Only the caller knows which.
    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"


class PrinterUnavailable(Exception):
    pass


@runtime_checkable
class Printer(Protocol):
    def submit(self, pdf_path: str) -> str: ...

    def job_status(self, job_id: str) -> JobStatus: ...

    def available(self) -> bool: ...


class CupsPrinter:
    def __init__(
        self,
        printer_name: str,
        media: str = "na_index-4x6_4x6in",
        media_source: str = "rear",
    ):
        self.printer_name = printer_name
        self.media = media
        self.media_source = media_source

    def submit(self, pdf_path: str) -> str:
        cmd = ["lp", "-d", self.printer_name]
        if self.media:
            cmd += ["-o", f"media={self.media}"]
        if self.media_source:
            cmd += ["-o", f"media-source={self.media_source}"]
        cmd.append(str(pdf_path))

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TIMEOUT, check=True
            )
        except subprocess.CalledProcessError as exc:
            raise PrinterUnavailable(
                f"lp failed ({exc.returncode}): {(exc.stderr or '').strip()}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PrinterUnavailable("lp timed out") from exc
        except FileNotFoundError as exc:
            raise PrinterUnavailable("lp not found; is CUPS installed?") from exc

        match = REQUEST_ID_RE.search(result.stdout or "")
        if not match:
            raise PrinterUnavailable(
                f"could not parse lp output: {(result.stdout or '').strip()!r}"
            )
        return match.group(1)

    def job_status(self, job_id: str) -> JobStatus:
        pending = self._lpstat(["-W", "not-completed", "-o"])
        if pending is None:
            return JobStatus.UNREACHABLE
        if self._has_job(pending, job_id):
            return JobStatus.PENDING

        # `-W completed` lists cancelled and aborted jobs next to the ones that
        # really printed, so being in this queue is not proof of a label. `-l`
        # adds the job-state-reasons line that tells them apart, and it has to
        # come before `-o`: lpstat acts on each option as it reads it.
        completed = self._lpstat(["-W", "completed", "-l", "-o"])
        if completed is None:
            return JobStatus.UNREACHABLE
        alerts = self._job_alerts(completed, job_id)
        if alerts is None:
            return JobStatus.UNKNOWN
        return JobStatus.CANCELLED if UNPRINTED_RE.search(alerts) else JobStatus.COMPLETED

    def available(self) -> bool:
        try:
            result = subprocess.run(
                ["lpstat", "-p", self.printer_name],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        return result.returncode == 0

    def _lpstat(self, args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["lpstat", *args],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                # CUPS translates its output, and a localised "Alerts:" would
                # read as a job with no reasons - i.e. as a clean print.
                env={**os.environ, "LC_ALL": "C"},
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout or ""

    @staticmethod
    def _has_job(output: str, job_id: str) -> bool:
        return any(line.split(" ", 1)[0] == job_id for line in output.splitlines() if line)

    @staticmethod
    def _job_alerts(output: str, job_id: str) -> str | None:
        """`job_id`'s job-state-reasons, or None if CUPS has no such job.

        `lpstat -l` prints a job on one line and indents its details under it:

            Canon_TS9521-42 brian 41984 Sat 01 Aug 2026 03:16:04 PM EDT
                Alerts: job-canceled-by-user
                queued for Canon_TS9521

        A job with no Alerts line at all reads as an empty string, i.e. printed:
        that is the benefit of the doubt this method gave every completed job
        before it learned to read the reasons, and it keeps a CUPS build that
        omits the line from parking every label Elaine prints.
        """
        found = False
        for line in output.splitlines():
            if not line.strip():
                continue
            if not line[0].isspace():
                if found:  # past our job's block; the alerts belong to another
                    break
                found = line.split(" ", 1)[0] == job_id
            elif found and line.strip().lower().startswith(ALERTS_PREFIX):
                return line.strip()[len(ALERTS_PREFIX) :].strip()
        return "" if found else None


class FilePrinter:
    """Dev/test printer: copies the PDF into out_dir instead of printing."""

    def __init__(self, out_dir: str | Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Path] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def submit(self, pdf_path: str) -> str:
        src = Path(pdf_path)
        if not src.is_file():
            raise PrinterUnavailable(f"no such file: {src}")
        with self._lock:
            self._counter += 1
            job_id = f"file-{self._counter}"
        dest = self.out_dir / f"{job_id}-{src.name}"
        shutil.copyfile(src, dest)
        self.jobs[job_id] = dest
        return job_id

    def job_status(self, job_id: str) -> JobStatus:
        return JobStatus.COMPLETED

    def available(self) -> bool:
        return True


def make_printer(config) -> Printer:
    if config.printer_name in ("", "file"):
        return FilePrinter(Path(config.data_dir) / "printed")
    return CupsPrinter(
        config.printer_name, config.print_media, config.print_media_source
    )
