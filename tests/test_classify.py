import json
from pathlib import Path

import pytest

from labelagent import classify as classify_module
from labelagent.classify import Classification, build_prompt, classify_email
from labelagent.config import Config
from labelagent.emailparse import parse_email

FIXTURES = Path(__file__).parent / "fixtures"

POSHMARK_ITEM = "Kate Spade Tinsel Small Dome Crossbody Bag Rose Gold Glitter Sparkle"
POSHMARK_ORDER = "6a6e119205255dd754e8b790"
POSHMARK_TRACKING = "9434650208104113715936"
VINTED_TRACKING = "9434636208303484184646"
VINTED_SHIP_BY = "2026-08-10T02:00:00"


def load(name: str):
    return parse_email((FIXTURES / f"{name}.eml").read_bytes(), uid=name)


@pytest.fixture
def offline() -> Config:
    return Config(data_dir="unused", anthropic_api_key="")


@pytest.fixture
def online() -> Config:
    return Config(
        data_dir="unused", anthropic_api_key="test-key", classifier_model="test-model"
    )


def stub_llm(monkeypatch, payload):
    """Monkeypatch the raw API call; returns a list of the prompts it saw."""
    seen: list[str] = []

    def fake(c, config):
        seen.append(build_prompt(c))
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(classify_module, "_call_llm", fake)
    return seen


# --- heuristic path ----------------------------------------------------------


@pytest.mark.parametrize(
    "name, is_label, platform, confidence",
    [
        ("poshmark-direct", True, "poshmark", 0.9),
        ("vinted-direct", True, "vinted", 0.9),
        ("poshmark-forwarded", True, "poshmark", 0.9),
        ("vinted-forwarded", True, "vinted", 0.9),
        ("not-a-label", False, "poshmark", 0.9),
        ("unrelated", False, None, 0.9),
    ],
)
def test_heuristic_classifications(name, is_label, platform, confidence, offline):
    result = classify_email(load(name), offline)
    assert result.is_label_email is is_label
    assert result.platform == platform
    assert result.confidence == confidence
    assert result.source == "heuristic"


def test_heuristic_fills_metadata(offline):
    result = classify_email(load("poshmark-direct"), offline)
    assert result.item_title == POSHMARK_ITEM
    assert result.order_ref == POSHMARK_ORDER
    assert result.tracking_number == POSHMARK_TRACKING
    assert result.ship_by is None

    vinted = classify_email(load("vinted-direct"), offline)
    assert vinted.tracking_number == VINTED_TRACKING
    assert vinted.order_ref == "21272887347"
    assert vinted.ship_by == VINTED_SHIP_BY


def test_heuristic_partial_match_has_lower_confidence(offline):
    c = load("poshmark-direct")
    c.body_text = c.body_text.replace(POSHMARK_TRACKING, "")
    result = classify_email(c, offline)
    assert result.is_label_email is True
    assert result.tracking_number is None
    assert result.confidence == 0.5


def test_empty_api_key_never_calls_the_llm(monkeypatch, offline):
    stub_llm(monkeypatch, RuntimeError("the LLM must not be called"))
    assert classify_email(load("poshmark-direct"), offline).source == "heuristic"


# --- LLM path ----------------------------------------------------------------


def test_llm_result_wins_over_heuristic(monkeypatch, online):
    seen = stub_llm(
        monkeypatch,
        json.dumps(
            {
                "is_label_email": True,
                "platform": "poshmark",
                "item_title": "Kate Spade Crossbody",
                "order_ref": "ORDER-42",
                "tracking_number": POSHMARK_TRACKING,
                "ship_by": "2026-08-03T00:00:00",
                "confidence": 0.95,
            }
        ),
    )
    result = classify_email(load("poshmark-direct"), online)
    assert result == Classification(
        is_label_email=True,
        platform="poshmark",
        item_title="Kate Spade Crossbody",
        order_ref="ORDER-42",
        tracking_number=POSHMARK_TRACKING,
        ship_by="2026-08-03T00:00:00",
        confidence=0.95,
        source="llm",
    )
    assert "pre-paid mailing label 4x6.pdf" in seen[0]
    assert "orders@poshmark.com" in seen[0]


