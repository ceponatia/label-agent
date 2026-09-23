"""Gmail polling: fetch candidate emails, classify them, record label rows.

After a message is safely handled, the agent applies the
`label-agent/processed` Gmail label and archives it by removing Gmail's Inbox
label. The processed label is created automatically when needed. A message is
only finalized once its row and PDF are durably on disk, so a crash re-fetches
rather than loses a label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pymupdf as fitz

from . import storage
from .classify import classify_email
from .config import Config
from .db import Database, now_iso
from .emailparse import EmailCandidate, detect_platform, is_candidate, parse_email
from .models import Label, LabelStatus, Level, Stage

PROCESSED_LABEL = "label-agent/processed"
LAST_POLL_KEY = "last_poll_at"

# Poshmark sometimes sends the sale email before its label service can generate
# the PDF. Those notices have no attachment, so include the phrases Poshmark
# uses for that condition in the real polling query without widening the poll
# to every offer/comment/marketing email from the platforms.
POSHMARK_DELAY_SEARCH = '{has:attachment "shipping label system" "label service"}'

# Statuses that mean "ingest never finished". Only these get their attachment
# re-saved when the message comes round again; a pruned printed/duplicate row
# has legitimately lost its PDF and must not be resurrected into the queue.
RECOVERABLE = (LabelStatus.INGESTED, LabelStatus.FAILED)

_POSHMARK_DELAY_PATTERNS = (
    re.compile(r"shipping\s+label\s+system\s+is\s+experiencing\s+(?:delays?|issues?)", re.I),
    re.compile(r"label\s+service\s+(?:is\s+)?(?:not\s+available|unavailable|down)", re.I),
    re.compile(
        r"(?:pre[- ]paid[, ]+pre[- ]addressed\s+)?shipping\s+label.*?"
        r"automatically\s+(?:mailed|emailed)\s+to\s+you.*?service\s+is\s+available",
        re.I | re.S,
    ),
)
_POSHMARK_BUYER_RE = re.compile(r"^\s*Buyer\s*\n\s*([^\r\n]+)", re.I | re.M)
_PDF_RECIPIENT_MARKERS = ("SHIP TO", "SHIP TO ADDRESS", "DELIVER TO", "RECIPIENT")


class IngestError(RuntimeError):
    pass


class Fetcher(Protocol):
    def fetch_candidates(self) -> list[tuple[str, bytes]]: ...

    def mark_processed(self, uid: str) -> None: ...


@dataclass
class IngestResult:
    checked: int = 0
    new_labels: list[int] = field(default_factory=list)
    duplicates: list[int] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# --- polling -----------------------------------------------------------------


def _save_attachment(db: Database, config: Config, label_id: int, c: EmailCandidate):
    filename, content = c.pdf_attachments[0]
    path = storage.original_pdf_path(config.data_dir, label_id)
    storage.ensure_label_dir(config.data_dir, label_id)
    path.write_bytes(content)
    db.update_label(label_id, original_path=str(path))
    return filename, path


def _has_original(label: Label) -> bool:
    return bool(label.original_path) and Path(label.original_path).is_file()


def _duplicate_of(db: Database, tracking: str | None) -> Label | None:
    if not tracking:
        return None
    for existing in db.find_by_tracking(tracking):
        if existing.status != LabelStatus.DUPLICATE:
            return existing
    return None


def _finish_message(fetcher: Fetcher, uid: str) -> None:
    """Finalize a safely handled message, with Gmail archiving when supported.

    Test/alternate fetchers only implement ``mark_processed`` and keep the old
    behavior. The Gmail fetcher exposes ``finalize_processed`` so its custom
    label creation, processed-label write, Inbox removal, and rollback stay in
    one place.
    """
    finalize = getattr(fetcher, "finalize_processed", None)
    if finalize is not None:
        finalize(uid)
    else:
        fetcher.mark_processed(uid)


def is_poshmark_label_delay(c: EmailCandidate) -> bool:
    """Return True for Poshmark's no-PDF sale notice during label outages."""
    if detect_platform(c) != "poshmark" or c.pdf_attachments:
        return False
    text = f"{c.subject}\n{c.body_text}"
    return any(pattern.search(text) for pattern in _POSHMARK_DELAY_PATTERNS)


