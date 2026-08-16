"""FastAPI control panel for the label agent (LAN only, no auth per SPEC §7)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

import fitz
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import __version__, storage
from ..config import DEFAULT_CONFIG_FILE, Config
from ..db import Database
from ..models import Label, LabelStatus, Level, Stage

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

PREVIEW_DPI = 120
HISTORY_CHART_DAYS = 14

PENDING_STATUSES = (
    LabelStatus.READY,
    LabelStatus.QUEUED,
    LabelStatus.WAITING_FOR_PRINTER,
)
ATTENTION_STATUSES = (
    LabelStatus.NEEDS_REVIEW,
    LabelStatus.WAITING_FOR_PRINTER,
    LabelStatus.DUPLICATE,
    LabelStatus.FAILED,
)
PROBLEM_LEVELS = (Level.WARN, Level.ERROR)
TRUTHY = {"1", "true", "yes", "on"}


class AgentController(Protocol):
    """Runtime surface the web app drives; implemented by the service (Wave 3)."""

    def state(self) -> dict: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def set_auto_print(self, on: bool) -> None: ...

    def check_now(self) -> dict: ...

    def print_label(self, label_id: int) -> dict: ...

    def test_print(self) -> dict: ...


class StubController:
    """In-memory AgentController for tests and demos.

    Mutating calls are appended to ``.calls`` as ``(name, argument)`` tuples;
    ``state()`` is a read and is not recorded.
    """

    def __init__(
        self,
        agent_state: str = "running",
        auto_print: bool = True,
        printer_available: bool = True,
        printer_name: str = "Canon_TS9521",
        last_poll_at: str | None = "2026-08-01T09:30:00",
    ):
        self.agent_state = agent_state
        self.auto_print = auto_print
        self.printer_available = printer_available
        self.printer_name = printer_name
        self.last_poll_at = last_poll_at
        self.calls: list[tuple[str, Any]] = []

    def state(self) -> dict:
        return {
            "agent_state": self.agent_state,
            "auto_print": self.auto_print,
            "printer_available": self.printer_available,
            "printer_name": self.printer_name,
            "last_poll_at": self.last_poll_at,
        }

    def pause(self) -> None:
        self.calls.append(("pause", None))
        self.agent_state = "paused"

    def resume(self) -> None:
        self.calls.append(("resume", None))
        self.agent_state = "running"

    def set_auto_print(self, on: bool) -> None:
        self.calls.append(("set_auto_print", on))
        self.auto_print = bool(on)

    def check_now(self) -> dict:
        self.calls.append(("check_now", None))
        self.last_poll_at = "2026-08-01T09:45:00"
        return {"checked": 3, "new_labels": 0, "detail": "No new label emails."}

    def print_label(self, label_id: int) -> dict:
        self.calls.append(("print_label", label_id))
        return {"ok": True, "detail": f"Label {label_id} queued for printing."}

    # optional extensions beyond AgentController
    def print_original(self, label_id: int) -> dict:
        self.calls.append(("print_original", label_id))
        return {"ok": True, "detail": f"Original PDF for label {label_id} queued."}

    def set_printer_name(self, name: str) -> None:
        self.calls.append(("set_printer_name", name))
        self.printer_name = name

    def reschedule_poll(self) -> int | None:
        self.calls.append(("reschedule_poll", None))
        return None

    def test_print(self) -> dict:
        self.calls.append(("test_print", None))
        return {"ok": True, "detail": "Calibration page sent to the printer."}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in TRUTHY


def _today() -> str:
    return date.today().isoformat()


def _shift_day(day: str, delta: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=delta)).isoformat()


def _valid_day(value: str | None) -> str:
    try:
        return date.fromisoformat(value or "").isoformat()
    except ValueError:
        return _today()


def _label_dict(label: Label) -> dict:
    return asdict(label)


def _pdf_path(config: Config, label: Label, which: str) -> Path:
    if which == "original":
        return (
            Path(label.original_path)
            if label.original_path
            else storage.original_pdf_path(config.data_dir, label.id)
        )
    return (
        Path(label.print_path)
        if label.print_path
        else storage.print_pdf_path(config.data_dir, label.id)
    )


def _render_preview(pdf_path: Path, png_path: Path, dpi: int = PREVIEW_DPI) -> None:
    with fitz.open(str(pdf_path)) as doc:
        if doc.page_count == 0:
            raise HTTPException(status_code=404, detail="pdf has no pages")
        pixmap = doc.load_page(0).get_pixmap(dpi=dpi)
        png_path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(png_path))


async def _payload(request: Request) -> dict:
    """Read a POST body as a dict from JSON, form data, or an empty body."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            data = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        return {key: form.getlist(key)[-1] for key in form.keys()}
    return {}


