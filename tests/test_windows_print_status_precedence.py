import json

from labelagent import printing
from labelagent.printing import JobStatus, WindowsPrinter


def _install_job(monkeypatch, sumatra_path, status):
    class FakeRun:
        def __call__(
            self,
            cmd,
            capture_output=False,
            text=False,
            timeout=None,
            check=False,
            env=None,
        ):
            payload = json.dumps(
                [{"ID": 42, "DocumentName": "print.pdf", "JobStatus": status}]
            )
            return printing.subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(printing.subprocess, "run", FakeRun())
    return WindowsPrinter("Canon", str(sumatra_path))


def test_printed_deleting_is_completed(monkeypatch, tmp_path):
    sumatra = tmp_path / "SumatraPDF.exe"
    sumatra.write_bytes(b"fake")
    printer = _install_job(monkeypatch, sumatra, "Printed, Deleting")

    assert printer.job_status("win-42") is JobStatus.COMPLETED


def test_completed_deleting_is_completed(monkeypatch, tmp_path):
    sumatra = tmp_path / "SumatraPDF.exe"
    sumatra.write_bytes(b"fake")
    printer = _install_job(monkeypatch, sumatra, "Completed, Deleting")

    assert printer.job_status("win-42") is JobStatus.COMPLETED


def test_deleting_without_success_is_cancelled(monkeypatch, tmp_path):
    sumatra = tmp_path / "SumatraPDF.exe"
    sumatra.write_bytes(b"fake")
    printer = _install_job(monkeypatch, sumatra, "Deleting")

    assert printer.job_status("win-42") is JobStatus.CANCELLED
