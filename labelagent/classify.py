"""Classify candidate emails as shipping-label emails and pull out metadata.

The heuristic path is always computed; when an Anthropic API key is configured
the LLM answer is layered on top of it. The LLM can only ever add information
or flip `is_label_email` - anything it omits is backfilled from the regexes, and
a regex-extracted tracking number always wins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import Config
from .emailparse import (
    EmailCandidate,
    extract_metadata,
    is_candidate,
    normalize_tracking,
)

MAX_TOKENS = 500
PLATFORMS = ("poshmark", "vinted")

PROMPT = """You classify seller emails for a shipping-label printing agent.

Decide whether this email is a Poshmark or Vinted email that carries a prepaid
USPS shipping label as a PDF attachment (a sale/label email), as opposed to an
offer, comment, marketing or unrelated email. Forwarded copies (Fwd:, headers
quoted in the body) count as label emails.

From: {sender}
Subject: {subject}
PDF attachments: {attachments}

Body (truncated):
\"\"\"
{body}
\"\"\"

Reply with ONLY a JSON object, no prose and no code fences, with exactly these
keys:
{{"is_label_email": true|false,
  "platform": "poshmark"|"vinted"|null,
  "item_title": string|null,
  "order_ref": string|null,
  "tracking_number": string|null,
  "ship_by": string|null,
  "confidence": number between 0 and 1}}

order_ref is the Poshmark Order ID or the Vinted Transaction ID. ship_by is an
ISO-8601 timestamp, or null when the email states no deadline. Use null for
anything you cannot read directly from the email; never guess."""


@dataclass
class Classification:
    is_label_email: bool = False
    platform: str | None = None
    item_title: str | None = None
    order_ref: str | None = None
    tracking_number: str | None = None
    ship_by: str | None = None
    confidence: float = 0.0
    source: str = "heuristic"


def build_prompt(c: EmailCandidate) -> str:
    return PROMPT.format(
        sender=c.from_addr or "(unknown)",
        subject=c.subject or "(none)",
        attachments=", ".join(c.attachment_names) or "(none)",
        body=(c.body_text or "(empty)"),
    )


def _call_llm(c: EmailCandidate, config: Config) -> str:
    """Raw Anthropic call. Monkeypatched in tests - never runs offline."""
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model=config.classifier_model,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": build_prompt(c)}],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )


def heuristic_classification(c: EmailCandidate, config: Config) -> Classification:
    meta = extract_metadata(c)
    candidate = is_candidate(c, config)
    if not candidate:
        return Classification(
            is_label_email=False,
            platform=meta["platform"],
            item_title=meta["item_title"],
            order_ref=meta["order_ref"],
            tracking_number=meta["tracking_number"],
            ship_by=meta["ship_by"],
            confidence=0.9,
            source="heuristic",
        )
    complete = bool(meta["platform"]) and bool(meta["tracking_number"])
    return Classification(
        is_label_email=True,
        platform=meta["platform"],
        item_title=meta["item_title"],
        order_ref=meta["order_ref"],
        tracking_number=meta["tracking_number"],
        ship_by=meta["ship_by"],
        confidence=0.9 if complete else 0.5,
        source="heuristic",
    )


def _first_json_object(raw: str) -> dict:
    """Find the first {...} block in a possibly chatty/fenced response."""
    decoder = json.JSONDecoder()
    start = raw.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(raw[start:])
        except ValueError:
            start = raw.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = raw.find("{", start + 1)
    raise ValueError("no JSON object in response")


def _text(value) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _platform(value) -> str | None:
    text = _text(value)
    if not text:
        return None
    lowered = text.strip().lower()
    return lowered if lowered in PLATFORMS else None


def _confidence(value, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(1.0, number))


def _merge(data: dict, heuristic: Classification) -> Classification:
    is_label = data.get("is_label_email")
    merged = Classification(
        is_label_email=bool(is_label)
        if isinstance(is_label, bool)
        else heuristic.is_label_email,
        platform=_platform(data.get("platform")) or heuristic.platform,
        item_title=_text(data.get("item_title")) or heuristic.item_title,
        order_ref=_text(data.get("order_ref")) or heuristic.order_ref,
        tracking_number=_text(data.get("tracking_number")) or heuristic.tracking_number,
        ship_by=_text(data.get("ship_by")) or heuristic.ship_by,
        confidence=_confidence(data.get("confidence"), heuristic.confidence),
        source="llm",
    )

    # The tracking number is the shipment's natural key: trust the regex.
    regex_tracking = heuristic.tracking_number
    if regex_tracking:
        if normalize_tracking(merged.tracking_number) != regex_tracking:
            merged.tracking_number = regex_tracking
            merged.confidence = min(merged.confidence, 0.7)
        else:
            merged.tracking_number = regex_tracking
    return merged


def classify_email(c: EmailCandidate, config: Config) -> Classification:
    heuristic = heuristic_classification(c, config)
    if not config.anthropic_api_key:
        return heuristic
    try:
        return _merge(_first_json_object(_call_llm(c, config)), heuristic)
    except Exception:
        return heuristic


__all__ = [
    "Classification",
    "classify_email",
    "heuristic_classification",
    "build_prompt",
    "MAX_TOKENS",
]
