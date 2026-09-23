#!/usr/bin/env python
"""Build the synthetic shipping-label PDF fixtures used by the pipeline tests.

Run with:  .venv/bin/python tests/fixtures/make_label_fixtures.py

The generated PDFs are committed to the repo. Tests read them directly and never
regenerate them, so this script exists for deliberate regeneration only.

Why they are generated at all: the fixtures they replace were real Poshmark and
Vinted labels carrying a real seller, two real buyers, their street addresses and
live USPS tracking numbers. Everything here is invented. The names, addresses,
order id, transaction id and tracking numbers below are placeholders chosen to
look like the real thing without matching anybody.

What the tests need from these two files, and therefore what must not drift when
the artwork is edited:

poshmark-label.pdf
    292 x 436 pt, one page. Deliberately *not* an exact 4x6: it is inside the
    pipeline's 5% SIZE_TOLERANCE, so process_label_pdf takes the "passthrough"
    branch while still exercising non-uniform stretch-fit scaling. A border box
    runs the full height of the page so no blank horizontal band ever appears in
    top_instruction_label_bbox's search window - otherwise this file would be
    mistaken for a Vinted/FedEx instruction banner and cropped.

vinted-label.pdf
    792 x 612 pt (letter landscape), one page, whose entire content is a single
    embedded raster image - no vector paths at all, exactly like the real Vinted
    PDFs. That forces process_label_pdf past vector_label_bbox and onto the
    "raster-crop" branch. The label artwork is enclosed in a solid black frame
    landing at exactly VINTED_LABEL_RECT, which raster_label_bbox finds as the
    single largest external contour. Everything outside the frame is pure white,
    so the contour is unambiguous and the connected-components fallback never
    has to run.

Both barcodes are Code 128 Set C, drawn module by module from the table below,
and both must decode with pyzbar after the label has been through the pipeline.
main() renders, decodes and measures the candidates before anything is written,
so a layout change that breaks a barcode fails here rather than in the suite.

No third-party barcode library is used on purpose: the runtime does not need one
and the fixtures should not add a dependency.
"""

from __future__ import annotations

import random
import shutil
import sys
import tempfile
from pathlib import Path

import pymupdf as fitz

FIXTURES = Path(__file__).resolve().parent
REPO_ROOT = FIXTURES.parent.parent

POSHMARK_LABEL_PDF = FIXTURES / "poshmark-label.pdf"
VINTED_LABEL_PDF = FIXTURES / "vinted-label.pdf"

# --- invented label content --------------------------------------------------

SELLER_NAME = "Jamie Carter"
SELLER_STREET = "48 Birchwood Ln"
SELLER_CITY = "Rivertown OH 44011"

POSHMARK_BUYER_NAME = "Taylor Brooks"
POSHMARK_BUYER_STREET = "902 Example Way"
POSHMARK_BUYER_CITY = "Lakeview TX 75001"
POSHMARK_BUYER_HANDLE = "@samplebuyer42"
POSHMARK_ORDER_ID = "a1b2c3d4e5f60718293a4b5c"
POSHMARK_TRACKING = "9405509699938843001234"

VINTED_BUYER_NAME = "Morgan Reyes"
VINTED_BUYER_STREET = "17 Placeholder Ct"
VINTED_BUYER_CITY = "Millbrook GA 30004"
VINTED_TRANSACTION = "e7a2f9c4-1b6d-4a3e-9f21-7c8d5e6b0a12"
VINTED_TRACKING = "9405511899223344557766"

WATERMARK = "SAMPLE - SYNTHETIC TEST LABEL"
BANNER = "USPS GROUND ADVANTAGE"

# --- page and artwork geometry -----------------------------------------------

POSHMARK_PAGE = fitz.Rect(0, 0, 292, 436)
POSHMARK_BORDER = fitz.Rect(4, 4, 288, 432)

VINTED_PAGE = fitz.Rect(0, 0, 792, 612)
# The frame the raster-crop detector is expected to lock onto.
VINTED_LABEL_RECT = fitz.Rect(36.0, 90.0, 324.0, 522.0)
# White margin baked into the raster around the frame, so the frame's outer edge
# is never the image's own edge and cannot pick up resampling fringe.
VINTED_PAD = 6.0

