"""Parse raw RFC822 messages into label-email candidates.

Everything here is deterministic: header/body extraction plus regex heuristics
for the metadata both platforms print in plain text. The LLM classifier in
`classify.py` builds on top of this and falls back to it.
"""

from __future__ import annotations

import email
import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from .config import Config

BODY_LIMIT = 8000


@dataclass
class EmailCandidate:
    uid: str = ""
    message_id: str = ""
    from_addr: str = ""
    subject: str = ""
    body_text: str = ""
    date: str | None = None
    pdf_attachments: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def attachment_names(self) -> list[str]:
        return [name for name, _ in self.pdf_attachments]


# --- HTML -> text ------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
_BLOCK_RE = re.compile(
    r"</?(?:br|p|div|tr|td|th|table|li|ul|ol|h[1-6]|blockquote|hr)\b[^>]*>", re.I
)
_TAG_RE = re.compile(r"<[^>]*>")
_INLINE_SPACE_RE = re.compile(r"[ \t]{2,}")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    text = _SCRIPT_RE.sub(" ", html)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _INLINE_SPACE_RE.sub(" ", text)
    text = _TRAILING_SPACE_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


# --- message parsing ---------------------------------------------------------


def _header(msg: Message, name: str) -> str:
    """Header value with folding artifacts collapsed to single spaces."""
    value = msg.get(name)
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except (LookupError, ValueError, KeyError):
        pass
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _body_text(msg: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_part_text(part))
        elif ctype == "text/html":
            html.append(_part_text(part))
    joined = "\n".join(t for t in plain if t.strip())
    if not joined.strip():
        joined = html_to_text("\n".join(html))
    return joined.strip()[:BODY_LIMIT]


def _pdf_attachments(msg: Message) -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename() or ""
        ctype = (part.get_content_type() or "").lower()
        if ctype != "application/pdf" and not filename.lower().endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append((filename or "attachment.pdf", bytes(payload)))
    return found


def _date_iso(raw: str) -> str | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        return None


def parse_email(raw: bytes, uid: str = "") -> EmailCandidate:
    msg = email.message_from_bytes(raw, policy=policy.default)
    from_header = _header(msg, "From")
    addr = parseaddr(from_header)[1] or from_header
    return EmailCandidate(
        uid=uid,
        message_id=_header(msg, "Message-ID"),
        from_addr=addr,
        subject=_header(msg, "Subject"),
        body_text=_body_text(msg),
        date=_date_iso(_header(msg, "Date")),
        pdf_attachments=_pdf_attachments(msg),
    )


# --- candidate detection -----------------------------------------------------

_FORWARD_FROM_RE = re.compile(r"^[>\s]*\*?\s*From:\s*(.+)$", re.M | re.I)


def sender_texts(c: EmailCandidate) -> list[str]:
    """The real sender plus any quoted `From:` lines from a forwarded copy."""
    texts = [c.from_addr]
    texts += [m.strip() for m in _FORWARD_FROM_RE.findall(c.body_text)]
    return [t for t in texts if t]


def is_candidate(c: EmailCandidate, config: Config) -> bool:
    if not c.pdf_attachments:
        return False
    domains = [
        d.strip().lower()
        for d in (config.poshmark_sender_domain, config.vinted_sender_domain)
        if d and d.strip()
    ]
    haystack = " ".join(sender_texts(c)).lower()
    return any(domain in haystack for domain in domains)


# --- metadata extraction -----------------------------------------------------

_FWD_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fwd|fw)\s*:\s*)+", re.I)
_QUOTED_RE = re.compile(r"[\"“]\s*([^\"”]+?)\s*[\"”]")
_VINTED_TAIL_RE = re.compile(r"^(.*?)\s+shipping\s+label\b", re.I | re.S)
_VINTED_ITEM_RE = re.compile(r"^\s*Item\s*:\s*(.+)$", re.M)

_TRACKING_CORE = r"(?<!\d)\d(?:[ \t]?\d){19,29}(?!\d)"
_TRACKING_LABELLED_RE = re.compile(
    r"tracking\s*(?:number|code|no\.?|#)?\s*[:#]?\s*(" + _TRACKING_CORE + r")",
    re.I,
)
_TRACKING_ANY_RE = re.compile(_TRACKING_CORE)

