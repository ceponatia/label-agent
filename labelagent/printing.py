import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

TIMEOUT = 10.0
SUMATRA_TIMEOUT = 60.0
WINDOWS_JOB_POLL_ATTEMPTS = 10
WINDOWS_JOB_POLL_INTERVAL = 0.1
REQUEST_ID_RE = re.compile(r"request id is (\S+)")
IS_WINDOWS = os.name == "nt"
DEFAULT_WINDOWS_PRINT_SETTINGS = (
    "fit,simplex,paper=auto,bin=auto,disable-auto-rotation,ignore-pdf-print-settings"
)
WINDOWS_JOB_PREFIX = "win-"
WINDOWS_COMPLETE_PREFIX = "win-complete-"
WINDOWS_CANCELLED_RE = re.compile(r"cancell?ed|delet|abort", re.IGNORECASE)
SUMATRA_ERRORS = {
    2: "could not open the PDF",
    3: "the PDF does not allow printing",
    4: "the printer does not exist",
    5: "the printer driver or device failed",
    6: "printing is disabled by policy",
}

# `lpstat -l` labels the job-state-reasons line; IPP spells it "canceled", but
# match both spellings so a differently-worded CUPS build cannot read as a
# successful print.
ALERTS_PREFIX = "alerts:"
UNPRINTED_RE = re.compile(r"cancell?ed|aborted", re.IGNORECASE)


class JobStatus(StrEnum):
    PENDING = "pending"
    PRINTING = "printing"
    COMPLETED = "completed"
    # Cancelled by a human or aborted by the print system: the job is finished
    # and nothing came out of the printer.
    CANCELLED = "cancelled"
    FAILED = "failed"
    # The print system has no record of this job. Only the caller knows whether
    # that means it aged out after printing or never reached a queue.
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