# Resolution the vector artwork is rasterized at before being embedded. 400 dpi
# keeps the barcode above three device pixels per narrow module once the
# pipeline has scaled the label and the suite re-renders it at 150 dpi.
ARTWORK_DPI = 400
# Narrow-module width, in artwork pixels. A whole number of pixels so every bar
# edge lands on a pixel boundary and the bars stay crisp.
MODULE_PX = 9
MODULE_PT = MODULE_PX * 72.0 / ARTWORK_DPI

BORDER_WIDTH = 1.5
RULE_WIDTH = 1.0
THICK_RULE_WIDTH = 3.0

BLACK = (0, 0, 0)
REGULAR = "helv"
BOLD = "hebo"

# --- Code 128 ----------------------------------------------------------------

# Module widths for Code 128 symbols 0-106, as bar/space/bar/space/bar/space.
# Every data symbol is 11 modules wide; the stop symbol (106) is 13.
CODE128_PATTERNS = (
    "212222", "222122", "222221", "121223", "121322", "131222", "122213",
    "122312", "132212", "221213", "221312", "231212", "112232", "122132",
    "122231", "113222", "123122", "123221", "223211", "221132", "221231",
    "213212", "223112", "312131", "311222", "321122", "321221", "312212",
    "322112", "322211", "212123", "212321", "232121", "111323", "131123",
    "131321", "112313", "132113", "132311", "211313", "231113", "231311",
    "112133", "112331", "132131", "113123", "113321", "133121", "313121",
    "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114",
    "413111", "241112", "134111", "111242", "121142", "121241", "114212",
    "124112", "124211", "411212", "421112", "421211", "212141", "214121",
    "412121", "111143", "111341", "131141", "114113", "114311", "411113",
    "411311", "113141", "114131", "311141", "411131", "211412", "211214",
    "211232", "2331112",
)
CODE128_START_C = 105
CODE128_STOP = 106


def code128_c_runs(digits: str) -> list[tuple[bool, int]]:
    """(is_bar, module_count) runs for a Code 128 Set C encoding of `digits`.

    Set C packs two digits per symbol, which is what makes a 22-digit USPS
    tracking number fit across a 4x6 label at a scannable module width.
    """
    if not digits.isdigit() or len(digits) % 2:
        raise ValueError(f"Code 128 Set C needs an even run of digits, got {digits!r}")

    values = [int(digits[index : index + 2]) for index in range(0, len(digits), 2)]
    checksum = CODE128_START_C
    for position, value in enumerate(values, start=1):
        checksum += position * value

    symbols = [CODE128_START_C, *values, checksum % 103, CODE128_STOP]
    runs: list[tuple[bool, int]] = []
    for symbol in symbols:
        for index, width in enumerate(CODE128_PATTERNS[symbol]):
            runs.append((index % 2 == 0, int(width)))
    return runs


def code128_width_pt(digits: str) -> float:
    return sum(width for _, width in code128_c_runs(digits)) * MODULE_PT


def draw_barcode(
    page: fitz.Page, left: float, top: float, height: float, digits: str
) -> None:
    """Draw `digits` as Code 128 Set C bars with their left edge at `left`."""
    x = snap(left)
    for is_bar, width in code128_c_runs(digits):
        span = width * MODULE_PT
        if is_bar:
            fill_rect(page, fitz.Rect(x, top, x + span, top + height))
        x += span


def snap(value: float) -> float:
    """Round a point coordinate onto the artwork's pixel grid."""
    return round(value * ARTWORK_DPI / 72.0) * 72.0 / ARTWORK_DPI


def spaced(digits: str, group: int = 4) -> str:
    return " ".join(digits[i : i + group] for i in range(0, len(digits), group))


# --- drawing helpers ---------------------------------------------------------


def fill_rect(page: fitz.Page, rect: fitz.Rect) -> None:
    page.draw_rect(rect, color=None, fill=BLACK, width=0)


