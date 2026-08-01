import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from labelagent import storage
from labelagent.config import Config
from labelagent.db import Database
from labelagent.models import Label, LabelStatus, Level, Platform, Stage
from labelagent.web.app import StubController, create_app

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


@dataclass
class Env:
    client: TestClient
    db: Database
    config: Config
    controller: StubController


def make_pdf(path: Path, text: str = "LABEL", width: float = 288, height: float = 432) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((40, 60), text, fontsize=24)
    doc.save(str(path))
    doc.close()
    return path


def add_label(db: Database, **kwargs) -> Label:
    defaults = dict(
        platform=Platform.POSHMARK,
        item_title="Blue Sweater",
        order_ref="ORD-1",
        tracking_number="9400111",
        gmail_message_id=f"msg-{kwargs.get('gmail_message_id', id(kwargs))}",
        created_at=f"{TODAY}T09:00:00",
    )
    defaults.update(kwargs)
    return db.insert_label(Label(**defaults))


def seed(db: Database) -> dict[str, Label]:
    labels = {
        "printed": add_label(
            db,
            gmail_message_id="m-printed",
            item_title="Red Coat",
            status=LabelStatus.PRINTED,
            printed_at=f"{TODAY}T09:05:00",
            print_count=1,
        ),
        "ready": add_label(
            db,
            gmail_message_id="m-ready",
            item_title="Green Scarf",
            platform=Platform.VINTED,
            tracking_number="9400222",
            status=LabelStatus.READY,
        ),
        "needs_review": add_label(
            db,
            gmail_message_id="m-review",
            item_title="Denim Jacket",
            platform=Platform.VINTED,
            tracking_number="9400333",
            status=LabelStatus.NEEDS_REVIEW,
            status_detail="barcode cut off",
        ),
        "waiting": add_label(
            db,
            gmail_message_id="m-waiting",
            item_title="Wool Hat",
            tracking_number="9400444",
            status=LabelStatus.WAITING_FOR_PRINTER,
            status_detail="printer unreachable since 14:32",
        ),
        "duplicate": add_label(
            db,
            gmail_message_id="m-dupe",
            item_title="Wool Hat",
            tracking_number="9400444",
            status=LabelStatus.DUPLICATE,
        ),
        "failed": add_label(
            db,
            gmail_message_id="m-failed",
            item_title="Silk Dress",
            tracking_number="9400555",
            status=LabelStatus.FAILED,
            status_detail="cups error",
        ),
        "yesterday": add_label(
            db,
            gmail_message_id="m-old",
            item_title="Old Boots",
            tracking_number="9400666",
            status=LabelStatus.PRINTED,
            created_at=f"{YESTERDAY}T08:00:00",
            printed_at=f"{YESTERDAY}T08:10:00",
            print_count=1,
        ),
    }

    db.add_event(Stage.INGEST, Level.INFO, "downloaded attachment", labels["printed"].id)
    db.add_event(Stage.PRINT, Level.INFO, "job completed", labels["printed"].id)
    db.add_event(Stage.VERIFY, Level.WARN, "low confidence crop", labels["needs_review"].id)
    db.add_event(Stage.CROP, Level.ERROR, "no bounding box found", labels["needs_review"].id)
    db.add_event(Stage.PRINT, Level.ERROR, "cups error", labels["failed"].id)
    db.add_event(Stage.SYSTEM, Level.ERROR, "gmail unreachable")
    return labels


@pytest.fixture
def env(tmp_path):
    config = Config(data_dir=str(tmp_path / "data"))
    db = Database(config.db_path)
    db.init()
    controller = StubController()
    client = TestClient(create_app(db, config, controller))
    yield Env(client=client, db=db, config=config, controller=controller)
    db.close()


@pytest.fixture
def seeded(env):
    env.labels = seed(env.db)
    return env


# --- pages ---------------------------------------------------------------


