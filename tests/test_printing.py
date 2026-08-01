import subprocess

import pytest

from labelagent import printing
from labelagent.config import Config
from labelagent.printing import (
    CupsPrinter,
    FilePrinter,
    JobStatus,
    Printer,
    PrinterUnavailable,
    make_printer,
)


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "print.pdf"
    path.write_bytes(b"%PDF-1.4 fake\n")
    return path


class FakeRun:
    """Stands in for subprocess.run.

    Results are keyed by argv[0]; a list value is consumed one entry per call.
    An entry is either an Exception to raise or (returncode, stdout, stderr).
    """

    def __init__(self, results: dict):
        self.results = results
        self.calls: list[list[str]] = []

    def __call__(self, cmd, capture_output=False, text=False, timeout=None, check=False):
        self.calls.append(cmd)
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


# --- FilePrinter -----------------------------------------------------------


def test_file_printer_copies_and_increments(tmp_path, pdf):
    out = tmp_path / "printed"
    printer = FilePrinter(out)

    first = printer.submit(str(pdf))
    second = printer.submit(str(pdf))

    assert first == "file-1"
    assert second == "file-2"
    assert (out / "file-1-print.pdf").read_bytes() == pdf.read_bytes()
    assert (out / "file-2-print.pdf").exists()
    assert printer.jobs[first] == out / "file-1-print.pdf"


def test_file_printer_creates_out_dir(tmp_path):
    out = tmp_path / "nested" / "printed"
    FilePrinter(out)
    assert out.is_dir()


def test_file_printer_status_and_availability(tmp_path, pdf):
    printer = FilePrinter(tmp_path / "printed")
    job_id = printer.submit(str(pdf))
    assert printer.job_status(job_id) is JobStatus.COMPLETED
    assert printer.available() is True


def test_file_printer_missing_file_raises(tmp_path):
    printer = FilePrinter(tmp_path / "printed")
    with pytest.raises(PrinterUnavailable):
        printer.submit(str(tmp_path / "nope.pdf"))


def test_file_printer_satisfies_protocol(tmp_path):
    assert isinstance(FilePrinter(tmp_path / "printed"), Printer)


# --- make_printer ----------------------------------------------------------


def test_make_printer_defaults_to_file(tmp_path):
    printer = make_printer(Config(data_dir=str(tmp_path)))
    assert isinstance(printer, FilePrinter)
    assert printer.out_dir == tmp_path / "printed"


def test_make_printer_file_keyword(tmp_path):
    assert isinstance(
        make_printer(Config(data_dir=str(tmp_path), printer_name="file")), FilePrinter
    )


def test_make_printer_cups(tmp_path):
    printer = make_printer(
        Config(
            data_dir=str(tmp_path),
            printer_name="Canon_TS9521",
            print_media="na_index-4x6_4x6in",
            print_media_source="rear",
        )
    )
    assert isinstance(printer, CupsPrinter)
    assert printer.printer_name == "Canon_TS9521"
    assert printer.media == "na_index-4x6_4x6in"
    assert printer.media_source == "rear"


# --- CupsPrinter.submit ----------------------------------------------------


def test_cups_submit_builds_command_and_parses_job_id(monkeypatch, pdf):
    fake = install(
        monkeypatch, {"lp": (0, "request id is Canon_TS9521-42 (1 file(s))\n", "")}
    )
    printer = CupsPrinter("Canon_TS9521", "na_index-4x6_4x6in", "rear")

    assert printer.submit(str(pdf)) == "Canon_TS9521-42"
    assert fake.calls[0] == [
        "lp",
        "-d",
        "Canon_TS9521",
        "-o",
        "media=na_index-4x6_4x6in",
        "-o",
        "media-source=rear",
        str(pdf),
    ]


def test_cups_submit_omits_empty_options(monkeypatch, pdf):
    fake = install(monkeypatch, {"lp": (0, "request id is P-1 (1 file(s))\n", "")})
    CupsPrinter("P", "", "").submit(str(pdf))
    assert fake.calls[0] == ["lp", "-d", "P", str(pdf)]


