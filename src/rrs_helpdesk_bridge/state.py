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
"""


class Action(StrEnum):
    CREATED = "created"
    APPENDED = "appended"
    SKIPPED = "skipped"
    FAILED = "failed"


# A failed report is retried on the next run; the others are final.
FINAL_ACTIONS = (Action.CREATED, Action.APPENDED, Action.SKIPPED)


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
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO processed_reports (
                    report_id, client_id, signature, action,
                    odoo_ticket_id, detail, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    client_id = excluded.client_id,
                    signature = excluded.signature,
                    action = excluded.action,
                    odoo_ticket_id = excluded.odoo_ticket_id,
                    detail = excluded.detail,
                    processed_at = excluded.processed_at
                """,
                (
                    report_id,
                    client_id,
                    signature,
                    str(action),
                    odoo_ticket_id,
                    detail,
                    datetime.now(UTC).isoformat(),
                ),
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
