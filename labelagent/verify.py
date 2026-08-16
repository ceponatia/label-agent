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

ANTHROPIC = "anthropic"
REPLICATE = "replicate"
# Replicate bills its own Claude proxy at Anthropic's own rate, so going through
# it saves nothing; Gemini Flash is about half that and reads documents well,
# which is why it is the default there. Both are "Warm" proxies to always-on
# endpoints - the per-GPU-second community models on Replicate cold-boot for
# anywhere up to minutes, which a label waiting to print cannot afford.
DEFAULT_VISION_MODELS = {
    ANTHROPIC: VISION_MODEL,
    REPLICATE: "google/gemini-3-flash",
}
# Replicate's own Claude wrapper scales images to 0.5 MP before the model sees
# them, which is exactly the detail a printed name lives in.
REPLICATE_MAX_IMAGE_RESOLUTION = 2
PREVIEW_MAX_HEIGHT = 1000
VISION_PROMPT = """This image is a USPS shipping label prepared for printing on 4x6 label stock.

Check that it is complete and printable: the tracking barcode is fully visible and uncut, the ship-to address is complete, and the postage block is present. Ignore small white margins.

Also read the recipient's name from the ship-to (delivery) address block: it is the first line of that address, above the street line. Do not use the sender/return address.

Reply with strict JSON only, no prose and no code fences:
{"ok": true or false, "problems": ["short description", ...], "ship_to_name": "recipient name exactly as printed" or null}

Use an empty problems list when ok is true, and null for ship_to_name when it cannot be read."""


@dataclass
class VerifyResult:
    ok: bool
    problems: list[str]
    barcodes: list[str]
    source: str  # "deterministic" | "deterministic+llm"
    warnings: list[str] = field(default_factory=list)
    # Recipient name read off the label by the vision check, when it ran. The
    # label is the only place Vinted prints the buyer's real name, and the real
    # label PDFs are raster images, so vision is the only reader we have.
    ship_to_name: str | None = None


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
    ship_to_name: str | None = None

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

        if vision_available(config):
            try:
                verdict = call_vision_api(_render_preview_png(page), config)
                source = "deterministic+llm"
                problems.extend(_vision_problems(verdict))
                ship_to_name = _vision_ship_to_name(verdict)
            except Exception:
                pass

    return VerifyResult(
        not problems, problems, barcodes, source, warnings, ship_to_name
    )


def read_ship_to_name(pdf_path: str | Path, config: Config | None) -> str | None:
    """Read just the recipient name off a label PDF via the vision model.

    Used to backfill buyer names on labels that were ingested before the name
    was captured. Returns None with no vision provider configured (raster
    labels have no text layer to fall back to) and when the model reads
    nothing. A failing vision call raises: the backfill exists to answer "why
    is the buyer missing?", and swallowing the API error here once left it
    reporting "0 filled" with no way to tell a dead key from an unreadable
    label.
    """
    if not vision_available(config):
        return None
    with fitz.open(str(pdf_path)) as doc:
        if doc.page_count == 0:
            return None
        verdict = call_vision_api(_render_preview_png(doc[0]), config)
    return _vision_ship_to_name(verdict)


def decode_barcodes(image) -> list[str]:
    """Decoded barcode payloads. Raises if pyzbar is unavailable."""
    from pyzbar import pyzbar

    return [code.data.decode("utf-8", "replace") for code in pyzbar.decode(image)]


def vision_provider(config: Config | None) -> str:
    return (getattr(config, "vision_provider", "") or ANTHROPIC).strip().lower()


def vision_model(config: Config) -> str:
    configured = (getattr(config, "vision_model", "") or "").strip()
    return configured or DEFAULT_VISION_MODELS.get(vision_provider(config), VISION_MODEL)


def vision_available(config: Config | None) -> bool:
    """Whether a vision check can run at all.

    The label PDFs are raster images with no text layer, so without a key there
    is nothing that can read them - callers skip the check rather than fail.
    """
    if config is None:
        return False
    if vision_provider(config) == REPLICATE:
        return bool(getattr(config, "replicate_api_token", ""))
    return bool(config.anthropic_api_key)


def call_vision_api(png: bytes, config: Config) -> dict:
    """Ask the vision model whether the label is complete. Tests monkeypatch this."""
    if vision_provider(config) == REPLICATE:
        return _call_replicate(png, config)
    return _call_anthropic(png, config)


def _call_replicate(png: bytes, config: Config) -> dict:
    """Run the vision check through Replicate.

    Replicate exposes no JSON-schema enforcement on any vision model, so the
    reply is parsed with the same tolerant reader used everywhere else.
    """
    import replicate

    model = vision_model(config)
    data_uri = "data:image/png;base64," + base64.standard_b64encode(png).decode("ascii")
    payload: dict = {"prompt": VISION_PROMPT}
    if model.startswith("anthropic/"):
        payload["image"] = data_uri
        payload["max_tokens"] = VISION_MAX_TOKENS
        payload["max_image_resolution"] = REPLICATE_MAX_IMAGE_RESOLUTION
    else:
        payload["images"] = [data_uri]

    client = replicate.Client(api_token=config.replicate_api_token)
    output = client.run(model, input=payload)
    return parse_verdict(_replicate_text(output))


def _replicate_text(output) -> str:
    """Replicate hands text back as a string or as a list of chunks."""
    if isinstance(output, str):
        return output
    if isinstance(output, (list, tuple)):
        return "".join(str(chunk) for chunk in output)
    return str(output)


def _call_anthropic(png: bytes, config: Config) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    message = client.messages.create(
        model=vision_model(config),
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


def _vision_ship_to_name(verdict: dict) -> str | None:
    """The recipient name from a vision verdict, or None for anything unusable."""
    name = verdict.get("ship_to_name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


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