class WindowsPrinter:
    """Windows print-spooler adapter using SumatraPDF for PDF rendering.

    Sumatra's exit code tells us whether the document reached the Windows print
    system. `Get-PrintJob` then lets us follow an observed spooler job. Windows
    does not retain a completed-job history by default, so a job that we saw in
    the queue and can no longer find is treated as completed. If a very fast job
    disappears before we can observe its spooler id, Sumatra's successful handoff
    is treated as immediate completion rather than risking a duplicate retry.
    """

    def __init__(
        self,
        printer_name: str,
        sumatra_path: str = "",
        print_settings: str = DEFAULT_WINDOWS_PRINT_SETTINGS,
    ):
        self.printer_name = printer_name
        self.sumatra_path = find_sumatra(sumatra_path)
        self.print_settings = print_settings or DEFAULT_WINDOWS_PRINT_SETTINGS

    def submit(self, pdf_path: str) -> str:
        source = Path(pdf_path)
        if not source.is_file():
            raise PrinterUnavailable(f"no such file: {source}")
        if not self.sumatra_path or not Path(self.sumatra_path).is_file():
            raise PrinterUnavailable(
                "SumatraPDF not found; install it or set LABELAGENT_SUMATRA_PATH"
            )

        before = self._jobs()
        if before is None:
            raise PrinterUnavailable(
                f"Windows print queue {self.printer_name!r} is unavailable"
            )
        before_ids = {self._job_id(job) for job in before}

        cmd = [
            self.sumatra_path,
            "-silent",
            "-print-to",
            self.printer_name,
            "-print-settings",
            self.print_settings,
            str(source),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=SUMATRA_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise PrinterUnavailable("SumatraPDF print command timed out") from exc
        except OSError as exc:
            raise PrinterUnavailable(f"could not start SumatraPDF: {exc}") from exc

        if result.returncode != 0:
            detail = SUMATRA_ERRORS.get(
                result.returncode,
                (result.stderr or result.stdout or "unknown SumatraPDF error").strip(),
            )
            raise PrinterUnavailable(
                f"SumatraPDF print failed ({result.returncode}): {detail}"
            )

        # Sumatra uses the file name as the Windows print-job name. Give the
        # spooler a short grace period to expose the new job after Sumatra exits.
        for attempt in range(WINDOWS_JOB_POLL_ATTEMPTS):
            jobs = self._jobs()
            if jobs is None:
                break
            job = self._submitted_job(jobs, before_ids, source.name)
            if job is not None:
                return f"{WINDOWS_JOB_PREFIX}{self._job_id(job)}"
            if attempt + 1 < WINDOWS_JOB_POLL_ATTEMPTS:
                time.sleep(WINDOWS_JOB_POLL_INTERVAL)

        # A small one-page label can leave the queue before PowerShell sees it.
        # Sumatra returned success, so do not re-submit and risk a duplicate.
        return f"{WINDOWS_COMPLETE_PREFIX}{uuid.uuid4().hex}"

    def job_status(self, job_id: str) -> JobStatus:
        if job_id.startswith(WINDOWS_COMPLETE_PREFIX):
            return JobStatus.COMPLETED
        if not job_id.startswith(WINDOWS_JOB_PREFIX):
            return JobStatus.UNKNOWN
        try:
            native_id = int(job_id[len(WINDOWS_JOB_PREFIX) :])
        except ValueError:
            return JobStatus.UNKNOWN

        jobs = self._jobs()
        if jobs is None:
            # We already observed this job in the Windows spooler at submit time.
            # A transient PowerShell/spooler query failure must not make the
            # service re-submit the label and risk a duplicate physical print.
            return JobStatus.PENDING
        job = next((j for j in jobs if self._job_id(j) == native_id), None)
        if job is None:
            # submit() only returns win-<id> after observing that exact spooler
            # job, so absence later means it left the live Windows queue.
            return JobStatus.COMPLETED

        status = str(job.get("JobStatus") or "").strip().lower()
        if WINDOWS_CANCELLED_RE.search(status):
            return JobStatus.CANCELLED
        if "printed" in status:
            return JobStatus.COMPLETED
        if "printing" in status:
            return JobStatus.PRINTING

        # Offline, out-of-paper, paused and error states intentionally stay
        # pending while Windows owns the job. Re-submitting them could print a
        # second label when the queue recovers.
        return JobStatus.PENDING

    def available(self) -> bool:
        return bool(
            self.sumatra_path
            and Path(self.sumatra_path).is_file()
            and self._jobs() is not None
        )

    def _jobs(self) -> list[dict] | None:
        command = (
            "$jobs = @(Get-PrintJob -PrinterName $env:LABELAGENT_WINDOWS_PRINTER "
            "-ErrorAction Stop | Select-Object ID,DocumentName,"
            "@{Name='JobStatus';Expression={$_.JobStatus.ToString()}}); "
            "ConvertTo-Json -Compress -InputObject $jobs"
        )
        env = {**os.environ, "LABELAGENT_WINDOWS_PRINTER": self.printer_name}
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                env=env,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads((result.stdout or "[]").strip() or "[]")
        except (TypeError, ValueError):
            return None
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return None
        return [job for job in data if isinstance(job, dict)]

    @staticmethod
    def _job_id(job: dict) -> int | None:
        raw = job.get("ID", job.get("Id"))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _submitted_job(
        cls, jobs: list[dict], before_ids: set[int | None], filename: str
    ) -> dict | None:
        new_jobs = [job for job in jobs if cls._job_id(job) not in before_ids]
        if not new_jobs:
            return None
        wanted = filename.casefold()
        named = [
            job
            for job in new_jobs
            if str(job.get("DocumentName") or "").casefold() == wanted
        ]
        if len(named) == 1:
            return named[0]
        if len(new_jobs) == 1:
            return new_jobs[0]
        return None


def find_sumatra(explicit: str = "") -> str:
    """Return an explicit or conventional SumatraPDF executable path."""
    if explicit:
        return str(Path(os.path.expandvars(explicit)).expanduser())

    on_path = shutil.which("SumatraPDF.exe") or shutil.which("SumatraPDF")
    if on_path:
        return on_path

    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "SumatraPDF" / "SumatraPDF.exe")
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(key)
        if root:
            candidates.append(Path(root) / "SumatraPDF" / "SumatraPDF.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


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
    if IS_WINDOWS:
        return WindowsPrinter(
            config.printer_name,
            getattr(config, "sumatra_path", ""),
            getattr(config, "windows_print_settings", DEFAULT_WINDOWS_PRINT_SETTINGS),
        )
    return CupsPrinter(
        config.printer_name, config.print_media, config.print_media_source
    )
