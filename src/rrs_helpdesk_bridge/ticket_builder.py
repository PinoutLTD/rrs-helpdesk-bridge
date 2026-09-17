"""Pure functions turning a report into Odoo helpdesk ticket values.

Nothing here talks to Odoo or to the filesystem, so the mapping can be read,
tested, and reviewed on its own. Two rules come from the live helpdesk: the
ticket is always created in the "New" stage (the closing stages carry a
customer e-mail template), and no address fields are ever written, so a ticket
cannot notify a client by mistake.
"""

import hashlib
import html
import json
from datetime import UTC, datetime

from rrs_helpdesk_bridge.manifests import Report, ReportIssue

ISSUE_LABELS = {
    "accumulated_system_log_problems": "System log",
    "entities_health_problems": "Entities health",
}

PRIORITY_MEDIUM = "1"
PRIORITY_HIGH = "2"
PRIORITY_VERY_HIGH = "3"

MAX_TITLE_LENGTH = 120
MAX_MESSAGE_LENGTH = 300
MAX_TOP_EVENTS = 25
MAX_DEVICES = 30
MAX_RAW_DETAILS = 4000
ODOO_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def issue_label(issue_type: str) -> str:
    return ISSUE_LABELS.get(issue_type, issue_type)


def issue_fingerprint(issue: ReportIssue) -> str:
    """What distinguishes two problems of the same type on the same site.

    Empty in v1: reports of one type are folded into one open ticket per site,
    because the concrete errors rotate while the underlying fault does not. A
    closed ticket is never reused, so a later report opens a new one.
    """

    return ""


def ticket_signature(client_id: str, issue: ReportIssue) -> str:
    source = f"{client_id}|{issue.type}|{issue_fingerprint(issue)}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def ticket_title(client_id: str, issue: ReportIssue) -> str:
    headline = (issue.summary or issue_label(issue.type)).strip()
    title = f"[{client_id}] {headline}"
    if len(title) > MAX_TITLE_LENGTH:
        title = title[: MAX_TITLE_LENGTH - 1].rstrip() + "…"
    return title


def ticket_priority(issue: ReportIssue) -> str:
    by_level = issue.details.get("by_level_events") or {}
    levels = {str(level).upper() for level in by_level}
    if "CRITICAL" in levels:
        return PRIORITY_VERY_HIGH
    if "ERROR" in levels or issue.type == "entities_health_problems":
        return PRIORITY_HIGH
    return PRIORITY_MEDIUM


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def odoo_datetime(value: datetime) -> str:
    """Odoo stores datetimes as naive UTC strings."""

    return value.astimezone(UTC).strftime(ODOO_DATETIME_FORMAT)


def last_occurred(report: Report) -> str:
    issue = report.issue
    occurred = parse_timestamp(issue.ts_end) if issue else None
    return odoo_datetime(occurred or report.manifest.datalog_timestamp)


def escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def truncate(value: object, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def table(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f"<tr><td style='padding-right:12px'><b>{escape(name)}</b></td>"
        f"<td>{value}</td></tr>"
        for name, value in rows
    )
    return f"<table>{cells}</table>"


def report_facts(report: Report) -> str:
    manifest = report.manifest
    issue = report.issue
    rows = [
        ("Site", escape(manifest.client_id)),
        ("Report", escape(manifest.report_id)),
        (
            "Datalog",
            f"#{manifest.datalog_index} at {escape(manifest.datalog_timestamp)}",
        ),
        ("IPFS CID", escape(manifest.cid)),
        ("Sender", escape(manifest.sender_address)),
    ]
    if issue and (issue.ts_start or issue.ts_end):
        rows.insert(2, ("Period", f"{escape(issue.ts_start)} — {escape(issue.ts_end)}"))
    return table(rows)


def log_events_html(details: dict) -> str:
    events = details.get("top_events") or []
    if not isinstance(events, list) or not events:
        return ""
    header = (
        "<tr><th align='left'>Level</th><th align='left'>Logger</th>"
        "<th align='left'>Count</th><th align='left'>Last seen</th>"
        "<th align='left'>Message</th></tr>"
    )
    rows = []
    for event in events[:MAX_TOP_EVENTS]:
        if not isinstance(event, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{escape(event.get('level', ''))}</td>"
            f"<td>{escape(event.get('name', ''))}</td>"
            f"<td>{escape(event.get('count', ''))}</td>"
            f"<td>{escape(event.get('last_seen', ''))}</td>"
            f"<td>{escape(truncate(event.get('message', ''), MAX_MESSAGE_LENGTH))}</td>"
            "</tr>"
        )
    hidden = len(events) - MAX_TOP_EVENTS
    more = (
        f"<p>… and {hidden} more event(s), see the attached log.</p>"
        if hidden > 0
        else ""
    )
    return (
        "<p><b>Top log events</b></p>"
        "<table border='1' cellpadding='4' cellspacing='0'>"
        f"{header}{''.join(rows)}</table>{more}"
    )


def entities_html(details: dict) -> str:
    unavailable = details.get("unavailable_entities") or {}
    if not isinstance(unavailable, dict):
        return ""
    devices = unavailable.get("devices") or {}
    pure = unavailable.get("pure_entities") or []
    parts = ["<p><b>Unavailable</b></p><ul>"]
    for device in list(devices.values())[:MAX_DEVICES]:
        if not isinstance(device, dict):
            continue
        entities = ", ".join(escape(entity) for entity in device.get("entities") or [])
        name = escape(device.get("device_name", "Unknown device"))
        parts.append(f"<li>{name}: {entities}</li>")
    for entity in pure[:MAX_DEVICES]:
        parts.append(f"<li>{escape(entity)}</li>")
    parts.append("</ul>")
    hidden = max(len(devices) - MAX_DEVICES, 0) + max(len(pure) - MAX_DEVICES, 0)
    if hidden:
        parts.append(
            f"<p>… and {hidden} more unavailable device(s) or entity(ies), "
            "see the attached report.</p>"
        )
    return "".join(parts)


def details_html(issue: ReportIssue) -> str:
    if issue.type == "accumulated_system_log_problems":
        return log_events_html(issue.details)
    if issue.type == "entities_health_problems":
        return entities_html(issue.details)
    raw = truncate(
        json.dumps(issue.details, ensure_ascii=False, indent=2), MAX_RAW_DETAILS
    )
    return f"<p><b>Details</b></p><pre>{escape(raw)}</pre>"


def files_html(report: Report) -> str:
    files = report.manifest.files
    if not files:
        return ""
    items = "".join(
        f"<li>{escape(file.name)} ({file.size_bytes} B)</li>" for file in files
    )
    return f"<p><b>Files in the report</b></p><ul>{items}</ul>"


def client_reference_html(partner_id: int) -> str:
    """Where the client lives in Odoo, without making them a recipient."""

    link = f"/web#id={partner_id}&model=res.partner&view_type=form"
    return (
        f'<p>Client record in Odoo: <a href="{link}">contact #{partner_id}</a>. '
        "Not linked to this ticket on purpose, so that closing it sends the "
        "client nothing.</p>"
    )


def ticket_description(report: Report, client_partner_id: int | None = None) -> str:
    issue = report.issue
    parts = ["<p>Reported by the Robonomics Report Service.</p>", report_facts(report)]
    if client_partner_id is not None:
        parts.append(client_reference_html(client_partner_id))
    if issue is None:
        parts.append("<p>The report carries logs only, without a described issue.</p>")
    else:
        headline = escape(issue.summary or issue_label(issue.type))
        parts.append(f"<p><b>{headline}</b></p>")
        parts.append(details_html(issue))
    parts.append(files_html(report))
    return "".join(part for part in parts if part)


def build_ticket_values(
    report: Report,
    partner_id: int | None,
    stage_id: int,
    channel_id: int,
    company_id: int,
    client_partner_id: int | None = None,
) -> dict:
    """Values for creating a helpdesk ticket.

    `partner_email` and `email_cc` are deliberately absent, and `stage_id` is
    always the opening stage: this service must never mail a client. A
    `partner_id` is set only when linking was explicitly enabled; otherwise the
    client is referenced in the description instead, which reaches no one.
    """

    issue = report.issue or ReportIssue()
    reference = client_partner_id if partner_id is None else None
    values = {
        "name": ticket_title(report.manifest.client_id, issue),
        "description": ticket_description(report, reference),
        "company_id": company_id,
        "stage_id": stage_id,
        "channel_id": channel_id,
        "priority": ticket_priority(issue),
        "source": ticket_signature(report.manifest.client_id, issue),
        "count": 1,
        "last_occurred": last_occurred(report),
    }
    if partner_id is not None:
        values["partner_id"] = partner_id
    return values


def build_new_ticket_notice(report: Report) -> str:
    """The creation message colleagues receive by e-mail; details are in the
    ticket itself, so this stays short."""

    issue = report.issue or ReportIssue()
    headline = escape(issue.summary or issue_label(issue.type))
    return (
        f"<p><b>{escape(report.manifest.client_id)}</b>: {headline}</p>"
        f"{report_facts(report)}"
    )


def build_repeat_note(report: Report, count: int) -> str:
    """An internal note added when the same problem is reported again."""

    issue = report.issue or ReportIssue()
    headline = escape(issue.summary or issue_label(issue.type))
    return (
        f"<p><b>Reported again</b> (report {count} for this ticket): {headline}</p>"
        f"{report_facts(report)}{details_html(issue) if report.issue else ''}"
    )