def clean_person_name(value: str | None) -> str | None:
    name = " ".join((value or "").strip().strip(":").split())
    if not name or len(name) > 80 or not any(ch.isalpha() for ch in name):
        return None
    if name[0].isdigit() or any(ch.isdigit() for ch in name) or "@" in name:
        return None
    upper = name.upper()
    if any(
        phrase in upper
        for phrase in ("USPS", "TRACKING", "PRIORITY MAIL", "GROUND ADVANTAGE", "SHIP TO")
    ):
        return None
    return name.title() if name.isupper() else name


def _buyer_from_email(c: EmailCandidate) -> str | None:
    if detect_platform(c) != "poshmark":
        return None
    match = _POSHMARK_BUYER_RE.search(c.body_text or "")
    return clean_person_name(match.group(1)) if match else None


def _buyer_from_pdf(path: Path) -> str | None:
    """Best-effort recipient extraction from the shipping label itself.

    Vinted's email body does not always contain the buyer's real name, while the
    shipping label necessarily contains the recipient. Failure here is metadata
    loss only and must never make an otherwise printable label fail ingestion.
    """
    try:
        with fitz.open(str(path)) as doc:
            if doc.page_count == 0:
                return None
            text = doc.load_page(0).get_text("text") or ""
    except Exception:
        return None

    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        upper = line.upper()
        for marker in _PDF_RECIPIENT_MARKERS:
            if upper == marker or upper == f"{marker}:":
                if index + 1 < len(lines):
                    candidate = clean_person_name(lines[index + 1])
                    if candidate:
                        return candidate
            elif upper.startswith(f"{marker}:"):
                candidate = clean_person_name(line.split(":", 1)[1])
                if candidate:
                    return candidate
    return None


def _fill_buyer_name(
    db: Database, label_id: int, c: EmailCandidate, pdf_path: Path | None = None
) -> str | None:
    buyer = _buyer_from_email(c)
    if not buyer and pdf_path is not None:
        buyer = _buyer_from_pdf(pdf_path)
    if buyer:
        db.update_label(label_id, buyer_name=buyer)
    return buyer


def _process_message(
    db: Database,
    config: Config,
    fetcher: Fetcher,
    uid: str,
    raw: bytes,
    result: IngestResult,
) -> None:
    c = parse_email(raw, uid)

    if is_poshmark_label_delay(c):
        result.skipped += 1
        subject = c.subject or "(no subject)"
        db.add_event(
            Stage.INGEST,
            Level.ERROR,
            "Poshmark could not generate the shipping label because its label service "
            "is temporarily delayed. No action is needed; Poshmark says it will email "
            f"the prepaid label automatically when service is available again. Sale email: {subject}",
        )
        _finish_message(fetcher, uid)
        return

    if not is_candidate(c, config):
        result.skipped += 1
        _finish_message(fetcher, uid)
        return

    classification = classify_email(c, config)
    if not classification.is_label_email:
        result.skipped += 1
        db.add_event(
            Stage.INGEST,
            Level.INFO,
            f"not a label email ({classification.source}, "
            f"confidence {classification.confidence:.2f}): {c.subject}",
        )
        _finish_message(fetcher, uid)
        return

    existing = db.find_by_gmail_message_id(c.message_id) if c.message_id else None
    if existing is not None:
        # A row whose attachment never reached disk is not "already handled".
        # Marking the message processed here used to hide it from every future
        # Gmail search, stranding a prepaid label with no PDF and no UI action
        # that could bring it back.
        if existing.status in RECOVERABLE and not _has_original(existing):
            _, path = _save_attachment(db, config, existing.id, c)
            if not existing.buyer_name:
                _fill_buyer_name(db, existing.id, c, path)
            db.update_status(existing.id, LabelStatus.INGESTED, None)
            db.add_event(
                Stage.INGEST,
                Level.WARN,
                "re-saved the attachment for a label that had none on disk",
                existing.id,
            )
            result.new_labels.append(existing.id)
        else:
            result.skipped += 1
        _finish_message(fetcher, uid)
        return

    duplicate = _duplicate_of(db, classification.tracking_number)
    label = Label(
        platform=classification.platform,
        item_title=classification.item_title,
        buyer_name=_buyer_from_email(c),
        order_ref=classification.order_ref,
        tracking_number=classification.tracking_number,
        ship_by=classification.ship_by,
        gmail_message_id=c.message_id or None,
        email_received_at=c.date,
        status=LabelStatus.DUPLICATE if duplicate else LabelStatus.INGESTED,
        status_detail=(
            f"duplicate tracking number, first seen on label {duplicate.id}"
            if duplicate
            else None
        ),
    )
    label = db.insert_label(label)

    try:
        filename, path = _save_attachment(db, config, label.id, c)
    except Exception as exc:
        db.update_status(
            label.id, LabelStatus.FAILED, f"could not save attachment: {exc}"
        )
        raise

    if not label.buyer_name:
        _fill_buyer_name(db, label.id, c, path)

    if duplicate:
        result.duplicates.append(label.id)
        db.add_event(
            Stage.INGEST,
            Level.WARN,
            f"duplicate tracking {classification.tracking_number} "
            f"(label {duplicate.id}); not auto-printed",
            label.id,
        )
    else:
        result.new_labels.append(label.id)
        db.add_event(
            Stage.INGEST,
            Level.INFO,
            f"ingested {classification.platform or 'unknown'} label "
            f"'{classification.item_title or c.subject}' from {filename} "
            f"({classification.source}, confidence {classification.confidence:.2f})",
            label.id,
        )

    _finish_message(fetcher, uid)


