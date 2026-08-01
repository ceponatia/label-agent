from email.message import EmailMessage
from pathlib import Path

import pytest

from labelagent.config import Config
from labelagent.emailparse import (
    BODY_LIMIT,
    EmailCandidate,
    extract_metadata,
    extract_ship_by,
    extract_tracking,
    html_to_text,
    is_candidate,
    parse_email,
    strip_forward_prefix,
)

FIXTURES = Path(__file__).parent / "fixtures"

POSHMARK_ITEM = "Kate Spade Tinsel Small Dome Crossbody Bag Rose Gold Glitter Sparkle"
POSHMARK_ORDER = "6a6e119205255dd754e8b790"
POSHMARK_TRACKING = "9434650208104113715936"
POSHMARK_ATTACHMENT = "pre-paid mailing label 4x6.pdf"

VINTED_ITEM = (
    "Wild Fable Fruit Print Cutout Tie Strap One Piece Bathing Suit US Women’s Medium"
)
VINTED_TRANSACTION = "21272887347"
VINTED_TRACKING = "9434636208303484184646"
VINTED_SHIP_BY = "2026-08-10T02:00:00"
VINTED_ATTACHMENT = "Vinted-Label-21272887347.pdf"

ALL_FIXTURES = [
    "poshmark-direct",
    "vinted-direct",
    "poshmark-forwarded",
    "vinted-forwarded",
    "not-a-label",
    "unrelated",
]


def load(name: str) -> EmailCandidate:
    return parse_email((FIXTURES / f"{name}.eml").read_bytes(), uid=name)


@pytest.fixture
def config() -> Config:
    return Config(data_dir="unused")


# --- parsing -----------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_parses(name):
    c = load(name)
    assert c.uid == name
    assert c.message_id.startswith("<")
    assert "@" in c.from_addr
    assert c.subject
    assert c.body_text
    assert c.date is not None


def test_parse_poshmark_direct():
    c = load("poshmark-direct")
    assert c.from_addr == "orders@poshmark.com"
    assert c.message_id == "<posh-direct-1@poshmark.com>"
    assert c.subject == f'"{POSHMARK_ITEM}" just sold to @vchavez75 on Poshmark!'
    assert c.date.startswith("2026-08-01T11:32:47")
    assert POSHMARK_TRACKING in c.body_text
    assert [name for name, _ in c.pdf_attachments] == [POSHMARK_ATTACHMENT]
    assert c.pdf_attachments[0][1].startswith(b"%PDF")


def test_parse_vinted_direct():
    c = load("vinted-direct")
    assert c.from_addr == "no-reply@vinted.com"
    assert c.message_id == "<vinted-direct-1@vinted.com>"
    assert c.subject == f"{VINTED_ITEM} shipping label – use by 08/10/2026 02:00 AM"
    assert c.date.startswith("2026-07-31T23:28:35")
    assert [name for name, _ in c.pdf_attachments] == [VINTED_ATTACHMENT]
    assert c.pdf_attachments[0][1].startswith(b"%PDF")


def test_html_only_body_is_stripped_to_text():
    c = load("vinted-direct")
    assert "<" not in c.body_text
    assert "Tracking code:" in c.body_text
    assert "Transaction ID:" in c.body_text
    assert VINTED_TRACKING in c.body_text


def test_plain_text_is_preferred_over_html():
    c = load("poshmark-direct")
    # the plain part has this exact spacing; the HTML alternative does not
    assert "Your Earnings (minus fee and taxes)    $33.60" in c.body_text


def test_forwarded_keeps_attachment_and_quotes_original_headers():
    posh = load("poshmark-forwarded")
    assert posh.from_addr == "elaineamy.g2010@gmail.com"
    assert posh.subject.startswith("Fwd: ")
    assert "From: Poshmark <orders@poshmark.com>" in posh.body_text
    assert [name for name, _ in posh.pdf_attachments] == [POSHMARK_ATTACHMENT]

    vinted = load("vinted-forwarded")
    assert vinted.from_addr == "elaineamy.g2010@gmail.com"
    assert "From: Team Vinted <no-reply@vinted.com>" in vinted.body_text
    assert [name for name, _ in vinted.pdf_attachments] == [VINTED_ATTACHMENT]


def test_body_is_truncated():
    msg = EmailMessage()
    msg["From"] = "Poshmark <orders@poshmark.com>"
    msg["Subject"] = "long"
    msg["Message-ID"] = "<long@poshmark.com>"
    msg.set_content("x" * (BODY_LIMIT * 2))
    assert len(parse_email(bytes(msg)).body_text) == BODY_LIMIT