def test_dashboard_renders(seeded):
    response = seeded.client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Running" in body
    assert seeded.controller.printer_name in body
    assert "Denim Jacket" in body  # needs_review shows in the attention list
    assert "Wool Hat" in body  # waiting_for_printer / duplicate
    assert "Red Coat" in body  # today's labels
    assert "Old Boots" not in body  # yesterday belongs to history
    assert 'hx-post="/api/agent/pause"' in body


def test_dashboard_chips_show_today_counts(seeded):
    body = seeded.client.get("/").text
    # printed today = 1, pending (ready + waiting) = 2, needs review = 1, errors today = 3
    assert ">1</span><span class=\"l\">printed today</span>" in body
    assert ">2</span><span class=\"l\">pending</span>" in body
    assert ">3</span><span class=\"l\">errors today</span>" in body


def test_dashboard_when_empty(env):
    response = env.client.get("/")
    assert response.status_code == 200
    assert "Nothing waiting" in response.text


def test_history_defaults_to_today(seeded):
    response = seeded.client.get("/history")
    assert response.status_code == 200
    assert "Red Coat" in response.text
    assert "Old Boots" not in response.text
    assert f'value="{TODAY}"' in response.text


def test_history_date_filtering(seeded):
    response = seeded.client.get("/history", params={"date": YESTERDAY})
    assert response.status_code == 200
    assert "Old Boots" in response.text
    assert "Red Coat" not in response.text
    assert f"/history?date={(date.fromisoformat(YESTERDAY) - timedelta(days=1)).isoformat()}" in response.text


def test_history_chart_has_14_bars(seeded):
    body = seeded.client.get("/history").text
    assert body.count('class="bar"') == 14


def test_history_bad_date_falls_back_to_today(seeded):
    response = seeded.client.get("/history", params={"date": "not-a-date"})
    assert response.status_code == 200
    assert f'value="{TODAY}"' in response.text


def test_label_detail_renders(seeded):
    label = seeded.labels["needs_review"]
    response = seeded.client.get(f"/labels/{label.id}")
    assert response.status_code == 200
    body = response.text
    assert "Denim Jacket" in body
    assert "9400333" in body  # tracking in the metadata table
    assert "barcode cut off" in body  # status detail is prominent
    assert "no bounding box found" in body  # event timeline
    assert "Print original" in body


def test_label_detail_404(env):
    response = env.client.get("/labels/999")
    assert response.status_code == 404
    assert response.json()["detail"] == "label not found"


def test_errors_page_lists_warn_and_error_only(seeded):
    response = seeded.client.get("/errors")
    assert response.status_code == 200
    body = response.text
    assert "gmail unreachable" in body
    assert "low confidence crop" in body
    assert "downloaded attachment" not in body  # info level is filtered out
    assert f'href="/labels/{seeded.labels["failed"].id}"' in body


def test_errors_page_stage_filter(seeded):
    response = seeded.client.get("/errors", params={"stage": "crop"})
    assert response.status_code == 200
    assert "no bounding box found" in response.text
    assert "gmail unreachable" not in response.text


def test_settings_page_shows_stored_values(env):
    env.db.set_setting("poll_interval_min", "7")
    env.db.set_setting("printer_name", "Kitchen_Canon")
    response = env.client.get("/settings")
    assert response.status_code == 200
    assert 'value="7"' in response.text
    assert "Kitchen_Canon" in response.text
    assert "no restart" in response.text


# --- agent actions -------------------------------------------------------


def test_pause_and_resume(env):
    response = env.client.post("/api/agent/pause")
    assert response.status_code == 200
    assert ("pause", None) in env.controller.calls
    assert response.json()["agent_state"] == "paused"
    assert env.db.get_setting("agent_state") == "paused"

    response = env.client.post("/api/agent/resume")
    assert ("resume", None) in env.controller.calls
    assert response.json()["agent_state"] == "running"
    assert env.db.get_setting("agent_state") == "running"