def frame(page: fitz.Page, rect: fitz.Rect, width: float) -> None:
    """A filled black frame whose *outer* edge is exactly `rect`.

    Filled bands rather than a stroked outline, so the detected dark bounding
    box is the rect itself and does not depend on how a stroke is centred.
    """
    fill_rect(page, fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + width))
    fill_rect(page, fitz.Rect(rect.x0, rect.y1 - width, rect.x1, rect.y1))
    fill_rect(page, fitz.Rect(rect.x0, rect.y0, rect.x0 + width, rect.y1))
    fill_rect(page, fitz.Rect(rect.x1 - width, rect.y0, rect.x1, rect.y1))


def hrule(page: fitz.Page, x0: float, x1: float, y: float, width: float) -> None:
    fill_rect(page, fitz.Rect(x0, y, x1, y + width))


def vrule(page: fitz.Page, x: float, y0: float, y1: float, width: float) -> None:
    fill_rect(page, fitz.Rect(x, y0, x + width, y1))


def text(page: fitz.Page, point, value: str, size: float, bold: bool = False) -> None:
    page.insert_text(point, value, fontname=BOLD if bold else REGULAR, fontsize=size)


def text_width(value: str, size: float, bold: bool = False) -> float:
    return fitz.get_text_length(value, fontname=BOLD if bold else REGULAR, fontsize=size)


def text_center(page, center_x, y, value: str, size: float, bold: bool = False) -> None:
    text(page, (center_x - text_width(value, size, bold) / 2, y), value, size, bold)


def text_right(page, right_x, y, value: str, size: float, bold: bool = False) -> None:
    text(page, (right_x - text_width(value, size, bold), y), value, size, bold)


def draw_matrix(page, rect: fitz.Rect, columns: int, rows: int, seed: int) -> None:
    """A decorative 2D-barcode block: deterministic noise, not a real symbol.

    Stands in for the IMpb data matrix and the platform QR code, which only need
    to occupy the right space and carry the right ink density. The fixed seed
    keeps regeneration byte-stable.
    """
    generator = random.Random(seed)
    cell_width = rect.width / columns
    cell_height = rect.height / rows
    for row in range(rows):
        for column in range(columns):
            if generator.random() < 0.5:
                continue
            x = rect.x0 + column * cell_width
            y = rect.y0 + row * cell_height
            fill_rect(page, fitz.Rect(x, y, x + cell_width, y + cell_height))


# --- artwork -----------------------------------------------------------------


