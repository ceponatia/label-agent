from dataclasses import dataclass
from enum import StrEnum


class Platform(StrEnum):
    POSHMARK = "poshmark"
    VINTED = "vinted"


class LabelStatus(StrEnum):
    INGESTED = "ingested"
    PROCESSING = "processing"
    READY = "ready"
    QUEUED = "queued"
    PRINTING = "printing"
    PRINTED = "printed"
    NEEDS_REVIEW = "needs_review"
    WAITING_FOR_PRINTER = "waiting_for_printer"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class Stage(StrEnum):
    INGEST = "ingest"
    CLASSIFY = "classify"
    CROP = "crop"
    VERIFY = "verify"
    PRINT = "print"
    SYSTEM = "system"


class Level(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass
class Label:
    id: int | None = None
    platform: str = Platform.POSHMARK
    item_title: str | None = None
    buyer_name: str | None = None
    order_ref: str | None = None
    tracking_number: str | None = None
    ship_by: str | None = None
    gmail_message_id: str | None = None
    email_received_at: str | None = None
    status: str = LabelStatus.INGESTED
    status_detail: str | None = None
    original_path: str | None = None
    print_path: str | None = None
    print_count: int = 0
    created_at: str | None = None
    printed_at: str | None = None


@dataclass
class Event:
    id: int | None = None
    label_id: int | None = None
    stage: str = Stage.SYSTEM
    level: str = Level.INFO
    message: str = ""
    created_at: str | None = None
