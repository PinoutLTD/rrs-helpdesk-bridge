"""What this bridge has already done, in its own SQLite database.

Odoo remains the source of truth for tickets: this database only records which
report has been handled, so a report is not filed twice. There is no cached
"signature → open ticket" mapping on purpose — a ticket can be closed in Odoo
at any time, and only Odoo knows that.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_reports (
    report_id      TEXT PRIMARY KEY,
    client_id      TEXT NOT NULL,
    signature      TEXT NOT NULL,
    action         TEXT NOT NULL,
    odoo_ticket_id INTEGER,
    detail         TEXT,
    processed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_processed_reports_signature
    ON processed_reports (signature);
CREATE TABLE IF NOT EXISTS unpinned (
    cid         TEXT PRIMARY KEY,
    report_id   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    unpinned_at TEXT NOT NULL
);
"""

# Columns added after the first release; ALTER TABLE keeps existing rows. The
# connector deletes a report's directory after 30 days, so what unpinning needs
# later is kept here: the report's CID and when the site published it.
ADDED_COLUMNS = {"cid": "TEXT", "published_at": "TEXT"}


class Action(StrEnum):
    CREATED = "created"
    APPENDED = "appended"
    SKIPPED = "skipped"
    FAILED = "failed"


# A failed report is retried on the next run; the others are final.
FINAL_ACTIONS = (Action.CREATED, Action.APPENDED, Action.SKIPPED)


@dataclass(frozen=True)
class PinnedReport:
    """A handled report whose archive is still pinned, as far as we know."""

    report_id: str
    cid: str
    action: Action
    odoo_ticket_id: int | None
    published_at: str


@dataclass(frozen=True)
class ProcessedReport:
    report_id: str
    client_id: str
    signature: str
    action: Action
    odoo_ticket_id: int | None
    detail: str | None
    processed_at: str


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            present = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(processed_reports)")
            }
            for column, kind in ADDED_COLUMNS.items():
                if column not in present:
                    connection.execute(
                        f"ALTER TABLE processed_reports ADD COLUMN {column} {kind}"
                    )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def is_handled(self, report_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT action FROM processed_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return row is not None and row["action"] in FINAL_ACTIONS

    def record(
        self,
        report_id: str,
        client_id: str,
        signature: str,
        action: Action,
        odoo_ticket_id: int | None = None,
        detail: str | None = None,
        cid: str | None = None,
        published_at: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO processed_reports (
                    report_id, client_id, signature, action,
                    odoo_ticket_id, detail, processed_at, cid, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    client_id = excluded.client_id,
                    signature = excluded.signature,
                    action = excluded.action,
                    odoo_ticket_id = excluded.odoo_ticket_id,
                    detail = excluded.detail,
                    processed_at = excluded.processed_at,
                    cid = COALESCE(excluded.cid, processed_reports.cid),
                    published_at = COALESCE(
                        excluded.published_at, processed_reports.published_at
                    )
                """,
                (
                    report_id,
                    client_id,
                    signature,
                    str(action),
                    odoo_ticket_id,
                    detail,
                    datetime.now(UTC).isoformat(),
                    cid,
                    published_at,
                ),
            )

    def remember_source(self, report_id: str, cid: str, published_at: str) -> None:
        """Fill in the CID of a report handled before it was recorded."""

        with self._connect() as connection:
            connection.execute(
                "UPDATE processed_reports SET cid = ?, published_at = ? "
                "WHERE report_id = ? AND cid IS NULL",
                (cid, published_at, report_id),
            )

    def pinned_reports(self) -> list[PinnedReport]:
        """Handled reports with a known CID that were not unpinned yet."""

        finals = tuple(str(action) for action in FINAL_ACTIONS)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT report_id, cid, action, odoo_ticket_id, published_at
                FROM processed_reports
                WHERE cid IS NOT NULL
                  AND action IN ({", ".join("?" * len(finals))})
                  AND cid NOT IN (SELECT cid FROM unpinned)
                ORDER BY published_at
                """,
                finals,
            ).fetchall()
        return [
            PinnedReport(
                report_id=row["report_id"],
                cid=row["cid"],
                action=Action(row["action"]),
                odoo_ticket_id=row["odoo_ticket_id"],
                published_at=row["published_at"],
            )
            for row in rows
        ]

    def mark_unpinned(self, cid: str, report_id: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO unpinned (cid, report_id, reason, unpinned_at) "
                "VALUES (?, ?, ?, ?)",
                (cid, report_id, reason, datetime.now(UTC).isoformat()),
            )

    def get(self, report_id: str) -> ProcessedReport | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM processed_reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        if row is None:
            return None
        return ProcessedReport(
            report_id=row["report_id"],
            client_id=row["client_id"],
            signature=row["signature"],
            action=Action(row["action"]),
            odoo_ticket_id=row["odoo_ticket_id"],
            detail=row["detail"],
            processed_at=row["processed_at"],
        )