def poshmark_artwork() -> fitz.Document:
    """Vector artwork for the Poshmark label, at its final 292 x 436 pt size."""
    doc = fitz.open()
    page = doc.new_page(width=POSHMARK_PAGE.width, height=POSHMARK_PAGE.height)

    box = POSHMARK_BORDER
    frame(page, box, BORDER_WIDTH)
    center_x = box.x0 + box.width / 2

    # Postage block.
    postage_bottom = 104.0
    vrule(page, 100, box.y0, postage_bottom, RULE_WIDTH)
    vrule(page, 208, box.y0, postage_bottom, RULE_WIDTH)
    hrule(page, box.x0, box.x1, postage_bottom, RULE_WIDTH)
    text(page, (22, 88), "G", 82, bold=True)

    text(page, (106, 18), "US POSTAGE AND FEES PAID", 7, bold=True)
    text(page, (106, 28), "GROUND ADVANTAGE IMI", 7, bold=True)
    text(page, (106, 42), "Sep 22 2026", 8.5)
    text(page, (106, 53), "Mailed from ZIP 44011", 6.5)
    text(page, (106, 62), "5 LB GROUND ADVANTAGE RATE", 6)
    text(page, (106, 70), "ZONE 7", 6)
    text(page, (106, 86), "12658072", 7)
    text(page, (106, 97), "Commercial", 6)
    draw_matrix(page, fitz.Rect(214, 12, 278, 64), 16, 13, seed=1013)
    text_right(page, 282, 97, "063S0013140801", 6)

    # Service banner.
    text_center(page, center_x, 126, BANNER, 16, bold=True)
    hrule(page, box.x0, box.x1, 134, RULE_WIDTH)

    # Return address and order identifiers.
    text(page, (16, 154), SELLER_NAME, 13)
    text(page, (16, 170), SELLER_STREET.upper(), 13)
    text(page, (16, 186), SELLER_CITY.upper(), 13)
    page.draw_rect(fitz.Rect(196, 141, 242, 160), color=BLACK, width=1)
    text(page, (203, 155), "C052", 12, bold=True)
    text(page, (250, 155), "0001", 13, bold=True)
    text_right(page, 282, 198, f"Order # {POSHMARK_ORDER_ID}", 6)
    text_right(page, 282, 208, f"Buyer {POSHMARK_BUYER_HANDLE}", 6)

    # Ship-to block.
    text(page, (14, 232), "SHIP", 7, bold=True)
    text(page, (14, 241), "TO:", 7, bold=True)
    draw_matrix(page, fitz.Rect(12, 248, 44, 280), 10, 10, seed=2027)
    text(page, (50, 236), POSHMARK_BUYER_NAME, 14)
    text(page, (50, 254), POSHMARK_BUYER_STREET.upper(), 14)
    text(page, (50, 272), POSHMARK_BUYER_CITY.upper(), 14)
    text(page, (14, 292), WATERMARK, 6.5)

    # Tracking block.
    hrule(page, box.x0, box.x1, 300, THICK_RULE_WIDTH)
    text_center(page, center_x, 320, "USPS TRACKING #", 13, bold=True)
    bar_width = code128_width_pt(POSHMARK_TRACKING)
    draw_barcode(page, box.x0 + (box.width - bar_width) / 2, 328, 64, POSHMARK_TRACKING)
    text_center(page, center_x, 408, spaced(POSHMARK_TRACKING), 13, bold=True)

    # Footer wordmark.
    hrule(page, box.x0, box.x1, 412, RULE_WIDTH)
    text(page, (160, 428), "POSHMARK", 15, bold=True)
    draw_matrix(page, fitz.Rect(252, 414, 280, 430), 8, 8, seed=3041)

    return doc


def vinted_artwork() -> fitz.Document:
    """Vector artwork for the Vinted label: a framed 4x6 block plus white pad."""
    doc = fitz.open()
    width = VINTED_LABEL_RECT.width + 2 * VINTED_PAD
    height = VINTED_LABEL_RECT.height + 2 * VINTED_PAD
    page = doc.new_page(width=width, height=height)

    box = fitz.Rect(
        VINTED_PAD,
        VINTED_PAD,
        VINTED_PAD + VINTED_LABEL_RECT.width,
        VINTED_PAD + VINTED_LABEL_RECT.height,
    )
    frame(page, box, BORDER_WIDTH)
    center_x = box.x0 + box.width / 2

    def x(offset: float) -> float:
        return box.x0 + offset

    def y(offset: float) -> float:
        return box.y0 + offset

    # Postage block.
    vrule(page, x(72), box.y0, y(100), RULE_WIDTH)
    hrule(page, box.x0, box.x1, y(100), RULE_WIDTH)
    text(page, (x(8), y(84)), "G", 78, bold=True)

    text(page, (x(78), y(18)), "US POSTAGE PAID IMI", 7.5, bold=True)
    text(page, (x(78), y(32)), "2026-09-22", 6.5)
    text(page, (x(78), y(43)), "44011", 6.5)
    text(page, (x(78), y(54)), "C18163239", 6.5)
    text(page, (x(78), y(65)), "Commercial", 6.5)
    text(page, (x(78), y(78)), "3.0 LB ZONE 4", 6.5)
    draw_matrix(page, fitz.Rect(x(150), y(20), x(278), y(58)), 40, 12, seed=5087)
    text_right(page, x(280), y(94), "0901000028607", 6)

    # Service banner.
    text_center(page, center_x, y(120), BANNER, 13, bold=True)
    hrule(page, box.x0, box.x1, y(128), RULE_WIDTH)

    # Return address and transaction identifiers.
    text(page, (x(12), y(148)), SELLER_NAME.upper(), 8.5)
    text(page, (x(12), y(160)), SELLER_STREET.upper(), 8.5)
    text(page, (x(12), y(172)), SELLER_CITY.upper(), 8.5)
    text_right(page, x(278), y(155), "0001", 16, bold=True)
    page.draw_rect(fitz.Rect(x(228), y(166), x(272), y(184)), color=BLACK, width=1)
    text(page, (x(236), y(179)), "R003", 11)

    # Ship-to block.
    text(page, (x(12), y(226)), "SHIP TO:", 8)
    draw_matrix(page, fitz.Rect(x(12), y(236), x(46), y(270)), 11, 11, seed=6091)
    text(page, (x(54), y(236)), VINTED_BUYER_NAME.upper(), 11)
    text(page, (x(54), y(252)), VINTED_BUYER_STREET.upper(), 11)
    text(page, (x(54), y(268)), VINTED_BUYER_CITY.upper(), 11)
    text_right(page, x(278), y(296), f"ID: #{VINTED_TRANSACTION}", 7)
    text_right(page, x(278), y(308), "Vinted.com", 7)
    text(page, (x(12), y(308)), WATERMARK, 6.5)

    # Tracking block.
    hrule(page, box.x0, box.x1, y(320), THICK_RULE_WIDTH)
    text_center(page, center_x, y(340), "USPS TRACKING #", 13, bold=True)
    bar_width = code128_width_pt(VINTED_TRACKING)
    draw_barcode(page, box.x0 + (box.width - bar_width) / 2, y(348), 62, VINTED_TRACKING)
    text_center(page, center_x, y(426), spaced(VINTED_TRACKING), 12, bold=True)

    return doc


