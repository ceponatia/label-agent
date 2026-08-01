import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .models import Event, Label, Level, Stage

SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
  id INTEGER PRIMARY KEY,
  platform TEXT CHECK(platform IN ('poshmark','vinted')),
  item_title TEXT,
  order_ref TEXT,
  tracking_number TEXT,
  ship_by TEXT,
  gmail_message_id TEXT UNIQUE,
  email_received_at TEXT,
  status TEXT CHECK(status IN (
    'ingested','processing','ready','queued','printing',
    'printed','needs_review','waiting_for_printer','failed','duplicate')),
  status_detail TEXT,
  original_path TEXT,
  print_path TEXT,
  print_count INTEGER DEFAULT 0,
  created_at TEXT,
  printed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_labels_tracking ON labels(tracking_number);
CREATE INDEX IF NOT EXISTS idx_labels_status ON labels(status);
CREATE INDEX IF NOT EXISTS idx_labels_created_at ON labels(created_at);
CREATE INDEX IF NOT EXISTS idx_labels_printed_at ON labels(printed_at);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  label_id INTEGER NULL REFERENCES labels(id),
  stage TEXT,
  level TEXT,
  message TEXT,
  created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_label_id ON events(label_id);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

LABEL_COLUMNS = (
    "platform",
    "item_title",
    "order_ref",
    "tracking_number",
    "ship_by",
    "gmail_message_id",
    "email_received_at",
    "status",
    "status_detail",
    "original_path",
    "print_path",
    "print_count",
    "created_at",
    "printed_at",
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _label(row: sqlite3.Row | None) -> Label | None:
    if row is None:
        return None
    return Label(**{k: row[k] for k in ("id",) + LABEL_COLUMNS})


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        label_id=row["label_id"],
        stage=row["stage"],
        level=row["level"],
        message=row["message"],
        created_at=row["created_at"],
    )


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def init(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # labels

    def insert_label(self, label: Label) -> Label:
        label.created_at = label.created_at or now_iso()
        cols = ", ".join(LABEL_COLUMNS)
        placeholders = ", ".join("?" for _ in LABEL_COLUMNS)
        values = [getattr(label, c) for c in LABEL_COLUMNS]
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO labels ({cols}) VALUES ({placeholders})", values
            )
            self.conn.commit()
            label.id = cur.lastrowid
        return label

    def get_label(self, label_id: int) -> Label | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM labels WHERE id = ?", (label_id,)
            ).fetchone()
        return _label(row)

    def list_labels(
        self,
        date: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[Label]:
        sql = "SELECT * FROM labels"
        where: list[str] = []
        args: list = []
        if date:
            where.append("(date(created_at) = ? OR date(printed_at) = ?)")
            args += [date, date]
        if status:
            where.append("status = ?")
            args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [_label(r) for r in rows]

    def update_label(self, label_id: int, **fields) -> Label | None:
        updates = {k: v for k, v in fields.items() if k in LABEL_COLUMNS}
        if not updates:
            return self.get_label(label_id)
        assignments = ", ".join(f"{k} = ?" for k in updates)
        args = list(updates.values()) + [label_id]
        with self._lock:
            self.conn.execute(f"UPDATE labels SET {assignments} WHERE id = ?", args)
            self.conn.commit()
        return self.get_label(label_id)

    def update_status(
        self, label_id: int, status: str, detail: str | None = None
    ) -> Label | None:
        return self.update_label(label_id, status=str(status), status_detail=detail)

    def mark_printed(self, label_id: int) -> Label | None:
        with self._lock:
            self.conn.execute(
                "UPDATE labels SET status = 'printed', printed_at = ?, "
                "print_count = print_count + 1 WHERE id = ?",
                (now_iso(), label_id),
            )
            self.conn.commit()
        return self.get_label(label_id)

    def find_by_tracking(self, tracking: str) -> list[Label]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM labels WHERE tracking_number = ? ORDER BY id",
                (tracking,),
            ).fetchall()
        return [_label(r) for r in rows]

    def find_by_gmail_message_id(self, msgid: str) -> Label | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM labels WHERE gmail_message_id = ?", (msgid,)
            ).fetchone()
        return _label(row)

    # events

    def add_event(
        self,
        stage: str,
        level: str,
        message: str,
        label_id: int | None = None,
    ) -> Event:
        event = Event(
            label_id=label_id,
            stage=str(stage),
            level=str(level),
            message=message,
            created_at=now_iso(),
        )
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO events (label_id, stage, level, message, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event.label_id,
                    event.stage,
                    event.level,
                    event.message,
                    event.created_at,
                ),
            )
            self.conn.commit()
            event.id = cur.lastrowid
        return event

    def list_events(
        self,
        level: str | None = None,
        stage: str | None = None,
        limit: int = 200,
        label_id: int | None = None,
    ) -> list[Event]:
        sql = "SELECT * FROM events"
        where: list[str] = []
        args: list = []
        if level:
            where.append("level = ?")
            args.append(level)
        if stage:
            where.append("stage = ?")
            args.append(stage)
        if label_id is not None:
            where.append("label_id = ?")
            args.append(label_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [_event(r) for r in rows]

    # settings

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self.conn.commit()

    # metrics

    def daily_metrics(self, date_from: str, date_to: str) -> list[dict]:
        sql = """
        WITH days(d) AS (
          SELECT DISTINCT date(created_at) FROM labels
            WHERE date(created_at) BETWEEN :a AND :b
          UNION
          SELECT DISTINCT date(printed_at) FROM labels
            WHERE printed_at IS NOT NULL AND date(printed_at) BETWEEN :a AND :b
        )
        SELECT
          d AS date,
          (SELECT COUNT(*) FROM labels WHERE date(printed_at) = d) AS printed,
          (SELECT COUNT(*) FROM labels
             WHERE date(created_at) = d AND status = 'failed') AS failed,
          (SELECT COUNT(*) FROM labels
             WHERE date(created_at) = d AND status = 'needs_review') AS needs_review,
          (SELECT COUNT(*) FROM labels WHERE date(created_at) = d) AS created
        FROM days
        ORDER BY d
        """
        with self._lock:
            rows = self.conn.execute(sql, {"a": date_from, "b": date_to}).fetchall()
        return [dict(r) for r in rows]


__all__ = ["Database", "now_iso", "SCHEMA", "Event", "Label", "Level", "Stage"]
