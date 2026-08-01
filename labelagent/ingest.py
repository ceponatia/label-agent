"""Gmail polling: fetch candidate emails, classify them, record label rows.

The IMAP mailbox is read-only apart from adding the `label-agent/processed`
Gmail label; nothing is ever marked seen, moved or deleted. A message is only
marked processed once its row and its PDF are durably on disk, so a crash
re-fetches rather than loses a label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import storage
from .classify import classify_email
from .config import Config
from .db import Database, now_iso
from .emailparse import EmailCandidate, is_candidate, parse_email
from .models import Label, LabelStatus, Level, Stage

PROCESSED_LABEL = "label-agent/processed"
LAST_POLL_KEY = "last_poll_at"

# Statuses that mean "ingest never finished". Only these get their attachment
# re-saved when the message comes round again; a pruned printed/duplicate row
# has legitimately lost its PDF and must not be resurrected into the queue.
RECOVERABLE = (LabelStatus.INGESTED, LabelStatus.FAILED)


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


def _process_message(
    db: Database,
    config: Config,
    fetcher: Fetcher,
    uid: str,
    raw: bytes,
    result: IngestResult,
) -> None:
    c = parse_email(raw, uid)

    if not is_candidate(c, config):
        result.skipped += 1
        fetcher.mark_processed(uid)
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
        fetcher.mark_processed(uid)
        return

    existing = db.find_by_gmail_message_id(c.message_id) if c.message_id else None
    if existing is not None:
        # A row whose attachment never reached disk is not "already handled".
        # Marking the message processed here used to hide it from every future
        # Gmail search, stranding a prepaid label with no PDF and no UI action
        # that could bring it back.
        if existing.status in RECOVERABLE and not _has_original(existing):
            _save_attachment(db, config, existing.id, c)
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
        fetcher.mark_processed(uid)
        return

    duplicate = _duplicate_of(db, classification.tracking_number)
    label = Label(
        platform=classification.platform,
        item_title=classification.item_title,
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

    fetcher.mark_processed(uid)


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


class ImapFetcher:
    """Gmail IMAP fetcher using Gmail's X-GM-RAW search and X-GM-LABELS."""

    def __init__(self, config: Config, mailbox=None):
        self.config = config
        self._mailbox = mailbox

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

    def search_query(self) -> str:
        return (
            f"(from:{self.config.poshmark_sender_domain} "
            f"OR from:{self.config.vinted_sender_domain}) "
            f"has:attachment -label:{PROCESSED_LABEL}"
        )

    def search_uids(self) -> list[str]:
        status, data = self.client.uid("SEARCH", "X-GM-RAW", f'"{self.search_query()}"')
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

    def fetch_candidates(self) -> list[tuple[str, bytes]]:
        return [(uid, self.fetch_raw(uid)) for uid in self.search_uids()]

    def mark_processed(self, uid: str) -> None:
        status, _ = self.client.uid(
            "STORE", str(uid), "+X-GM-LABELS", f"({PROCESSED_LABEL})"
        )
        _check(status, "STORE")


def make_fetcher(config: Config) -> Fetcher:
    return ImapFetcher(config)


__all__ = [
    "Fetcher",
    "IngestResult",
    "IngestError",
    "ImapFetcher",
    "poll_once",
    "make_fetcher",
    "PROCESSED_LABEL",
    "LAST_POLL_KEY",
]
