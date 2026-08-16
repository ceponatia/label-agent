from pathlib import Path

import fitz
import pytest

from labelagent import verify as verify_module
from labelagent.config import Config
from labelagent.pipeline import process_label_pdf
from labelagent.verify import VerifyResult, parse_verdict, verify_print_pdf

FIXTURES = Path(__file__).parent / "fixtures"
POSHMARK = FIXTURES / "poshmark-label.pdf"
VINTED = FIXTURES / "vinted-label.pdf"

POSHMARK_TRACKING = "9434650208104113715936"
VINTED_TRACKING = "9434636208303484184646"

WRONG_TRACKING = "9999999999999999999999"


@pytest.fixture
def poshmark_print(tmp_path):
    out = tmp_path / "poshmark-print.pdf"
    assert process_label_pdf(POSHMARK, out).ok
    return out


@pytest.fixture
def vinted_print(tmp_path):
    out = tmp_path / "vinted-print.pdf"
    assert process_label_pdf(VINTED, out).ok
    return out


def blank_page(tmp_path, name="blank.pdf", width=288, height=432):
    path = tmp_path / name
    with fitz.open() as doc:
        doc.new_page(width=width, height=height)
        doc.save(path)
    return path


def no_llm_config(**overrides):
    return Config(anthropic_api_key="", **overrides)


def test_poshmark_print_verifies(poshmark_print):
    result = verify_print_pdf(poshmark_print)

    assert result.ok
    assert result.problems == []
    assert result.warnings == []
    assert result.barcodes
    assert result.source == "deterministic"


def test_vinted_print_verifies(vinted_print):
    result = verify_print_pdf(vinted_print)

    assert result.ok
    assert result.problems == []
    assert result.barcodes
    assert result.source == "deterministic"


@pytest.mark.parametrize(
    "fixture_name,tracking",
    [("poshmark_print", POSHMARK_TRACKING), ("vinted_print", VINTED_TRACKING)],
)
def test_expected_tracking_matches(request, fixture_name, tracking):
    path = request.getfixturevalue(fixture_name)

    result = verify_print_pdf(path, expected_tracking=tracking)

    assert result.ok, result.problems


def test_expected_tracking_mismatch_is_a_problem(vinted_print):
    result = verify_print_pdf(vinted_print, expected_tracking=WRONG_TRACKING)

    assert result.ok is False
    assert any(WRONG_TRACKING in problem for problem in result.problems)
    assert result.barcodes


def test_tracking_matches_when_formatted_with_spaces(vinted_print):
    spaced = "9434 6362 0830 3484 1846 46"

    assert verify_print_pdf(vinted_print, expected_tracking=spaced).ok


def test_blank_page_is_not_ok(tmp_path):
    result = verify_print_pdf(blank_page(tmp_path))

    assert result.ok is False
    assert any("blank" in problem for problem in result.problems)
    assert "no scannable barcode" in result.problems


def test_wrong_page_size_is_a_problem(tmp_path):
    letter = blank_page(tmp_path, "letter.pdf", width=612, height=792)

    result = verify_print_pdf(letter)

    assert result.ok is False
    assert any("expected 288x432" in problem for problem in result.problems)


def test_unreadable_pdf_is_not_ok(tmp_path):
    path = tmp_path / "garbage.pdf"
    path.write_bytes(b"not a pdf")

    result = verify_print_pdf(path)

    assert result == VerifyResult(False, result.problems, [], "deterministic")
    assert result.problems


def test_missing_pyzbar_degrades_to_a_warning(poshmark_print, monkeypatch):
    def unavailable(image):
        raise ImportError("Unable to find zbar shared library")

    monkeypatch.setattr(verify_module, "decode_barcodes", unavailable)

    result = verify_print_pdf(poshmark_print)

    assert result.ok
    assert result.problems == []
    assert result.barcodes == []
    assert any("barcode check unavailable" in warning for warning in result.warnings)


def test_llm_is_not_called_without_an_api_key(poshmark_print, monkeypatch):
    def must_not_run(png, config):
        raise AssertionError("the vision check ran without an API key")

    monkeypatch.setattr(verify_module, "call_vision_api", must_not_run)

    result = verify_print_pdf(poshmark_print, config=no_llm_config())

    assert result.ok
    assert result.source == "deterministic"


def test_llm_agreeing_keeps_the_label_ok(poshmark_print, monkeypatch):
    seen = {}

    def agreeing(png, config):
        seen["png"] = png
        seen["model_key"] = config.anthropic_api_key
        return {"ok": True, "problems": []}

    monkeypatch.setattr(verify_module, "call_vision_api", agreeing)

    result = verify_print_pdf(poshmark_print, config=Config(anthropic_api_key="k"))

    assert result.ok
    assert result.problems == []
    assert result.source == "deterministic+llm"
    assert seen["png"].startswith(b"\x89PNG")
    assert seen["model_key"] == "k"


