import re
from pathlib import Path

import pymupdf as fitz
import pytest

from labelagent.pipeline import (
    TARGET_HEIGHT,
    TARGET_WIDTH,
    PipelineResult,
    process_label_pdf,
    raster_label_bbox,
    vector_label_bbox,
)

FIXTURES = Path(__file__).parent / "fixtures"
POSHMARK = FIXTURES / "poshmark-label.pdf"
VINTED = FIXTURES / "vinted-label.pdf"

POSHMARK_TRACKING = "9434650208104113715936"
VINTED_TRACKING = "9434636208303484184646"

# The vinted label sits in the left portion of a letter-landscape page. Measured
# from the fixture: a 288 x 432.5 pt block, i.e. exactly 4x6 inches.
VINTED_LABEL_RECT = (36.0, 89.8, 324.0, 522.2)

# Rendering DPI for crop-quality assertions. At 150 dpi a correctly cropped
# label decodes and covers ~20% of the page; the same label shrunk from the
# whole letter page covers ~6% and does not decode at all.
CHECK_DPI = 150
CROP_INK_FLOOR = 0.12


def render_gray(pdf_path, dpi=CHECK_DPI):
    import numpy as np

    with fitz.open(pdf_path) as doc:
        pixmap = doc[0].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    data = np.frombuffer(pixmap.samples, dtype=np.uint8)
    return data.reshape(pixmap.height, pixmap.stride)[:, : pixmap.width]


def decode(pdf_path, dpi=CHECK_DPI):
    from PIL import Image
    from pyzbar import pyzbar

    image = Image.fromarray(render_gray(pdf_path, dpi))
    return [code.data.decode("utf-8", "replace") for code in pyzbar.decode(image)]


def ink_coverage(pdf_path, dpi=CHECK_DPI):
    return float((render_gray(pdf_path, dpi) < 240).mean())


def page_size(pdf_path):
    with fitz.open(pdf_path) as doc:
        assert doc.page_count == 1
        return doc[0].rect.width, doc[0].rect.height


def assert_four_by_six(pdf_path):
    width, height = page_size(pdf_path)
    assert (width, height) == pytest.approx((TARGET_WIDTH, TARGET_HEIGHT), abs=0.01)
    assert height > width


def carries_tracking(payloads, tracking):
    return any(tracking in re.sub(r"\D", "", payload) for payload in payloads)


def test_poshmark_fixture_passes_through(tmp_path):
    out = tmp_path / "print.pdf"
    result = process_label_pdf(POSHMARK, out)

    assert result == PipelineResult(True, str(out), "passthrough", False, [])
    assert_four_by_six(out)


def test_poshmark_output_keeps_a_scannable_tracking_barcode(tmp_path):
    out = tmp_path / "print.pdf"
    process_label_pdf(POSHMARK, out)

    assert carries_tracking(decode(out), POSHMARK_TRACKING)


def test_vinted_bounding_box_matches_the_printed_label():
    with fitz.open(VINTED) as doc:
        page = doc[0]
        assert (page.rect.width, page.rect.height) == (792.0, 612.0)
        bbox = raster_label_bbox(page)

    assert bbox is not None
    assert tuple(bbox) == pytest.approx(VINTED_LABEL_RECT, abs=1.0)


def test_vinted_fixture_is_cropped_to_the_label(tmp_path):
    out = tmp_path / "print.pdf"
    result = process_label_pdf(VINTED, out)

    assert result.ok
    assert result.method in ("vector-crop", "raster-crop")
    assert result.needs_review is False
    assert result.problems == []
    assert_four_by_six(out)

    # The crop is real, not the whole page shrunk down: the barcode is still
    # big enough to scan and the label fills the sheet.
    assert carries_tracking(decode(out), VINTED_TRACKING)
    assert ink_coverage(out) >= CROP_INK_FLOOR


def test_whole_page_fallback_would_fail_the_crop_assertions(tmp_path):
    """Guards the thresholds above: an uncropped vinted page fails both."""
    from labelagent.pipeline import _write_fitted

    out = tmp_path / "whole.pdf"
    with fitz.open(VINTED) as doc:
        _write_fitted(doc, 0, doc[0].rect, out, stretch=False)

    assert decode(out) == []
    assert ink_coverage(out) < CROP_INK_FLOOR


