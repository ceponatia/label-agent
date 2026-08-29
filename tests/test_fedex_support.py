from pathlib import Path

import pymupdf as fitz

from labelagent.classify import PROMPT as CLASSIFY_PROMPT
from labelagent.pipeline import PipelineResult, process_label_pdf
from labelagent.verify import VISION_PROMPT

FIXTURES = Path(__file__).parent / "fixtures"
POSHMARK = FIXTURES / "poshmark-label.pdf"


def make_vinted_fedex_banner_label(path: Path) -> None:
    """Synthetic version of Vinted's FedEx layout, including the top warning."""
    with fitz.open() as doc:
        page = doc.new_page(width=288, height=432)
        page.insert_text((24, 18), "Ship with FedEx ONLY", fontsize=9)
        page.insert_text((24, 34), "You won't be paid if you try to ship with USPS", fontsize=8)
        page.insert_text((24, 50), "The parcel will be lost.", fontsize=8)

        label = fitz.Rect(12, 88, 276, 426)
        page.draw_rect(label, color=(0, 0, 0), width=1.5)
        page.insert_text((28, 112), "FEDEX SHIPPING LABEL", fontsize=14)
        page.insert_text((28, 140), "SHIP TO: TEST BUYER", fontsize=11)
        page.insert_text((28, 158), "123 TEST STREET", fontsize=10)

        # Dense barcode-like lower region: the production detector works from
        # rendered pixels because real Vinted PDFs are raster images.
        for index in range(24):
            x = 28 + index * 9
            width = 3 if index % 2 else 5
            page.draw_rect(
                fitz.Rect(x, 250, x + width, 350),
                color=None,
                fill=(0, 0, 0),
                width=0,
            )
        page.insert_text((28, 380), "1234 5678 9012", fontsize=12)
        doc.save(path)


def unwrapped(prompt: str) -> str:
    """Prompts are hard-wrapped, so a phrase can straddle a line break."""
    return " ".join(prompt.split())


def test_fedex_is_explicitly_valid_for_email_classification():
    assert "USPS and FedEx labels are both valid" in unwrapped(CLASSIFY_PROMPT)


def test_fedex_is_explicitly_valid_for_vision_verification():
    assert "USPS and FedEx labels are both valid" in unwrapped(VISION_PROMPT)
    assert (
        "do not treat the carrier being FedEx instead of USPS as a problem"
        in unwrapped(VISION_PROMPT)
    )


def test_vinted_fedex_instruction_banner_is_removed(tmp_path):
    source = tmp_path / "vinted-fedex-with-warning.pdf"
    output = tmp_path / "print.pdf"
    make_vinted_fedex_banner_label(source)

    result = process_label_pdf(source, output)

    assert result == PipelineResult(True, str(output), "banner-crop", False, [])
    with fitz.open(output) as doc:
        text = doc[0].get_text()
        assert "FEDEX SHIPPING LABEL" in text
        assert "Ship with FedEx ONLY" not in text
        assert "won't be paid" not in text
        assert "parcel will be lost" not in text
        assert (doc[0].rect.width, doc[0].rect.height) == (288, 432)


def test_normal_poshmark_four_by_six_still_passes_through(tmp_path):
    output = tmp_path / "print.pdf"

    result = process_label_pdf(POSHMARK, output)

    assert result == PipelineResult(True, str(output), "passthrough", False, [])