def poll_once(db: Database, config: Config, fetcher: Fetcher) -> IngestResult:
    result = IngestResult()
    try:
        try:
            messages = fetcher.fetch_candidates()
        except Exception as exc:
            message = f"mailbox fetch failed: {exc}"
            result.errors.append(message)
            db.add_event(Stage.SYSTEM, Level.ERROR, message)
            return result

        result.checked = len(messages)
        for uid, raw in messages:
            try:
                _process_message(db, config, fetcher, uid, raw, result)
            except Exception as exc:
                message = f"failed to ingest message {uid}: {exc}"
                result.errors.append(message)
                db.add_event(Stage.INGEST, Level.ERROR, message)
        return result
    finally:
        db.set_setting(LAST_POLL_KEY, now_iso())


# --- IMAP --------------------------------------------------------------------


def _check(status: str, command: str) -> None:
    if status != "OK":
        raise IngestError(f"IMAP {command} failed: {status}")


def _imap_quoted(value: str) -> str:
    """An IMAP quoted string, with the only two characters IMAP escapes escaped.

    The whole Gmail query travels inside one of these, and the sender domains
    spliced into it come from config.toml: an unescaped quote in there would
    close the string early and hand the server a different search than the one
    we meant.
    """
    return '"{}"'.format(value.replace("\\", "\\\\").replace('"', '\\"'))


def _response_text(data) -> str:
    parts: list[str] = []
    for item in data or []:
        if isinstance(item, bytes):
            parts.append(item.decode("utf-8", errors="replace"))
        else:
            parts.append(str(item))
    return " ".join(parts)


def _label_already_exists(data) -> bool:
    text = _response_text(data).upper()
    return any(
        marker in text
        for marker in ("ALREADYEXISTS", "ALREADY EXISTS", "DUPLICATE FOLDER", "DUPLICATE MAILBOX")
    )