def create_app(db: Database, config: Config, controller: AgentController) -> FastAPI:
    app = FastAPI(title="Label Agent", version=__version__)
    app.state.db = db
    app.state.config = config
    app.state.controller = controller

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["hhmm"] = lambda value: (value or "")[11:16]
    templates.env.filters["day"] = lambda value: (value or "")[:10]
    templates.env.filters["pretty"] = lambda value: str(value or "").replace("_", " ")
    templates.env.globals["version"] = __version__

    # --- shared queries -------------------------------------------------

    def today_counts() -> dict:
        day = _today()
        metrics = db.daily_metrics(day, day)
        printed = metrics[0]["printed"] if metrics else 0
        pending = sum(len(db.list_labels(status=status)) for status in PENDING_STATUSES)
        needs_review = len(db.list_labels(status=LabelStatus.NEEDS_REVIEW))
        errors = sum(
            1
            for event in db.list_events(level=Level.ERROR, limit=500)
            if (event.created_at or "").startswith(day)
        )
        return {
            "date": day,
            "printed": printed,
            "pending": pending,
            "needs_review": needs_review,
            "errors": errors,
        }

    def status_context() -> dict:
        return {"state": controller.state(), "counts": today_counts()}

    def _submit_print(label_id: int, which: str) -> dict:
        """Print the normalized label, or the untouched original when asked.

        ``print_original`` is an optional controller extension; builds without it
        say so rather than quietly printing the wrong file.
        """
        if which != "original":
            return controller.print_label(label_id)
        print_original = getattr(controller, "print_original", None)
        if print_original is None:
            return {
                "ok": False,
                "detail": "This build can only print the normalized 4x6 label.",
            }
        return print_original(label_id)

    def get_label_or_404(label_id: int) -> Label:
        label = db.get_label(label_id)
        if label is None:
            raise HTTPException(status_code=404, detail="label not found")
        return label

    def wants_fragment(request: Request) -> bool:
        return request.headers.get("HX-Request") is not None

    def status_card(request: Request) -> Response:
        return templates.TemplateResponse(
            request, "fragments/status_card.html", status_context()
        )

    def attention_labels() -> list[Label]:
        labels: list[Label] = []
        for status in ATTENTION_STATUSES:
            labels.extend(db.list_labels(status=status))
        labels.sort(key=lambda entry: (entry.created_at or "", entry.id or 0), reverse=True)
        return labels

    def chart_days(end_day: str, days: int = HISTORY_CHART_DAYS) -> list[dict]:
        start_day = _shift_day(end_day, -(days - 1))
        printed = {row["date"]: row["printed"] for row in db.daily_metrics(start_day, end_day)}
        series = [
            {"date": _shift_day(start_day, offset), "printed": 0} for offset in range(days)
        ]
        for entry in series:
            entry["printed"] = printed.get(entry["date"], 0)
        top = max([entry["printed"] for entry in series] + [1])
        for entry in series:
            entry["pct"] = round(100 * entry["printed"] / top)
        return series

    # --- pages ----------------------------------------------------------

    @app.get("/")
    def dashboard(request: Request) -> Response:
        context = status_context()
        context.update(
            active="dashboard",
            attention=attention_labels(),
            recent=db.list_labels(date=_today(), limit=50),
        )
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.get("/history")
    def history(request: Request, date: str | None = None) -> Response:
        day = _valid_day(date)
        context = {
            "active": "history",
            "day": day,
            "prev_day": _shift_day(day, -1),
            "next_day": _shift_day(day, 1),
            "is_today": day == _today(),
            "labels": db.list_labels(date=day, limit=200),
            "chart": chart_days(day),
        }
        return templates.TemplateResponse(request, "history.html", context)

    @app.get("/labels/{label_id}")
    def label_detail(request: Request, label_id: int) -> Response:
        label = get_label_or_404(label_id)
        context = {
            "active": "",
            "label": label,
            "events": db.list_events(label_id=label_id, limit=100),
            "needs_attention": label.status
            in (LabelStatus.NEEDS_REVIEW, LabelStatus.FAILED),
            "has_print_pdf": _pdf_path(config, label, "print").is_file(),
            "has_original_pdf": _pdf_path(config, label, "original").is_file(),
        }
        return templates.TemplateResponse(request, "label_detail.html", context)

    @app.get("/errors")
    def errors(request: Request, stage: str | None = None) -> Response:
        selected = stage if stage in set(Stage) else None
        events = [
            event
            for event in db.list_events(stage=selected, limit=500)
            if event.level in PROBLEM_LEVELS
        ][:200]
        context = {
            "active": "errors",
            "events": events,
            "stages": list(Stage),
            "selected_stage": selected,
        }
        return templates.TemplateResponse(request, "errors.html", context)

    @app.get("/settings")
    def settings(request: Request, saved: int | None = None) -> Response:
        state = controller.state()
        context = {
            "active": "settings",
            "saved": bool(saved),
            "state": state,
            "poll_interval_min": db.get_setting(
                "poll_interval_min", str(config.poll_interval_min)
            ),
            "auto_print": _as_bool(
                db.get_setting("auto_print", "on" if config.auto_print else "off")
            ),
            "printer_name": db.get_setting("printer_name", config.printer_name),
            "config_path": str(Path(getattr(config, "config_path", DEFAULT_CONFIG_FILE)).resolve()),
            "data_dir": str(Path(config.data_dir).resolve()),
            "db_path": str(Path(config.db_path).resolve()),
        }
        return templates.TemplateResponse(request, "settings.html", context)

    # --- agent API ------------------------------------------------------

    @app.get("/api/dashboard/counts")
    def api_dashboard_counts(request: Request) -> Response:
        return templates.TemplateResponse(
            request, "fragments/today_counts.html", {"counts": today_counts()}
        )

    @app.get("/api/status")
    def api_status() -> dict:
        return {**controller.state(), "today": today_counts()}

    @app.post("/api/agent/pause")
    def api_pause(request: Request) -> Response:
        controller.pause()
        db.set_setting("agent_state", "paused")
        if wants_fragment(request):
            return status_card(request)
        return JSONResponse(controller.state())

    @app.post("/api/agent/resume")
    def api_resume(request: Request) -> Response:
        controller.resume()
        db.set_setting("agent_state", "running")
        if wants_fragment(request):
            return status_card(request)
        return JSONResponse(controller.state())

    @app.post("/api/agent/auto-print")
    async def api_auto_print(request: Request) -> Response:
        # Stays async to read the body; the rest is blocking, so it goes to the
        # threadpool like every other handler here.
        on = _as_bool((await _payload(request)).get("on"))

        def apply() -> Response:
            controller.set_auto_print(on)
            db.set_setting("auto_print", "on" if on else "off")
            if wants_fragment(request):
                return status_card(request)
            return JSONResponse(controller.state())

        return await run_in_threadpool(apply)

    @app.post("/api/agent/check-now")
    def api_check_now(request: Request) -> Response:
        summary = controller.check_now()
        if wants_fragment(request):
            return templates.TemplateResponse(
                request,
                "fragments/check_result.html",
                {"summary": summary, "state": controller.state()},
            )
        return JSONResponse(summary)

    @app.post("/api/agent/test-print")
    def api_test_print(request: Request) -> Response:
        result = controller.test_print()
        if wants_fragment(request):
            return templates.TemplateResponse(
                request, "fragments/action_result.html", {"result": result}
            )
        return JSONResponse(result)

    # --- label API ------------------------------------------------------

    @app.get("/api/labels")
    def api_labels(
        date: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        labels = db.list_labels(date=date, status=status, limit=limit)
        return [_label_dict(label) for label in labels]

    @app.post("/api/labels/{label_id}/print")
    def api_print_label(
        request: Request, label_id: int, which: str = "print"
    ) -> Response:
        get_label_or_404(label_id)
        result = _submit_print(label_id, which)
        if wants_fragment(request):
            return templates.TemplateResponse(
                request, "fragments/action_result.html", {"result": result}
            )
        return JSONResponse(result)

    @app.get("/api/labels/{label_id}/preview.png")
    def api_preview(label_id: int, which: str = "print") -> Response:
        if which not in ("print", "original"):
            raise HTTPException(status_code=400, detail="which must be print or original")
        label = get_label_or_404(label_id)
        pdf_path = _pdf_path(config, label, which)
        if not pdf_path.is_file():
            raise HTTPException(status_code=404, detail=f"{which} pdf not found")

        png_path = storage.preview_png_path(config.data_dir, label_id, which)
        if not png_path.is_file() or png_path.stat().st_mtime < pdf_path.stat().st_mtime:
            _render_preview(pdf_path, png_path)
        return FileResponse(png_path, media_type="image/png")

    @app.get("/api/labels/{label_id}/original.pdf")
    def api_original_pdf(label_id: int) -> Response:
        return _pdf_response(label_id, "original")

    @app.get("/api/labels/{label_id}/print.pdf")
    def api_print_pdf(label_id: int) -> Response:
        return _pdf_response(label_id, "print")

    def _pdf_response(label_id: int, which: str) -> FileResponse:
        label = get_label_or_404(label_id)
        pdf_path = _pdf_path(config, label, which)
        if not pdf_path.is_file():
            raise HTTPException(status_code=404, detail=f"{which} pdf not found")
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=f"label-{label_id}-{which}.pdf",
        )

    # --- metrics & settings API -----------------------------------------

    @app.get("/api/metrics/daily")
    def api_metrics_daily(
        date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"),
    ) -> list[dict]:
        end = _valid_day(date_to)
        start = _valid_day(date_from) if date_from else _shift_day(end, -13)
        return db.daily_metrics(start, end)

    @app.post("/api/settings")
    async def api_settings(request: Request) -> Response:
        data = await _payload(request)
        updated: dict[str, str] = {}

        if "poll_interval_min" in data:
            updated["poll_interval_min"] = str(data["poll_interval_min"]).strip()
        if "printer_name" in data:
            updated["printer_name"] = str(data["printer_name"]).strip()
        if "auto_print" in data:
            updated["auto_print"] = "on" if _as_bool(data["auto_print"]) else "off"

        def apply() -> None:
            for key, value in updated.items():
                db.set_setting(key, value)
            if "auto_print" in updated:
                controller.set_auto_print(updated["auto_print"] == "on")
            # A printer named here has to reach the running print worker, or
            # labels keep stacking up in waiting_for_printer against the old
            # queue while the UI shows the corrected name.
            set_printer_name = getattr(controller, "set_printer_name", None)
            if "printer_name" in updated and set_printer_name is not None:
                set_printer_name(updated["printer_name"])
            # Same story for the interval: the poll job baked one in at startup,
            # so a number saved here means nothing until the timer is retimed.
            reschedule_poll = getattr(controller, "reschedule_poll", None)
            if "poll_interval_min" in updated and reschedule_poll is not None:
                reschedule_poll()

        await run_in_threadpool(apply)

        is_form = request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        )
        if is_form:
            return RedirectResponse("/settings?saved=1", status_code=303)
        return JSONResponse({"updated": updated})

    return app


__all__ = ["AgentController", "StubController", "create_app"]
