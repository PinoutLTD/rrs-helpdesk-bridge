"""Which of a report's files go onto the ticket, and how much of each.

A site's log is cumulative: every daily report carries the whole file again,
up to a few megabytes, with a few kilobytes of new lines. Measured on the test
helpdesk after two weeks, 435 MB of attached logs held about 19 MB of lines not
seen in an earlier report of the same site, and the rotated log and the traces
were mostly byte-for-byte repeats.

So the report that opens a ticket attaches everything: that is the history
behind the problem. A report appended to an open ticket attaches
- from each log, only the lines inside the report's own period
  (`ts_start`–`ts_end` of its issue); lines before it are in the earlier
  reports of the same ticket;
- any other file only when the ticket does not hold the same bytes already
  (Odoo's own checksum, SHA-1 of the content).
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from rrs_helpdesk_bridge.config import EnvSettings
from rrs_helpdesk_bridge.manifests import Report, resolve_inside

# JSON-lines logs written by the integration: one record per line with a `ts`.
LOG_FILES = {"home-assistant.log", "home-assistant.log.1"}


@dataclass
class AttachmentPlan:
    payloads: list[tuple[str, bytes]] = field(default_factory=list)
    # What was trimmed or left out, for the note on the ticket.
    notes: list[str] = field(default_factory=list)
    # What could not be attached at all, for the log.
    skipped: list[str] = field(default_factory=list)


def odoo_checksum(data: bytes) -> str:
    """How Odoo identifies an attachment's content (ir.attachment.checksum)."""

    return hashlib.sha1(data).hexdigest()


def report_period(report: Report) -> tuple[datetime, datetime] | None:
    issue = report.issue
    if issue is None or not issue.ts_start or not issue.ts_end:
        return None
    try:
        start = datetime.fromisoformat(issue.ts_start)
        end = datetime.fromisoformat(issue.ts_end)
    except ValueError:
        return None
    if start.tzinfo is None or end.tzinfo is None or start > end:
        return None
    return start, end


def lines_in_period(
    data: bytes, start: datetime, end: datetime
) -> tuple[bytes, int, int] | None:
    """The records of a JSON-lines log that fall inside the period.

    Returns (kept bytes, kept records, all records), or None when the file has
    no timestamped records at all: then it is not the log we know, and it is
    attached as it is rather than guessed at.
    """

    kept: list[bytes] = []
    records = 0
    for line in data.splitlines(keepends=True):
        try:
            ts = datetime.fromisoformat(json.loads(line)["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if ts.tzinfo is None:
            continue
        records += 1
        if start <= ts <= end:
            kept.append(line)
    if records == 0:
        return None
    return b"".join(kept), len(kept), records


def plan_attachments(
    report: Report,
    settings: EnvSettings,
    repeat: bool = False,
    on_ticket: frozenset[str] = frozenset(),
) -> AttachmentPlan:
    """The files to attach for this report.

    `repeat` is True when the report is appended to an open ticket;
    `on_ticket` holds the checksums of what that ticket already carries.
    """

    plan = AttachmentPlan()
    period = report_period(report) if repeat and settings.trim_repeated_logs else None
    for file in report.manifest.files:
        path = resolve_inside(report.directory, file.path)
        # The connector deletes artifacts by age, so a listed file may be gone
        # by the time an old report is filed. The ticket is still worth having.
        if not path.exists():
            plan.skipped.append(f"{file.name} (already deleted)")
            continue
        data = path.read_bytes()

        if period is not None and file.name in LOG_FILES:
            trimmed = lines_in_period(data, *period)
            if trimmed is not None:
                data, kept, records = trimmed
                if kept == 0:
                    plan.notes.append(f"{file.name}: no lines in this report's period")
                    continue
                plan.notes.append(
                    f"{file.name}: {kept} of {records} lines, "
                    "those of this report's period"
                )

        if len(data) > settings.max_attachment_bytes:
            plan.skipped.append(f"{file.name} (too large)")
            continue
        if repeat and odoo_checksum(data) in on_ticket:
            plan.notes.append(f"{file.name}: unchanged since an earlier report")
            continue
        plan.payloads.append((f"{report.directory.name}-{file.name}", data))
    return plan