_ORDER_REF_RE = re.compile(
    r"order\s*(?:id|number|no\.?|#)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{5,})", re.I
)
_TRANSACTION_REF_RE = re.compile(
    r"transaction\s*(?:id|number|no\.?|#)?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,})",
    re.I,
)

_DEADLINE_CONTEXT_RES = (
    re.compile(r"shipping\s+deadline\s*[:#]?\s*(.{0,48})", re.I | re.S),
    re.compile(r"\buse\s+by\b\s*(.{0,48})", re.I | re.S),
    re.compile(r"\bship\s+(?:your\s+parcel\s+)?before\b\s*(.{0,48})", re.I | re.S),
)
_DATETIME_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})(?:[,\s]+(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?)?"
)


def strip_forward_prefix(subject: str) -> str:
    return _FWD_PREFIX_RE.sub("", subject or "").strip()


def normalize_tracking(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits if 20 <= len(digits) <= 30 else None


def detect_platform(c: EmailCandidate) -> str | None:
    haystack = " ".join(sender_texts(c)).lower()
    if "poshmark" in haystack:
        return "poshmark"
    if "vinted" in haystack:
        return "vinted"
    subject = (c.subject or "").lower()
    if "poshmark" in subject:
        return "poshmark"
    if "vinted" in subject:
        return "vinted"
    return None


def extract_tracking(text: str) -> str | None:
    match = _TRACKING_LABELLED_RE.search(text)
    if match:
        found = normalize_tracking(match.group(1))
        if found:
            return found
    for match in _TRACKING_ANY_RE.finditer(text):
        found = normalize_tracking(match.group(0))
        if found:
            return found
    return None


def _order_ref(platform: str | None, text: str) -> str | None:
    patterns = (
        [_TRANSACTION_REF_RE, _ORDER_REF_RE]
        if platform == "vinted"
        else [_ORDER_REF_RE, _TRANSACTION_REF_RE]
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def _parse_deadline(snippet: str) -> str | None:
    match = _DATETIME_RE.search(snippet)
    if not match:
        return None
    month, day, year, hour, minute, meridiem = match.groups()
    try:
        if hour is None:
            return datetime(int(year), int(month), int(day)).isoformat()
        stamp = f"{month}/{day}/{year} {hour}:{minute} {meridiem.upper()}M"
        return datetime.strptime(stamp, "%m/%d/%Y %I:%M %p").isoformat()
    except ValueError:
        return None


def extract_ship_by(subject: str, body: str) -> str | None:
    for text in (body, subject):
        for pattern in _DEADLINE_CONTEXT_RES:
            for match in pattern.finditer(text or ""):
                parsed = _parse_deadline(match.group(1))
                if parsed:
                    return parsed
    return None


def _item_title(platform: str | None, subject: str, body: str) -> str | None:
    subject = strip_forward_prefix(subject)
    if platform == "vinted":
        match = _VINTED_TAIL_RE.match(subject)
        if match and match.group(1).strip():
            return match.group(1).strip()
        match = _VINTED_ITEM_RE.search(body or "")
        if match:
            return match.group(1).strip()
    quoted = _QUOTED_RE.search(subject)
    if quoted:
        return quoted.group(1).strip()
    if platform == "poshmark":
        match = re.match(r"^(.*?)\s+just sold\b", subject, re.I)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def extract_metadata(c: EmailCandidate) -> dict:
    platform = detect_platform(c)
    body = c.body_text or ""
    subject = c.subject or ""
    return {
        "platform": platform,
        "item_title": _item_title(platform, subject, body),
        "order_ref": _order_ref(platform, body) or _order_ref(platform, subject),
        "tracking_number": extract_tracking(body) or extract_tracking(subject),
        "ship_by": extract_ship_by(subject, body),
    }


__all__ = [
    "EmailCandidate",
    "parse_email",
    "is_candidate",
    "extract_metadata",
    "detect_platform",
    "extract_tracking",
    "extract_ship_by",
    "html_to_text",
    "normalize_tracking",
    "sender_texts",
    "strip_forward_prefix",
    "BODY_LIMIT",
]