# --- rasterizing -------------------------------------------------------------


def rasterize(artwork: fitz.Document) -> bytes:
    """Render artwork to a grayscale PNG at ARTWORK_DPI."""
    pixmap = artwork[0].get_pixmap(dpi=ARTWORK_DPI, colorspace=fitz.csGRAY)
    return pixmap.tobytes("png")


def save(doc: fitz.Document, destination: Path) -> None:
    """Save deterministically: no timestamps, and no freshly random trailer /ID.

    Without no_new_id the two files differ on every run, which would make every
    regeneration look like a content change in review.
    """
    doc.save(str(destination), garbage=3, deflate=True, no_new_id=True)


def build_poshmark(destination: Path) -> None:
    with poshmark_artwork() as artwork:
        png = rasterize(artwork)

    doc = fitz.open()
    try:
        page = doc.new_page(width=POSHMARK_PAGE.width, height=POSHMARK_PAGE.height)
        page.insert_image(POSHMARK_PAGE, stream=png)
        save(doc, destination)
    finally:
        doc.close()


def build_vinted(destination: Path) -> None:
    with vinted_artwork() as artwork:
        png = rasterize(artwork)

    placement = fitz.Rect(
        VINTED_LABEL_RECT.x0 - VINTED_PAD,
        VINTED_LABEL_RECT.y0 - VINTED_PAD,
        VINTED_LABEL_RECT.x1 + VINTED_PAD,
        VINTED_LABEL_RECT.y1 + VINTED_PAD,
    )

    doc = fitz.open()
    try:
        page = doc.new_page(width=VINTED_PAGE.width, height=VINTED_PAGE.height)
        page.insert_image(placement, stream=png)
        save(doc, destination)
    finally:
        doc.close()


# --- self-check --------------------------------------------------------------

# Mirrors tests/test_pipeline.py: same render DPI, same ink formula, same floor.
CHECK_DPI = 150
CROP_INK_FLOOR = 0.12
TARGET_SIZE = (288.0, 432.0)


def render_gray(pdf_path: Path, dpi: int = CHECK_DPI):
    import numpy as np

    with fitz.open(pdf_path) as doc:
        pixmap = doc[0].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    data = np.frombuffer(pixmap.samples, dtype=np.uint8)
    return data.reshape(pixmap.height, pixmap.stride)[:, : pixmap.width]