def test_llm_disagreeing_fails_the_label(poshmark_print, monkeypatch):
    def disagreeing(png, config):
        return {"ok": False, "problems": ["tracking barcode is cut off at the edge"]}

    monkeypatch.setattr(verify_module, "call_vision_api", disagreeing)

    result = verify_print_pdf(poshmark_print, config=Config(anthropic_api_key="k"))

    assert result.ok is False
    assert result.problems == ["vision: tracking barcode is cut off at the edge"]
    assert result.source == "deterministic+llm"
    assert result.barcodes


def test_llm_disagreeing_without_detail_still_fails(poshmark_print, monkeypatch):
    monkeypatch.setattr(
        verify_module, "call_vision_api", lambda png, config: {"ok": False}
    )

    result = verify_print_pdf(poshmark_print, config=Config(anthropic_api_key="k"))

    assert result.ok is False
    assert result.problems == ["vision: label failed the vision check"]


def test_llm_problems_merge_with_deterministic_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify_module,
        "call_vision_api",
        lambda png, config: {"ok": False, "problems": ["the page is empty"]},
    )

    result = verify_print_pdf(
        blank_page(tmp_path), config=Config(anthropic_api_key="k")
    )

    assert result.ok is False
    assert "no scannable barcode" in result.problems
    assert "vision: the page is empty" in result.problems
    assert result.source == "deterministic+llm"


def test_llm_failure_degrades_to_deterministic(poshmark_print, monkeypatch):
    def exploding(png, config):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(verify_module, "call_vision_api", exploding)

    result = verify_print_pdf(poshmark_print, config=Config(anthropic_api_key="k"))

    assert result.ok
    assert result.problems == []
    assert result.source == "deterministic"


def test_llm_ship_to_name_is_captured(poshmark_print, monkeypatch):
    monkeypatch.setattr(
        verify_module,
        "call_vision_api",
        lambda png, config: {"ok": True, "problems": [], "ship_to_name": "  Vanessa Chavez  "},
    )

    result = verify_print_pdf(poshmark_print, config=Config(anthropic_api_key="k"))

    assert result.ok
    assert result.ship_to_name == "Vanessa Chavez"


def test_llm_ship_to_name_garbage_or_missing_is_none(poshmark_print, monkeypatch):
    monkeypatch.setattr(
        verify_module,
        "call_vision_api",
        lambda png, config: {"ok": True, "problems": [], "ship_to_name": ["not", "a", "string"]},
    )
    assert (
        verify_print_pdf(poshmark_print, config=Config(anthropic_api_key="k")).ship_to_name
        is None
    )

    monkeypatch.setattr(
        verify_module, "call_vision_api", lambda png, config: {"ok": True, "problems": []}
    )
    assert (
        verify_print_pdf(poshmark_print, config=Config(anthropic_api_key="k")).ship_to_name
        is None
    )


def test_read_ship_to_name_reads_the_label(poshmark_print, monkeypatch):
    monkeypatch.setattr(
        verify_module,
        "call_vision_api",
        lambda png, config: {"ok": True, "problems": [], "ship_to_name": "VANESSA CHAVEZ"},
    )

    assert verify_module.read_ship_to_name(poshmark_print, Config(anthropic_api_key="k")) == (
        "VANESSA CHAVEZ"
    )


def test_read_ship_to_name_without_key_never_calls_the_model(poshmark_print, monkeypatch):
    def must_not_run(png, config):
        raise AssertionError("the vision call ran without an API key")

    monkeypatch.setattr(verify_module, "call_vision_api", must_not_run)

    assert verify_module.read_ship_to_name(poshmark_print, no_llm_config()) is None
    assert verify_module.read_ship_to_name(poshmark_print, None) is None


def test_read_ship_to_name_failure_degrades_to_none(poshmark_print, monkeypatch):
    def exploding(png, config):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(verify_module, "call_vision_api", exploding)

    assert (
        verify_module.read_ship_to_name(poshmark_print, Config(anthropic_api_key="k"))
        is None
    )


def test_parse_verdict_reads_fenced_json():
    reply = '```json\n{"ok": false, "problems": ["address is cut"]}\n```'

    assert parse_verdict(reply) == {"ok": False, "problems": ["address is cut"]}


def test_parse_verdict_rejects_prose():
    with pytest.raises(ValueError):
        parse_verdict("Looks fine to me!")