class ImapFetcher:
    """Gmail IMAP fetcher using Gmail's X-GM-RAW search and X-GM-LABELS."""

    def __init__(self, config: Config, mailbox=None):
        self.config = config
        self._mailbox = mailbox
        self._processed_label_ready = False

    # connection

    def mailbox(self):
        if self._mailbox is None:
            from imap_tools import MailBox

            self._mailbox = MailBox(self.config.imap_host).login(
                self.config.imap_user,
                self.config.imap_password,
                initial_folder=self.config.imap_folder,
            )
        return self._mailbox

    @property
    def client(self):
        return self.mailbox().client

    def close(self) -> None:
        if self._mailbox is not None:
            try:
                self._mailbox.logout()
            finally:
                self._mailbox = None

    # gmail queries

    def search_query(self, include_label_delays: bool = False) -> str:
        sender = (
            f"(from:{self.config.poshmark_sender_domain} "
            f"OR from:{self.config.vinted_sender_domain})"
        )
        candidate = POSHMARK_DELAY_SEARCH if include_label_delays else "has:attachment"
        return f"{sender} {candidate} -label:{PROCESSED_LABEL}"

    def search_uids(self, include_label_delays: bool = False) -> list[str]:
        status, data = self.client.uid(
            "SEARCH",
            "X-GM-RAW",
            _imap_quoted(self.search_query(include_label_delays=include_label_delays)),
        )
        _check(status, "SEARCH")
        uids: list[str] = []
        for chunk in data or []:
            if not chunk:
                continue
            if isinstance(chunk, bytes):
                chunk = chunk.decode("ascii", errors="replace")
            uids += chunk.split()
        return uids

    def fetch_raw(self, uid: str) -> bytes:
        # BODY.PEEK never sets the \Seen flag.
        status, data = self.client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        _check(status, "FETCH")
        for item in data or []:
            if isinstance(item, tuple) and len(item) > 1 and item[1]:
                return bytes(item[1])
        raise IngestError(f"no message body returned for uid {uid}")

    # Fetcher protocol

    def _fetch_candidates_once(self) -> list[tuple[str, bytes]]:
        return [
            (uid, self.fetch_raw(uid))
            for uid in self.search_uids(include_label_delays=True)
        ]

    def fetch_candidates(self) -> list[tuple[str, bytes]]:
        """Fetch candidates, reconnecting once if a cached IMAP socket died.

        Windows sleep can leave the process holding a TCP connection that Gmail
        has already closed. The first command after wake then fails (commonly
        WinError 10054). Reads are safe to repeat, so discard that connection,
        log in fresh, and retry this fetch once before surfacing an error.
        """
        try:
            return self._fetch_candidates_once()
        except Exception:
            try:
                self.close()
            except Exception:
                # A dead socket can also make LOGOUT fail. close() clears the
                # cached mailbox in its finally block, which is what matters.
                pass
            return self._fetch_candidates_once()

    def ensure_processed_label(self) -> None:
        """Create Label Agent's Gmail label once, accepting an existing label."""
        if self._processed_label_ready:
            return
        create = getattr(self.client, "create", None)
        if create is None:
            # Small test doubles and alternate IMAP clients may not expose
            # CREATE. STORE remains backward-compatible with the old fetcher.
            return
        status, data = create(PROCESSED_LABEL)
        if status != "OK" and not _label_already_exists(data):
            raise IngestError(
                f"IMAP CREATE failed: {status}: {_response_text(data) or 'no detail'}"
            )
        self._processed_label_ready = True

    def mark_processed(self, uid: str) -> None:
        status, _ = self.client.uid(
            "STORE", str(uid), "+X-GM-LABELS", f"({PROCESSED_LABEL})"
        )
        _check(status, "STORE")

    def unmark_processed(self, uid: str) -> None:
        status, _ = self.client.uid(
            "STORE", str(uid), "-X-GM-LABELS", f"({PROCESSED_LABEL})"
        )
        _check(status, "STORE")

    def archive_processed(self, uid: str) -> None:
        # In Gmail, archiving is removing the system \Inbox label; the message
        # remains in All Mail and under label-agent/processed.
        status, _ = self.client.uid(
            "STORE", str(uid), "-X-GM-LABELS", r"(\Inbox)"
        )
        _check(status, "STORE")

    def finalize_processed(self, uid: str) -> None:
        """Label and archive a safely handled message without stranding it.

        The processed label is added before Inbox is removed. If archiving
        fails, remove our processed label again so the Gmail search will retry
        the message on the next poll instead of silently leaving it in Inbox.
        """
        self.ensure_processed_label()
        self.mark_processed(uid)
        try:
            self.archive_processed(uid)
        except Exception as archive_error:
            try:
                self.unmark_processed(uid)
            except Exception as rollback_error:
                raise IngestError(
                    f"could not archive processed message {uid}: {archive_error}; "
                    f"could not roll back {PROCESSED_LABEL}: {rollback_error}"
                ) from archive_error
            raise


def make_fetcher(config: Config) -> Fetcher:
    return ImapFetcher(config)


__all__ = [
    "Fetcher",
    "IngestResult",
    "IngestError",
    "ImapFetcher",
    "poll_once",
    "make_fetcher",
    "clean_person_name",
    "PROCESSED_LABEL",
    "LAST_POLL_KEY",
    "POSHMARK_DELAY_SEARCH",
    "is_poshmark_label_delay",
]