def test_landscape_four_by_six_is_rotated_to_portrait(tmp_path):
    source = tmp_path / "landscape.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=TARGET_HEIGHT, height=TARGET_WIDTH)
        page.insert_text((40, 100), "LANDSCAPE LABEL", fontsize=20)
        doc.save(source)

    out = tmp_path / "print.pdf"
    result = process_label_pdf(source, out)

    assert result.ok
    assert result.method == "passthrough"
    assert result.needs_review is False
    assert_four_by_six(out)


@pytest.mark.parametrize("offset", [(61.5, 187.0), (150.0, 90.0), (30.0, 300.0)])
def test_vector_crop_finds_a_bordered_label(tmp_path, offset):
    source = tmp_path / "bordered.pdf"
    left, top = offset
    label = fitz.Rect(left, top, left + TARGET_WIDTH, top + TARGET_HEIGHT)

    with fitz.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.draw_rect(label, color=(0, 0, 0), width=1.5)
        page.insert_text((label.x0 + 20, label.y0 + 40), "SHIP TO: TEST", fontsize=14)
        for index in range(24):
            bar_left = label.x0 + 20 + index * 10
            page.draw_rect(
                fitz.Rect(bar_left, label.y0 + 250, bar_left + 5, label.y0 + 330),
                color=None,
                fill=(0, 0, 0),
                width=0,
            )
        doc.save(source)

        assert tuple(vector_label_bbox(doc[0])) == pytest.approx(tuple(label), abs=1.0)

    out = tmp_path / "print.pdf"
    result = process_label_pdf(source, out)

    assert result.ok
    assert result.method == "vector-crop"
    assert result.needs_review is False
    assert_four_by_six(out)


def test_blank_letter_page_needs_review_but_still_prints(tmp_path):
    source = tmp_path / "blank.pdf"
    with fitz.open() as doc:
        doc.new_page(width=612, height=792)
        doc.save(source)

    out = tmp_path / "print.pdf"
    result = process_label_pdf(source, out)

    assert result.ok
    assert result.needs_review is True
    assert result.problems and "no label bounding box" in result.problems[0]
    assert out.exists()
    assert_four_by_six(out)


def test_encrypted_pdf_fails_without_writing_output(tmp_path):
    source = tmp_path / "locked.pdf"
    with fitz.open() as doc:
        doc.new_page(width=612, height=792)
        doc.save(
            source,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            user_pw="secret",
            owner_pw="secret",
        )

    out = tmp_path / "print.pdf"
    result = process_label_pdf(source, out)

    assert result.ok is False
    assert result.print_path is None
    assert result.problems
    assert not out.exists()


def test_zero_page_pdf_fails_without_writing_output(tmp_path):
    source = tmp_path / "empty.pdf"
    source.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
    )

    out = tmp_path / "print.pdf"
    result = process_label_pdf(source, out)

    assert result.ok is False
    assert result.print_path is None
    assert result.problems == ["pdf has no pages"]
    assert not out.exists()


def test_unreadable_pdf_fails_without_writing_output(tmp_path):
    source = tmp_path / "garbage.pdf"
    source.write_bytes(b"this is not a pdf")

    out = tmp_path / "print.pdf"
    result = process_label_pdf(source, out)

    assert result.ok is False
    assert result.print_path is None
    assert result.problems
    assert not out.exists()


def test_a_failed_save_closes_the_output_document(tmp_path, monkeypatch):
    """A write error must not leave the PyMuPDF document open."""
    source = tmp_path / "letter.pdf"
    doc = fitz.open()
    page = doc.new_page(width=792, height=612)
    page.draw_rect(fitz.Rect(40, 40, 328, 472), color=(0, 0, 0), width=2)
    doc.save(str(source))
    doc.close()

    opened = []
    real_open = fitz.open

    def spy(*args, **kwargs):
        made = real_open(*args, **kwargs)
        opened.append(made)
        return made

    monkeypatch.setattr(fitz, "open", spy)
    monkeypatch.setattr(
        fitz.Document, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError):
        process_label_pdf(source, tmp_path / "out.pdf")

    assert opened and all(made.is_closed for made in opened)
