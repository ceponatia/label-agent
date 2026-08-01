"""Normalize a platform label PDF into a print-ready 4x6 page.

Pure functions: no database access, no network, no LLM.
"""

from dataclasses import dataclass, field
from pathlib import Path

import fitz

TARGET_WIDTH = 288.0
TARGET_HEIGHT = 432.0

# A page this close to 4x6 (either orientation) needs no crop.
SIZE_TOLERANCE = 0.05

# Breathing room around a detected label, in points.
CROP_MARGIN = 4.0

RASTER_DPI = 150
# Pixels darker than this count as content when detecting the label.
DARK_THRESHOLD = 200
# Connected components smaller than this are speckle, not label content.
MIN_COMPONENT_PIXELS = 12

# Plausibility bounds for a detected label: fraction of the page it covers and
# its long:short side ratio (a 4x6 label is 1.5).
VECTOR_AREA_RANGE = (0.08, 0.90)
VECTOR_ASPECT_RANGE = (1.2, 2.0)
CONTOUR_AREA_RANGE = (0.08, 0.90)
CONTOUR_ASPECT_RANGE = (1.15, 2.2)
UNION_AREA_RANGE = (0.05, 0.90)
UNION_ASPECT_RANGE = (1.0, 3.0)

NO_BBOX_PROBLEM = (
    "no label bounding box found; the whole original page was scaled to 4x6 instead"
)


@dataclass
class PipelineResult:
    ok: bool
    print_path: str | None
    method: str  # "passthrough" | "vector-crop" | "raster-crop" | "whole-page" | "none"
    needs_review: bool
    problems: list[str] = field(default_factory=list)


def process_label_pdf(original_pdf: str | Path, output_pdf: str | Path) -> PipelineResult:
    """Turn any label PDF into a single-page 288x432 pt portrait PDF."""
    source = Path(original_pdf)
    destination = Path(output_pdf)

    try:
        doc = fitz.open(source)
    except Exception as exc:
        return _failed(f"cannot read pdf: {exc}")

    with doc:
        if doc.needs_pass:
            return _failed("pdf is encrypted and cannot be opened")
        if doc.page_count == 0:
            return _failed("pdf has no pages")

        page = doc[0]
        page_rect = page.rect
        if page_rect.is_empty or page_rect.is_infinite:
            return _failed("pdf page has no usable size")

        if _is_four_by_six(page_rect.width, page_rect.height):
            _write_fitted(doc, 0, page_rect, destination, stretch=True)
            return PipelineResult(True, str(destination), "passthrough", False, [])

        bbox = vector_label_bbox(page)
        method = "vector-crop"
        if bbox is None:
            bbox = raster_label_bbox(page)
            method = "raster-crop"

        if bbox is None:
            _write_fitted(doc, 0, page_rect, destination, stretch=False)
            return PipelineResult(
                True, str(destination), "whole-page", True, [NO_BBOX_PROBLEM]
            )

        clip = _with_margin(bbox, page_rect)
        _write_fitted(doc, 0, clip, destination, stretch=False)
        return PipelineResult(True, str(destination), method, False, [])


def vector_label_bbox(page: fitz.Page) -> fitz.Rect | None:
    """Largest plausible rectangle among the page's drawn paths."""
    page_rect = page.rect
    page_area = page_rect.width * page_rect.height
    best: fitz.Rect | None = None

    try:
        drawings = page.get_drawings()
    except Exception:
        return None

    for drawing in drawings:
        candidates = [drawing.get("rect")]
        for item in drawing.get("items", []):
            if item and item[0] == "re":
                candidates.append(item[1])

        for candidate in candidates:
            if candidate is None:
                continue
            rect = fitz.Rect(candidate) & page_rect
            if not _plausible(rect, page_area, VECTOR_AREA_RANGE, VECTOR_ASPECT_RANGE):
                continue
            if best is None or _area(rect) > _area(best):
                best = rect

    return best


def raster_label_bbox(page: fitz.Page) -> fitz.Rect | None:
    """Bounding box of the dark content on a rendered copy of the page.

    Noise is filtered by dropping tiny connected components rather than by
    morphology, which would erode the label's one-pixel printed border.
    """
    import cv2
    import numpy as np

    pixmap = page.get_pixmap(dpi=RASTER_DPI, colorspace=fitz.csGRAY)
    gray = np.frombuffer(pixmap.samples, dtype=np.uint8)
    gray = gray.reshape(pixmap.height, pixmap.stride)[:, : pixmap.width]

    _, mask = cv2.threshold(gray, DARK_THRESHOLD, 255, cv2.THRESH_BINARY_INV)
    if not mask.any():
        return None

    page_area = page.rect.width * page.rect.height

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        largest = max(contours, key=cv2.contourArea)
        rect = _pixels_to_points(cv2.boundingRect(largest), page)
        if _plausible(rect, page_area, CONTOUR_AREA_RANGE, CONTOUR_ASPECT_RANGE):
            return rect

    union = _union_of_components(mask)
    if union is None:
        return None

    rect = _pixels_to_points(union, page)
    if _plausible(rect, page_area, UNION_AREA_RANGE, UNION_ASPECT_RANGE):
        return rect

    return None


