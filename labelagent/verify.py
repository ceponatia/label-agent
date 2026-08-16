"""Check that a normalized label is actually printable.

Deterministic checks always run. An optional vision check runs only when an
Anthropic API key is configured, and any failure there degrades to
deterministic-only rather than failing the label.
"""

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz

from .config import Config

TARGET_WIDTH = 288.0
TARGET_HEIGHT = 432.0
SIZE_TOLERANCE_PT = 1.0

BARCODE_DPI = 200
# Pixels darker than this count as ink.
INK_THRESHOLD = 240
# Real 4x6 labels land around 18-20% coverage; outside this band means a blank
# crop or an inverted mess.
MIN_INK = 0.03
MAX_INK = 0.60

VISION_MODEL = "claude-haiku-4-5"
VISION_MAX_TOKENS = 300
PREVIEW_MAX_HEIGHT = 1000
VISION_PROMPT = """This image is a USPS shipping label prepared for printing on 4x6 label stock.

Check that it is complete and printable: the tracking barcode is fully visible and uncut, the ship-to address is complete, and the postage block is present. Ignore small white margins.

Reply with strict JSON only, no prose and no code fences:
{"ok": true or false, "problems": ["short description", ...]}

Use an empty problems list when ok is true."""


@dataclass
class VerifyResult:
    ok: bool
    problems: list[str]
    barcodes: list[str]
    source: str  # "deterministic" | "deterministic+llm"
    warnings: list[str] = field(default_factory=list)


def verify_print_pdf(
    pdf_path: str | Path,
    expected_tracking: str | None = None,
    config: Config | None = None,
) -> VerifyResult:
    """Verify a print.pdf. ok is True only when no problems were found."""
    problems: list[str] = []
    warnings: list[str] = []
    barcodes: list[str] = []
    source = "deterministic"

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        return VerifyResult(False, [f"cannot read pdf: {exc}"], [], source)

    with doc:
        if doc.needs_pass:
            return VerifyResult(False, ["pdf is encrypted"], [], source)
        if doc.page_count == 0:
            return VerifyResult(False, ["pdf has no pages"], [], source)
        if doc.page_count != 1:
            problems.append(f"expected a single page, found {doc.page_count}")

        page = doc[0]
        width, height = page.rect.width, page.rect.height
        if (
            abs(width - TARGET_WIDTH) > SIZE_TOLERANCE_PT
            or abs(height - TARGET_HEIGHT) > SIZE_TOLERANCE_PT
        ):
            problems.append(
                f"page is {width:.1f}x{height:.1f} pt, expected "
                f"{TARGET_WIDTH:.0f}x{TARGET_HEIGHT:.0f} pt"
            )

        image, ink = _render_gray(page, BARCODE_DPI)

        if ink < MIN_INK:
            problems.append(f"page looks blank (ink coverage {ink:.1%})")
        elif ink > MAX_INK:
            problems.append(f"page looks too dark (ink coverage {ink:.1%})")

        try:
            barcodes = decode_barcodes(image)
        except Exception as exc:
            warnings.append(f"barcode check unavailable: {exc}")
        else:
            if not barcodes:
                problems.append("no scannable barcode")
            elif expected_tracking:
                if not _tracking_matches(barcodes, expected_tracking):
                    problems.append(
                        f"barcode does not match tracking number {expected_tracking}"
                    )

        if config is not None and config.anthropic_api_key:
            try:
                verdict = call_vision_api(_render_preview_png(page), config)
                source = "deterministic+llm"
                problems.extend(_vision_problems(verdict))
            except Exception:
                pass

    return VerifyResult(not problems, problems, barcodes, source, warnings)


def decode_barcodes(image) -> list[str]:
    """Decoded barcode payloads. Raises if pyzbar is unavailable."""
    from pyzbar import pyzbar

    return [code.data.decode("utf-8", "replace") for code in pyzbar.decode(image)]


def call_vision_api(png: bytes, config: Config) -> dict:
    """Ask the vision model whether the label is complete. Tests monkeypatch this."""
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    message = client.messages.create(
        model=VISION_MODEL,
        max_tokens=VISION_MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.standard_b64encode(png).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }
        ],
    )
    text = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )
    return parse_verdict(text)


def parse_verdict(text: str) -> dict:
    """Pull the JSON object out of a model reply, tolerating code fences."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in model reply")
    verdict = json.loads(match.group(0))
    if not isinstance(verdict, dict):
        raise ValueError("model reply is not a JSON object")
    return verdict


def _vision_problems(verdict: dict) -> list[str]:
    """Problems reported by the vision check, only when it says the label is bad."""
    if verdict.get("ok", True):
        return []
    reported = [str(p) for p in verdict.get("problems") or [] if str(p).strip()]
    if not reported:
        reported = ["label failed the vision check"]
    return [f"vision: {problem}" for problem in reported]


def _render_gray(page: fitz.Page, dpi: int):
    """Render the page to a grayscale PIL image and its ink coverage."""
    import numpy as np
    from PIL import Image

    pixmap = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    data = np.frombuffer(pixmap.samples, dtype=np.uint8)
    data = data.reshape(pixmap.height, pixmap.stride)[:, : pixmap.width]
    ink = float((data < INK_THRESHOLD).mean())
    return Image.fromarray(data), ink


def _render_preview_png(page: fitz.Page) -> bytes:
    zoom = min(PREVIEW_MAX_HEIGHT / max(page.rect.height, 1.0), 4.0)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    return pixmap.tobytes("png")


def _tracking_matches(barcodes: list[str], expected_tracking: str) -> bool:
    wanted = _digits(expected_tracking)
    if not wanted:
        return False
    for payload in barcodes:
        found = _digits(payload)
        if found and (wanted in found or found in wanted):
            return True
    return False


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)