def test_missing_date_header_is_none():
    msg = EmailMessage()
    msg["From"] = "orders@poshmark.com"
    msg["Subject"] = "no date"
    msg.set_content("hi")
    assert parse_email(bytes(msg)).date is None


def test_html_to_text_unescapes_and_breaks_lines():
    text = html_to_text(
        "<style>p{color:red}</style><p><b>From:</b> Team Vinted "
        "&lt;no-reply@vinted.com&gt;<br>next</p>"
    )
    assert "From: Team Vinted <no-reply@vinted.com>" in text
    assert "color:red" not in text
    assert "next" in text.splitlines()[-1]


# --- candidate detection -----------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("poshmark-direct", True),
        ("vinted-direct", True),
        ("poshmark-forwarded", True),
        ("vinted-forwarded", True),
        ("not-a-label", False),
        ("unrelated", False),
    ],
)
def test_is_candidate(name, expected, config):
    assert is_candidate(load(name), config) is expected


def test_candidate_requires_a_pdf(config):
    c = load("poshmark-direct")
    c.pdf_attachments = []
    assert is_candidate(c, config) is False


def test_candidate_requires_a_known_sender_domain(config):
    c = load("unrelated")
    assert is_candidate(c, config) is False
    c.from_addr = "orders@poshmark.com"
    assert is_candidate(c, config) is True


def test_candidate_honours_configured_domains(config):
    other = Config(poshmark_sender_domain="example.com", vinted_sender_domain="")
    assert is_candidate(load("poshmark-direct"), other) is False
    assert is_candidate(load("unrelated"), other) is True


# --- metadata ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["poshmark-direct", "poshmark-forwarded"])
def test_poshmark_metadata(name):
    assert extract_metadata(load(name)) == {
        "platform": "poshmark",
        "item_title": POSHMARK_ITEM,
        "order_ref": POSHMARK_ORDER,
        "tracking_number": POSHMARK_TRACKING,
        "ship_by": None,
    }


@pytest.mark.parametrize("name", ["vinted-direct", "vinted-forwarded"])
def test_vinted_metadata(name):
    assert extract_metadata(load(name)) == {
        "platform": "vinted",
        "item_title": VINTED_ITEM,
        "order_ref": VINTED_TRANSACTION,
        "tracking_number": VINTED_TRACKING,
        "ship_by": VINTED_SHIP_BY,
    }


def test_metadata_for_non_label_poshmark_email():
    meta = extract_metadata(load("not-a-label"))
    assert meta["platform"] == "poshmark"
    assert meta["item_title"] == POSHMARK_ITEM
    assert meta["tracking_number"] is None
    assert meta["order_ref"] is None
    assert meta["ship_by"] is None


def test_metadata_for_unrelated_email_is_all_none():
    assert extract_metadata(load("unrelated")) == {
        "platform": None,
        "item_title": None,
        "order_ref": None,
        "tracking_number": None,
        "ship_by": None,
    }


def test_forwarded_subject_prefix_is_stripped():
    assert load("poshmark-forwarded").subject.startswith("Fwd: ")
    assert strip_forward_prefix("Fwd: Re: hello") == "hello"
    assert strip_forward_prefix('Fwd: "Item" just sold') == '"Item" just sold'


# --- tracking / deadline heuristics -----------------------------------------


def test_tracking_tolerates_embedded_spaces():
    assert extract_tracking("Tracking Number 9434 6502 0810 4113 7159 36") == (
        POSHMARK_TRACKING
    )


def test_tracking_prefers_the_labelled_number():
    text = f"Reference 12345678901234567890123\nTracking code: {VINTED_TRACKING}"
    assert extract_tracking(text) == VINTED_TRACKING


def test_tracking_rejects_wrong_length_runs():
    assert extract_tracking("order 1234567890 total $42.00") is None
    assert extract_tracking("x" + "9" * 31 + "x") is None
    assert extract_tracking("9" * 20) == "9" * 20


def test_ship_by_parses_us_datetime():
    assert extract_ship_by("", "Shipping deadline: 08/10/2026 02:00 AM") == VINTED_SHIP_BY
    assert extract_ship_by("label - use by 12/01/2026 11:30 PM", "") == (
        "2026-12-01T23:30:00"
    )
    assert extract_ship_by("", "Ship your parcel before 08/09/2026") == (
        "2026-08-09T00:00:00"
    )
    assert extract_ship_by("", "no deadline here") is None
    assert extract_ship_by("", "use by 13/45/2026 02:00 AM") is None