def test_pause_via_htmx_returns_status_card(env):
    response = env.client.post("/api/agent/pause", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="status-card"' in response.text
    assert "Paused" in response.text
    assert 'hx-post="/api/agent/resume"' in response.text


def test_auto_print_toggle(env):
    response = env.client.post("/api/agent/auto-print", json={"on": False})
    assert response.status_code == 200
    assert ("set_auto_print", False) in env.controller.calls
    assert response.json()["auto_print"] is False
    assert env.db.get_setting("auto_print") == "off"

    env.client.post("/api/agent/auto-print", data={"on": "true"})
    assert ("set_auto_print", True) in env.controller.calls
    assert env.db.get_setting("auto_print") == "on"


def test_check_now(env):
    response = env.client.post("/api/agent/check-now")
    assert response.status_code == 200
    assert ("check_now", None) in env.controller.calls
    assert response.json()["checked"] == 3

    fragment = env.client.post("/api/agent/check-now", headers={"HX-Request": "true"})
    assert "No new label emails." in fragment.text


def test_test_print(env):
    response = env.client.post("/api/agent/test-print")
    assert response.status_code == 200
    assert ("test_print", None) in env.controller.calls
    assert response.json()["ok"] is True

    fragment = env.client.post("/api/agent/test-print", headers={"HX-Request": "true"})
    assert "Calibration page" in fragment.text


def test_print_label_routes_to_controller(seeded):
    label = seeded.labels["ready"]
    response = seeded.client.post(f"/api/labels/{label.id}/print")
    assert response.status_code == 200
    assert ("print_label", label.id) in seeded.controller.calls
    assert response.json() == {
        "ok": True,
        "detail": f"Label {label.id} queued for printing.",
    }


def test_print_label_htmx_fragment(seeded):
    label = seeded.labels["ready"]
    response = seeded.client.post(
        f"/api/labels/{label.id}/print", headers={"HX-Request": "true"}
    )
    assert "queued for printing" in response.text
    assert "text/html" in response.headers["content-type"]


def test_print_original_uses_separate_call(seeded):
    label = seeded.labels["needs_review"]
    response = seeded.client.post(f"/api/labels/{label.id}/print?which=original")
    assert response.status_code == 200
    assert ("print_original", label.id) in seeded.controller.calls
    assert ("print_label", label.id) not in seeded.controller.calls


def test_print_unknown_label_404(env):
    response = env.client.post("/api/labels/42/print")
    assert response.status_code == 404
    assert response.json()["detail"] == "label not found"
    assert env.controller.calls == []


# --- files ---------------------------------------------------------------


def test_preview_png_renders_and_caches(env):
    label = add_label(env.db, gmail_message_id="m-pdf", status=LabelStatus.READY)
    pdf = make_pdf(storage.print_pdf_path(env.config.data_dir, label.id), "PRINT ME")
    env.db.update_label(label.id, print_path=str(pdf))

    response = env.client.get(f"/api/labels/{label.id}/preview.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"

    cached = storage.preview_png_path(env.config.data_dir, label.id, "print")
    assert cached.is_file()
    stamp = cached.stat().st_mtime_ns

    again = env.client.get(f"/api/labels/{label.id}/preview.png")
    assert again.status_code == 200
    assert cached.stat().st_mtime_ns == stamp  # served from cache, not re-rendered


def test_preview_png_original_variant(env):
    label = add_label(env.db, gmail_message_id="m-orig")
    pdf = make_pdf(
        storage.original_pdf_path(env.config.data_dir, label.id), "ORIG", 792, 612
    )
    env.db.update_label(label.id, original_path=str(pdf))

    response = env.client.get(f"/api/labels/{label.id}/preview.png?which=original")
    assert response.status_code == 200
    assert storage.preview_png_path(env.config.data_dir, label.id, "original").is_file()


def test_preview_png_404_when_pdf_missing(env):
    label = add_label(env.db, gmail_message_id="m-nopdf")
    response = env.client.get(f"/api/labels/{label.id}/preview.png")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_preview_png_404_for_unknown_label(env):
    assert env.client.get("/api/labels/77/preview.png").status_code == 404


def test_preview_rejects_unknown_which(env):
    label = add_label(env.db, gmail_message_id="m-which")
    assert env.client.get(f"/api/labels/{label.id}/preview.png?which=sideways").status_code == 400


def test_original_and_print_pdf_download(env):
    label = add_label(env.db, gmail_message_id="m-both")
    make_pdf(storage.original_pdf_path(env.config.data_dir, label.id), "ORIG")
    make_pdf(storage.print_pdf_path(env.config.data_dir, label.id), "PRINT")

    original = env.client.get(f"/api/labels/{label.id}/original.pdf")
    assert original.status_code == 200
    assert original.headers["content-type"] == "application/pdf"
    assert original.content[:5] == b"%PDF-"
    assert f"label-{label.id}-original.pdf" in original.headers["content-disposition"]

    printed = env.client.get(f"/api/labels/{label.id}/print.pdf")
    assert printed.status_code == 200
    assert printed.content[:5] == b"%PDF-"


def test_pdf_404s(env):
    label = add_label(env.db, gmail_message_id="m-none")
    assert env.client.get(f"/api/labels/{label.id}/original.pdf").status_code == 404
    assert env.client.get("/api/labels/999/print.pdf").status_code == 404


def test_static_assets_served(env):
    for path in ("/static/style.css", "/static/htmx.min.js", "/static/manifest.webmanifest"):
        assert env.client.get(path).status_code == 200


# --- json api ------------------------------------------------------------


def test_api_status_shape(seeded):
    payload = seeded.client.get("/api/status").json()
    assert payload["agent_state"] == "running"
    assert payload["auto_print"] is True
    assert payload["printer_available"] is True
    assert payload["printer_name"] == "Canon_TS9521"
    assert payload["last_poll_at"]
    assert payload["today"] == {
        "date": TODAY,
        "printed": 1,
        "pending": 2,
        "needs_review": 1,
        "errors": 3,
    }


def test_api_labels_filters(seeded):
    everything = seeded.client.get("/api/labels").json()
    assert len(everything) == 7
    assert {"id", "platform", "status", "tracking_number"} <= set(everything[0])

    review = seeded.client.get("/api/labels", params={"status": "needs_review"}).json()
    assert [row["item_title"] for row in review] == ["Denim Jacket"]

    old = seeded.client.get("/api/labels", params={"date": YESTERDAY}).json()
    assert [row["item_title"] for row in old] == ["Old Boots"]


def test_api_metrics_daily_passthrough(seeded):
    rows = seeded.client.get(
        "/api/metrics/daily", params={"from": YESTERDAY, "to": TODAY}
    ).json()
    assert rows == seeded.db.daily_metrics(YESTERDAY, TODAY)
    assert {row["date"] for row in rows} == {YESTERDAY, TODAY}


def test_api_metrics_daily_defaults_to_last_14_days(seeded):
    rows = seeded.client.get("/api/metrics/daily").json()
    assert rows == seeded.db.daily_metrics(
        (date.today() - timedelta(days=13)).isoformat(), TODAY
    )


def test_settings_post_form_persists_and_redirects(env):
    response = env.client.post(
        "/api/settings",
        data={"poll_interval_min": "9", "printer_name": "Office_Canon", "auto_print": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"

    assert env.db.get_setting("poll_interval_min") == "9"
    assert env.db.get_setting("printer_name") == "Office_Canon"
    assert env.db.get_setting("auto_print") == "on"
    assert ("set_auto_print", True) in env.controller.calls


def test_settings_form_checkbox_semantics(env):
    # the hidden field always posts "off"; a checked box appends "on" after it
    env.client.post("/api/settings", data={"auto_print": ["off", "on"]})
    assert env.db.get_setting("auto_print") == "on"
    assert env.controller.auto_print is True

    # unchecked: only the hidden field is submitted
    env.client.post("/api/settings", data={"auto_print": "off"})
    assert env.db.get_setting("auto_print") == "off"
    assert env.controller.auto_print is False


def test_settings_post_json(env):
    response = env.client.post("/api/settings", json={"poll_interval_min": 15})
    assert response.status_code == 200
    assert response.json() == {"updated": {"poll_interval_min": "15"}}
    assert env.db.get_setting("poll_interval_min") == "15"
    assert env.db.get_setting("printer_name") is None  # untouched keys stay unset


# --- regressions -------------------------------------------------------------


def test_settings_pushes_the_printer_name_to_the_controller(env):
    """Storing the name is not enough; the running print worker needs it too."""
    env.client.post("/api/settings", json={"printer_name": "Canon_TS9521"})

    assert env.db.get_setting("printer_name") == "Canon_TS9521"
    assert ("set_printer_name", "Canon_TS9521") in env.controller.calls
    assert env.controller.state()["printer_name"] == "Canon_TS9521"


def test_settings_retimes_the_poll_job(env):
    """An interval that never reaches the scheduler is just a number in a table."""
    env.client.post("/api/settings", json={"poll_interval_min": 12})
    assert env.db.get_setting("poll_interval_min") == "12"
    assert ("reschedule_poll", None) in env.controller.calls

    # untouched interval, untouched timer
    env.controller.calls.clear()
    env.client.post("/api/settings", json={"printer_name": "Canon_TS9521"})
    assert ("reschedule_poll", None) not in env.controller.calls


def test_settings_saves_against_a_bare_controller(tmp_path):
    """Retiming and renaming the printer are optional extensions to the protocol.

    A controller that implements only `AgentController` still has to be able to
    save Settings rather than 500 on a missing attribute.
    """

    class BareController:
        def state(self) -> dict:
            return StubController().state()

        def pause(self) -> None: ...

        def resume(self) -> None: ...

        def set_auto_print(self, on: bool) -> None: ...

        def check_now(self) -> dict:
            return {}

        def print_label(self, label_id: int) -> dict:
            return {"ok": True, "detail": ""}

        def test_print(self) -> dict:
            return {"ok": True, "detail": ""}

    db = Database(tmp_path / "bare.db")
    db.init()
    config = Config(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "bare.db"))
    client = TestClient(create_app(db, config, BareController()))

    response = client.post(
        "/api/settings", json={"poll_interval_min": 4, "printer_name": "Canon"}
    )
    assert response.status_code == 200
    assert db.get_setting("poll_interval_min") == "4"
    assert db.get_setting("printer_name") == "Canon"
    db.close()


def test_missing_pdfs_never_recreate_a_pruned_labels_directory(env):
    """Pruning counts a label with a directory as one with files to delete."""
    label = add_label(env.db, status=LabelStatus.PRINTED, printed_at=f"{TODAY}T09:05:00")
    directory = storage.label_dir(env.config.data_dir, label.id)

    assert env.client.get(f"/labels/{label.id}").status_code == 200
    assert env.client.get(f"/api/labels/{label.id}/print.pdf").status_code == 404
    assert env.client.get(f"/api/labels/{label.id}/original.pdf").status_code == 404
    assert env.client.get(f"/api/labels/{label.id}/preview.png").status_code == 404

    assert not directory.exists()


def test_a_slow_controller_call_does_not_freeze_the_control_panel(tmp_path):
    """`lpstat` against an off printer blocks for seconds; the UI must not.

    Every handler that touches the controller, CUPS or PyMuPDF has to run off
    the event loop, or one slow call stalls every other request.
    """
    import asyncio

    import httpx

    class SlowController(StubController):
        def check_now(self) -> dict:
            time.sleep(0.4)  # stands in for an IMAP poll or an `lp` submission
            return {"detail": "done"}

    db = Database(tmp_path / "slow.db")
    db.init()
    config = Config(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "slow.db"))
    app = create_app(db, config, SlowController())

    async def race() -> tuple[float, float]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            start = time.monotonic()

            async def slow() -> float:
                await client.post("/api/agent/check-now")
                return time.monotonic() - start

            async def quick() -> float:
                await asyncio.sleep(0.05)
                response = await client.get("/api/labels")
                assert response.status_code == 200
                return time.monotonic() - start

            return await asyncio.gather(slow(), quick())

    slow_at, quick_at = asyncio.run(race())
    assert slow_at >= 0.4
    assert quick_at < 0.3, "a second request had to wait for the slow one"

    db.close()
