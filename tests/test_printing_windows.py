import json
import subprocess

import pytest

from labelagent import printing
from labelagent.config import Config
from labelagent.printing import (
    CupsPrinter,
    JobStatus,
    Printer,
    PrinterUnavailable,
    WindowsPrinter,
    find_sumatra,
    make_printer,
)


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "print.pdf"
    path.write_bytes(b"%PDF-1.4 fake\n")
    return path


@pytest.fixture
def sumatra(tmp_path):
    path = tmp_path / "SumatraPDF.exe"
    path.write_bytes(b"fake")
    return path


class FakeRun:
    """Stands in for subprocess.run for SumatraPDF and PowerShell."""

    def __init__(self, results: dict):
        self.results = results
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []

    def __call__(
        self, cmd, capture_output=False, text=False, timeout=None, check=False, env=None
    ):
        self.calls.append(cmd)
        self.envs.append(env)
        outcome = self.results[cmd[0]]
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        returncode, stdout, stderr = outcome
        if check and returncode != 0:
            raise subprocess.CalledProcessError(returncode, cmd, stdout, stderr)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def install(monkeypatch, results) -> FakeRun:
    fake = FakeRun(results)
    monkeypatch.setattr(printing.subprocess, "run", fake)
    return fake


def jobs(*items) -> str:
    return json.dumps(list(items))


# --- selection / discovery ------------------------------------------------


def test_make_printer_windows(monkeypatch, tmp_path, sumatra):
    monkeypatch.setattr(printing, "IS_WINDOWS", True)
    printer = make_printer(
        Config(
            data_dir=str(tmp_path),
            printer_name="Canon TS9500 series",
            sumatra_path=str(sumatra),
        )
    )
    assert isinstance(printer, WindowsPrinter)
    assert isinstance(printer, Printer)
    assert printer.printer_name == "Canon TS9500 series"
    assert printer.sumatra_path == str(sumatra)


def test_make_printer_non_windows_stays_cups(monkeypatch, tmp_path):
    monkeypatch.setattr(printing, "IS_WINDOWS", False)
    assert isinstance(
        make_printer(Config(data_dir=str(tmp_path), printer_name="Canon")), CupsPrinter
    )