def _union_of_components(mask) -> tuple[int, int, int, int] | None:
    """Bounding box covering every connected component bigger than a speck."""
    import cv2

    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    boxes = [
        stats[index]
        for index in range(1, count)
        if stats[index][cv2.CC_STAT_AREA] >= MIN_COMPONENT_PIXELS
    ]
    if not boxes:
        return None

    left = min(int(b[cv2.CC_STAT_LEFT]) for b in boxes)
    top = min(int(b[cv2.CC_STAT_TOP]) for b in boxes)
    right = max(int(b[cv2.CC_STAT_LEFT]) + int(b[cv2.CC_STAT_WIDTH]) for b in boxes)
    bottom = max(int(b[cv2.CC_STAT_TOP]) + int(b[cv2.CC_STAT_HEIGHT]) for b in boxes)
    return left, top, right - left, bottom - top


def _failed(problem: str) -> PipelineResult:
    return PipelineResult(False, None, "none", False, [problem])


def _is_four_by_six(width: float, height: float) -> bool:
    long_side, short_side = max(width, height), min(width, height)
    if short_side <= 0:
        return False
    if not _within(short_side, TARGET_WIDTH, SIZE_TOLERANCE):
        return False
    if not _within(long_side, TARGET_HEIGHT, SIZE_TOLERANCE):
        return False
    return _within(long_side / short_side, TARGET_HEIGHT / TARGET_WIDTH, SIZE_TOLERANCE)


def _within(value: float, target: float, tolerance: float) -> bool:
    return abs(value - target) <= target * tolerance


def _area(rect: fitz.Rect) -> float:
    return rect.width * rect.height


def _plausible(
    rect: fitz.Rect,
    page_area: float,
    area_range: tuple[float, float],
    aspect_range: tuple[float, float],
) -> bool:
    if rect.is_empty or rect.is_infinite or page_area <= 0:
        return False
    if rect.width <= 0 or rect.height <= 0:
        return False

    area_fraction = _area(rect) / page_area
    if not area_range[0] <= area_fraction <= area_range[1]:
        return False

    aspect = max(rect.width, rect.height) / min(rect.width, rect.height)
    return aspect_range[0] <= aspect <= aspect_range[1]


def _pixels_to_points(box: tuple[int, int, int, int], page: fitz.Page) -> fitz.Rect:
    x, y, width, height = box
    scale = 72.0 / RASTER_DPI
    origin = page.rect
    return fitz.Rect(
        origin.x0 + x * scale,
        origin.y0 + y * scale,
        origin.x0 + (x + width) * scale,
        origin.y0 + (y + height) * scale,
    )


def _with_margin(rect: fitz.Rect, page_rect: fitz.Rect) -> fitz.Rect:
    grown = fitz.Rect(
        rect.x0 - CROP_MARGIN,
        rect.y0 - CROP_MARGIN,
        rect.x1 + CROP_MARGIN,
        rect.y1 + CROP_MARGIN,
    )
    return grown & page_rect


def _write_fitted(
    doc: fitz.Document,
    page_no: int,
    clip: fitz.Rect,
    destination: Path,
    stretch: bool,
) -> None:
    """Place `clip` of the source page onto a fresh 288x432 pt page.

    Vector content is preserved: the source page is drawn, not rasterized.
    """
    width, height = clip.width, clip.height
    rotate = 0
    if width > height:
        width, height = height, width
        rotate = 90

    out = fitz.open()
    try:
        page = out.new_page(width=TARGET_WIDTH, height=TARGET_HEIGHT)
        page.draw_rect(page.rect, color=None, fill=(1, 1, 1), width=0)

        if stretch:
            target = page.rect
        else:
            scale = min(TARGET_WIDTH / width, TARGET_HEIGHT / height)
            drawn_width, drawn_height = width * scale, height * scale
            left = (TARGET_WIDTH - drawn_width) / 2
            top = (TARGET_HEIGHT - drawn_height) / 2
            target = fitz.Rect(left, top, left + drawn_width, top + drawn_height)

        page.show_pdf_page(target, doc, page_no, clip=clip, rotate=rotate)

        destination.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(destination), garbage=3, deflate=True)
    finally:
        out.close()