def decode(pdf_path: Path) -> list[str]:
    from PIL import Image
    from pyzbar import pyzbar

    image = Image.fromarray(render_gray(pdf_path))
    return [code.data.decode("utf-8", "replace") for code in pyzbar.decode(image)]


def ink_coverage(pdf_path: Path) -> float:
    return float((render_gray(pdf_path) < 240).mean())


def digits_only(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def carries(payloads: list[str], tracking: str) -> bool:
    return any(tracking in digits_only(payload) for payload in payloads)


def check_poshmark(source: Path, work: Path) -> None:
    from labelagent.pipeline import PipelineResult, process_label_pdf

    with fitz.open(source) as doc:
        assert doc.page_count == 1, "poshmark fixture must have one page"
        page = doc[0]
        size = (page.rect.width, page.rect.height)
        assert size == (292.0, 436.0), f"poshmark page is {size}, expected (292, 436)"
        assert not page.get_drawings(), "poshmark fixture should be a raster image"

    assert carries(decode(source), POSHMARK_TRACKING), (
        "poshmark source barcode does not decode to the tracking number"
    )

    out = work / "poshmark-print.pdf"
    result = process_label_pdf(source, out)
    expected = PipelineResult(True, str(out), "passthrough", False, [])
    assert result == expected, f"poshmark pipeline returned {result}"

    with fitz.open(out) as doc:
        size = (doc[0].rect.width, doc[0].rect.height)
    assert size == TARGET_SIZE, f"poshmark output is {size}, expected {TARGET_SIZE}"

    assert carries(decode(out), POSHMARK_TRACKING), (
        "poshmark output barcode does not decode to the tracking number"
    )
    print(f"  poshmark: {result.method}, ink {ink_coverage(out):.4f}")


def check_vinted(source: Path, work: Path) -> None:
    from labelagent.pipeline import (
        PipelineResult,
        process_label_pdf,
        raster_label_bbox,
        vector_label_bbox,
    )

    with fitz.open(source) as doc:
        assert doc.page_count == 1, "vinted fixture must have one page"
        page = doc[0]
        size = (page.rect.width, page.rect.height)
        assert size == (792.0, 612.0), f"vinted page is {size}, expected (792, 612)"
        assert not page.get_drawings(), (
            "vinted fixture has vector paths; it must be a single raster image"
        )
        assert vector_label_bbox(page) is None, (
            "vector_label_bbox found a rect, so the pipeline would not raster-crop"
        )
        bbox = raster_label_bbox(page)

    assert bbox is not None, "raster_label_bbox found no label"
    drift = max(
        abs(found - wanted)
        for found, wanted in zip(tuple(bbox), tuple(VINTED_LABEL_RECT))
    )
    assert drift <= 1.0, f"raster bbox {tuple(bbox)} drifted {drift:.2f} pt"

    out = work / "vinted-print.pdf"
    result = process_label_pdf(source, out)
    expected = PipelineResult(True, str(out), "raster-crop", False, [])
    assert result == expected, f"vinted pipeline returned {result}"

    with fitz.open(out) as doc:
        size = (doc[0].rect.width, doc[0].rect.height)
    assert size == TARGET_SIZE, f"vinted output is {size}, expected {TARGET_SIZE}"

    assert carries(decode(out), VINTED_TRACKING), (
        "vinted output barcode does not decode to the tracking number"
    )
    ink = ink_coverage(out)
    assert ink >= CROP_INK_FLOOR, f"vinted output ink {ink:.4f} < {CROP_INK_FLOOR}"
    measured = tuple(round(value, 3) for value in bbox)
    print(f"  vinted:   {result.method}, bbox {measured}, ink {ink:.4f}")


def main() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)
        poshmark = work / "poshmark-label.pdf"
        vinted = work / "vinted-label.pdf"

        build_poshmark(poshmark)
        build_vinted(vinted)

        print("self-check:")
        check_poshmark(poshmark, work)
        check_vinted(vinted, work)

        for candidate, final in (
            (poshmark, POSHMARK_LABEL_PDF),
            (vinted, VINTED_LABEL_PDF),
        ):
            shutil.copyfile(candidate, final)
            print(f"wrote {final} ({final.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