def test_cups_submit_raises_on_nonzero_exit(monkeypatch, pdf):
    install(monkeypatch, {"lp": (1, "", "lp: Error - scheduler not responding\n")})
    with pytest.raises(PrinterUnavailable, match="scheduler not responding"):
        CupsPrinter("P").submit(str(pdf))


def test_cups_submit_raises_on_timeout(monkeypatch, pdf):
    install(monkeypatch, {"lp": subprocess.TimeoutExpired(["lp"], 10)})
    with pytest.raises(PrinterUnavailable, match="timed out"):
        CupsPrinter("P").submit(str(pdf))


def test_cups_submit_raises_when_lp_missing(monkeypatch, pdf):
    install(monkeypatch, {"lp": FileNotFoundError("lp")})
    with pytest.raises(PrinterUnavailable, match="CUPS"):
        CupsPrinter("P").submit(str(pdf))


def test_cups_submit_raises_on_unparseable_output(monkeypatch, pdf):
    install(monkeypatch, {"lp": (0, "queued something somewhere\n", "")})
    with pytest.raises(PrinterUnavailable, match="could not parse"):
        CupsPrinter("P").submit(str(pdf))


# --- CupsPrinter.job_status ------------------------------------------------

PENDING_OUT = "Canon_TS9521-42 brian 41984 Sat 01 Aug 2026 03:16:00 PM EDT\n"
COMPLETED_OUT = "Canon_TS9521-42 brian 41984 Sat 01 Aug 2026 03:16:04 PM EDT\n"


def test_job_status_pending(monkeypatch):
    fake = install(monkeypatch, {"lpstat": (0, PENDING_OUT, "")})
    assert CupsPrinter("Canon_TS9521").job_status("Canon_TS9521-42") is JobStatus.PENDING
    assert fake.calls == [["lpstat", "-W", "not-completed", "-o"]]


def test_job_status_completed(monkeypatch):
    fake = install(
        monkeypatch, {"lpstat": [(0, "", ""), (0, COMPLETED_OUT, "")]}
    )

    assert (
        CupsPrinter("Canon_TS9521").job_status("Canon_TS9521-42") is JobStatus.COMPLETED
    )
    assert fake.calls == [
        ["lpstat", "-W", "not-completed", "-o"],
        ["lpstat", "-W", "completed", "-o"],
    ]


def test_job_status_failed_when_job_is_in_neither_queue(monkeypatch):
    install(monkeypatch, {"lpstat": (0, "", "")})
    assert CupsPrinter("P").job_status("P-42") is JobStatus.FAILED


def test_job_status_unreachable_on_nonzero_exit(monkeypatch):
    install(monkeypatch, {"lpstat": (1, "", "lpstat: Error - no such printer\n")})
    assert CupsPrinter("P").job_status("P-42") is JobStatus.UNREACHABLE


def test_job_status_unreachable_on_timeout(monkeypatch):
    install(monkeypatch, {"lpstat": subprocess.TimeoutExpired(["lpstat"], 10)})
    assert CupsPrinter("P").job_status("P-42") is JobStatus.UNREACHABLE


def test_job_status_does_not_match_job_id_prefixes(monkeypatch):
    install(monkeypatch, {"lpstat": (0, "P-420 brian 41984 Sat 01 Aug 2026\n", "")})
    assert CupsPrinter("P").job_status("P-42") is JobStatus.FAILED


# --- CupsPrinter.available -------------------------------------------------


def test_available_true(monkeypatch):
    fake = install(monkeypatch, {"lpstat": (0, "printer P is idle.  enabled\n", "")})
    assert CupsPrinter("P").available() is True
    assert fake.calls == [["lpstat", "-p", "P"]]


def test_available_false_on_nonzero_exit(monkeypatch):
    install(monkeypatch, {"lpstat": (1, "", "lpstat: Error - no such printer\n")})
    assert CupsPrinter("P").available() is False


def test_available_false_on_timeout(monkeypatch):
    install(monkeypatch, {"lpstat": subprocess.TimeoutExpired(["lpstat"], 10)})
    assert CupsPrinter("P").available() is False


def test_available_false_when_lpstat_missing(monkeypatch):
    install(monkeypatch, {"lpstat": FileNotFoundError("lpstat")})
    assert CupsPrinter("P").available() is False