def test_llm_nulls_are_backfilled_from_the_heuristic(monkeypatch, online):
    stub_llm(
        monkeypatch,
        json.dumps(
            {
                "is_label_email": True,
                "platform": None,
                "item_title": None,
                "order_ref": None,
                "tracking_number": None,
                "ship_by": None,
                "confidence": 0.8,
            }
        ),
    )
    result = classify_email(load("vinted-direct"), online)
    assert result.source == "llm"
    assert result.platform == "vinted"
    assert result.order_ref == "21272887347"
    assert result.tracking_number == VINTED_TRACKING
    assert result.ship_by == VINTED_SHIP_BY
    assert result.confidence == 0.8


def test_llm_can_reject_a_candidate(monkeypatch, online):
    stub_llm(
        monkeypatch,
        json.dumps({"is_label_email": False, "confidence": 0.99, "platform": "vinted"}),
    )
    result = classify_email(load("vinted-direct"), online)
    assert result.is_label_email is False
    assert result.source == "llm"


def test_json_is_found_inside_prose_and_fences(monkeypatch, online):
    stub_llm(
        monkeypatch,
        "Sure! Here you go:\n```json\n"
        + json.dumps({"is_label_email": True, "platform": "poshmark", "confidence": 0.7})
        + "\n```\nHope that helps.",
    )
    result = classify_email(load("poshmark-direct"), online)
    assert result.source == "llm"
    assert result.confidence == 0.7
    assert result.item_title == POSHMARK_ITEM


def test_tracking_disagreement_uses_regex_and_caps_confidence(monkeypatch, online):
    stub_llm(
        monkeypatch,
        json.dumps(
            {
                "is_label_email": True,
                "platform": "poshmark",
                "tracking_number": "1111111111111111111111",
                "confidence": 1.0,
            }
        ),
    )
    result = classify_email(load("poshmark-direct"), online)
    assert result.tracking_number == POSHMARK_TRACKING
    assert result.confidence == 0.7


def test_missing_llm_tracking_is_backfilled_without_capping(monkeypatch, online):
    spaced = "9434 6502 0810 4113 7159 36"
    stub_llm(
        monkeypatch,
        json.dumps(
            {
                "is_label_email": True,
                "platform": "poshmark",
                "tracking_number": spaced,
                "confidence": 0.95,
            }
        ),
    )
    result = classify_email(load("poshmark-direct"), online)
    assert result.tracking_number == POSHMARK_TRACKING
    assert result.confidence == 0.95


def test_llm_tracking_kept_when_regex_found_none(monkeypatch, online):
    c = load("poshmark-direct")
    c.body_text = c.body_text.replace(POSHMARK_TRACKING, "")
    stub_llm(
        monkeypatch,
        json.dumps(
            {
                "is_label_email": True,
                "platform": "poshmark",
                "tracking_number": "9405511899223197428490",
                "confidence": 0.85,
            }
        ),
    )
    result = classify_email(c, online)
    assert result.tracking_number == "9405511899223197428490"
    assert result.confidence == 0.85


def test_bad_platform_and_confidence_values_are_ignored(monkeypatch, online):
    stub_llm(
        monkeypatch,
        json.dumps(
            {
                "is_label_email": True,
                "platform": "depop",
                "confidence": "very high",
            }
        ),
    )
    result = classify_email(load("vinted-direct"), online)
    assert result.platform == "vinted"
    assert result.confidence == 0.9


def test_confidence_is_clamped(monkeypatch, online):
    stub_llm(
        monkeypatch,
        json.dumps({"is_label_email": True, "platform": "vinted", "confidence": 4.2}),
    )
    assert classify_email(load("vinted-direct"), online).confidence == 1.0


@pytest.mark.parametrize(
    "payload",
    [
        RuntimeError("api down"),
        "not json at all",
        "[1, 2, 3]",
        "",
    ],
)
def test_llm_failures_fall_back_to_the_heuristic(payload, monkeypatch, online):
    stub_llm(monkeypatch, payload)
    result = classify_email(load("poshmark-direct"), online)
    assert result.source == "heuristic"
    assert result.is_label_email is True
    assert result.tracking_number == POSHMARK_TRACKING