def test_find_sumatra_in_standard_per_user_location(monkeypatch, tmp_path):
    executable = tmp_path / "SumatraPDF" / "SumatraPDF.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"fake")
    monkeypatch.setattr(printing.shutil, "which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert find_sumatra() == str(executable)


# --- submit ---------------------------------------------------------------


def test_windows_submit_builds_sumatra_command_and_captures_job(
    monkeypatch, pdf, sumatra
):
    fake = install(
        monkeypatch,
        {
            "powershell.exe": [
                (0, jobs(), ""),
                (
                    0,
                    jobs(
                        {
                            "ID": 42,
                            "DocumentName": "print.pdf",
                            "JobStatus": "Normal",
                        }
                    ),
                    "",
                ),
            ],
            str(sumatra): (0, "", ""),
        },
    )
    printer = WindowsPrinter("Canon TS9500 series", str(sumatra))

    assert printer.submit(str(pdf)) == "win-42"
    assert fake.calls[1] == [
        str(sumatra),
        "-silent",
        "-print-to",
        "Canon TS9500 series",
        "-print-settings",
        printing.DEFAULT_WINDOWS_PRINT_SETTINGS,
        str(pdf),
    ]
    assert fake.envs[0]["LABELAGENT_WINDOWS_PRINTER"] == "Canon TS9500 series"


def test_windows_submit_uses_filename_to_avoid_neighbour_job(
    monkeypatch, pdf, sumatra
):
    install(
        monkeypatch,
        {
            "powershell.exe": [
                (
                    0,
                    jobs(
                        {"ID": 7, "DocumentName": "old.pdf", "JobStatus": "Normal"}
                    ),
                    "",
                ),
                (
                    0,
                    jobs(
                        {"ID": 7, "DocumentName": "old.pdf", "JobStatus": "Normal"},
                        {"ID": 8, "DocumentName": "other.pdf", "JobStatus": "Normal"},
                        {"ID": 9, "DocumentName": "print.pdf", "JobStatus": "Normal"},
                    ),
                    "",
                ),
            ],
            str(sumatra): (0, "", ""),
        },
    )
    assert WindowsPrinter("Canon", str(sumatra)).submit(str(pdf)) == "win-9"


def test_windows_submit_fast_job_becomes_completed_token(monkeypatch, pdf, sumatra):
    monkeypatch.setattr(printing, "WINDOWS_JOB_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(printing.time, "sleep", lambda _: None)
    install(
        monkeypatch,
        {
            "powershell.exe": [(0, "[]", ""), (0, "[]", ""), (0, "[]", "")],
            str(sumatra): (0, "", ""),
        },
    )
    printer = WindowsPrinter("Canon", str(sumatra))
    job_id = printer.submit(str(pdf))
    assert job_id.startswith("win-complete-")
    assert printer.job_status(job_id) is JobStatus.COMPLETED


def test_windows_submit_sumatra_failure_is_actionable(monkeypatch, pdf, sumatra):
    install(
        monkeypatch,
        {
            "powershell.exe": (0, "[]", ""),
            str(sumatra): (4, "", ""),
        },
    )
    with pytest.raises(PrinterUnavailable, match="printer does not exist"):
        WindowsPrinter("Missing", str(sumatra)).submit(str(pdf))


def test_windows_submit_sumatra_timeout_is_actionable(monkeypatch, pdf, sumatra):
    install(
        monkeypatch,
        {
            "powershell.exe": (0, "[]", ""),
            str(sumatra): subprocess.TimeoutExpired([str(sumatra)], 60),
        },
    )
    with pytest.raises(PrinterUnavailable, match="timed out"):
        WindowsPrinter("Canon", str(sumatra)).submit(str(pdf))


def test_windows_submit_requires_sumatra(pdf, tmp_path):
    with pytest.raises(PrinterUnavailable, match="SumatraPDF not found"):
        WindowsPrinter("Canon", str(tmp_path / "missing.exe")).submit(str(pdf))


def test_windows_submit_refuses_unqueryable_queue(monkeypatch, pdf, sumatra):
    install(monkeypatch, {"powershell.exe": (1, "", "no such printer")})
    with pytest.raises(PrinterUnavailable, match="print queue"):
        WindowsPrinter("Missing", str(sumatra)).submit(str(pdf))


# --- job status -----------------------------------------------------------


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("Normal", JobStatus.PENDING),
        ("Paused", JobStatus.PENDING),
        ("Offline", JobStatus.PENDING),
        ("PaperOut", JobStatus.PENDING),
        ("Error", JobStatus.PENDING),
        ("Printing", JobStatus.PRINTING),
        ("Printed", JobStatus.COMPLETED),
        ("Deleting", JobStatus.CANCELLED),
        ("Cancelled", JobStatus.CANCELLED),
    ],
)
def test_windows_job_status_maps_live_spooler_states(
    monkeypatch, sumatra, native, expected
):
    install(
        monkeypatch,
        {
            "powershell.exe": (
                0,
                jobs(
                    {"ID": 42, "DocumentName": "print.pdf", "JobStatus": native}
                ),
                "",
            )
        },
    )
    assert WindowsPrinter("Canon", str(sumatra)).job_status("win-42") is expected


def test_windows_observed_job_disappearing_means_completed(monkeypatch, sumatra):
    install(monkeypatch, {"powershell.exe": (0, "[]", "")})
    assert (
        WindowsPrinter("Canon", str(sumatra)).job_status("win-42")
        is JobStatus.COMPLETED
    )


def test_windows_job_query_failure_stays_pending_to_avoid_duplicate(
    monkeypatch, sumatra
):
    install(monkeypatch, {"powershell.exe": (1, "", "spooler unavailable")})
    assert WindowsPrinter("Canon", str(sumatra)).job_status("win-42") is JobStatus.PENDING


def test_windows_foreign_job_id_is_unknown(sumatra):
    assert WindowsPrinter("Canon", str(sumatra)).job_status("cups-42") is JobStatus.UNKNOWN


# --- availability ---------------------------------------------------------


def test_windows_available_requires_sumatra_and_queue(monkeypatch, sumatra, tmp_path):
    install(monkeypatch, {"powershell.exe": (0, "[]", "")})
    assert WindowsPrinter("Canon", str(sumatra)).available() is True
    assert WindowsPrinter("Canon", str(tmp_path / "missing.exe")).available() is False


def test_windows_available_false_when_queue_query_fails(monkeypatch, sumatra):
    install(monkeypatch, {"powershell.exe": (1, "", "spooler unavailable")})
    assert WindowsPrinter("Canon", str(sumatra)).available() is False
