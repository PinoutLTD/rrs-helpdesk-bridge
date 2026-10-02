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

from rrs_helpdesk_bridge.changes import ENTITIES, Changes
from rrs_helpdesk_bridge.manifests import Report, ReportIssue

ISSUE_LABELS = {
    "accumulated_system_log_problems": "System log",
    "entities_health_problems": "Entities health",
    "host_health": "Host health",
    "site_silent": "Site silent",
    "site_back": "Site back",
}

# Service reports of the connector (rrs-connector watchdog.py).
SITE_SILENT = "site_silent"
SITE_BACK = "site_back"
# Issue types filed into the ticket of another type: a site coming back is
# told in the ticket that said it was silent.
SIGNATURE_TYPES = {SITE_BACK: SITE_SILENT}

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
    issue_type = SIGNATURE_TYPES.get(issue.type, issue.type)
    source = f"{client_id}|{issue_type}|{issue_fingerprint(issue)}"
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
    if "ERROR" in levels or issue.type in (
        "entities_health_problems",
        "host_health",
        SITE_SILENT,
    ):
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
    occurred = occurred or report.manifest.datalog_timestamp
    return odoo_datetime(occurred or report.manifest.processed_at)


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
    ]
    if manifest.cid is None:
        rows.append(("Source", "Report Service connector, not a report of the site"))
    else:
        rows += [
            (
                "Datalog",
                f"#{manifest.datalog_index} at {escape(manifest.datalog_timestamp)}",
            ),
            ("IPFS CID", escape(manifest.cid)),
        ]
    rows.append(("Sender", escape(manifest.sender_address)))
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


def _minutes(value: object) -> str:
    if not isinstance(value, int) or value < 0:
        return "unknown"
    return f"{value // 60} h {value % 60} min"


def _percent(value: object) -> str:
    return f"{value}%" if isinstance(value, (int, float)) else "unknown"


def host_health_html(details: dict) -> str:
    """The server itself: memory, its growth, and how the last run ended.

    Every section is optional; the integration sends only what it found.
    """

    parts = []

    memory = details.get("memory")
    if isinstance(memory, dict):
        rows = [
            ("Used now", _percent(memory.get("used_percent"))),
            (
                "Peak",
                f"{_percent(memory.get('peak_percent'))} at "
                f"{escape(memory.get('peak_at', ''))}",
            ),
            (
                "Above the limit",
                f"{_percent(memory.get('limit_percent'))} since "
                f"{escape(memory.get('above_since', ''))}",
            ),
            (
                "Change over the day",
                f"{escape(memory.get('change_percent', ''))} points since "
                f"{escape(memory.get('change_since', ''))}",
            ),
            (
                "Memory",
                f"{escape(memory.get('available_mib', '?'))} MiB available of "
                f"{escape(memory.get('total_mib', '?'))} MiB",
            ),
            (
                "Swap in use",
                f"{escape(memory.get('swap_used_mib', '?'))} MiB of "
                f"{escape(memory.get('swap_total_mib', '?'))} MiB",
            ),
        ]
        parts.append("<p><b>Memory</b></p>" + table(rows))

    growth = details.get("growth")
    if isinstance(growth, dict) and isinstance(growth.get("daily_mean_percent"), dict):
        rows = [
            (str(day), _percent(mean))
            for day, mean in growth["daily_mean_percent"].items()
        ]
        parts.append("<p><b>Memory growing: daily mean</b></p>" + table(rows))

    containers = details.get("containers")
    if isinstance(containers, list) and containers:
        cells = "".join(
            f"<tr><td>{escape(c.get('name', ''))}</td>"
            f"<td align='right'>{escape(c.get('memory_mib', ''))} MiB</td>"
            f"<td align='right'>{_percent(c.get('memory_percent'))}</td></tr>"
            for c in containers
            if isinstance(c, dict)
        )
        parts.append(
            "<p><b>Memory by container</b></p>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th align='left'>Container</th><th>Memory</th><th>Share</th></tr>"
            f"{cells}</table>"
        )

    shutdown = details.get("shutdown")
    if isinstance(shutdown, dict):
        rebooted = shutdown.get("host_rebooted")
        rows = [
            ("Last seen alive", escape(shutdown.get("last_seen") or "unknown")),
            ("Started again", escape(shutdown.get("started", ""))),
            ("Down for", _minutes(shutdown.get("down_minutes"))),
            (
                "Whole host restarted",
                "unknown (no Supervisor)"
                if rebooted is None
                else ("yes" if rebooted else "no, Home Assistant alone"),
            ),
        ]
        if shutdown.get("host_booted"):
            rows.append(("Host booted", escape(shutdown["host_booted"])))
        parts.append("<p><b>Shutdown was not clean</b></p>" + table(rows))

    known = {"memory", "growth", "containers", "shutdown"}
    rest = {key: value for key, value in details.items() if key not in known}
    if rest:
        raw = truncate(json.dumps(rest, ensure_ascii=False, indent=2), MAX_RAW_DETAILS)
        parts.append(f"<p><b>Other details</b></p><pre>{escape(raw)}</pre>")
    return "".join(parts)


def _hours(value: object) -> str:
    return f"{value} h" if isinstance(value, int) else "unknown"


def site_signal_html(issue: ReportIssue) -> str:
    """A site silent, or back: when it last spoke and what it ran."""

    details = issue.details
    if issue.type == SITE_SILENT:
        rows = [
            ("Last signal", escape(details.get("last_signal") or "unknown")),
            ("Silent for", _hours(details.get("silent_hours"))),
        ]
    else:
        rows = [
            ("Silent since", escape(details.get("silent_since") or "unknown")),
            ("Back at", escape(details.get("back_at") or "unknown")),
            ("Was silent for", _hours(details.get("silent_hours"))),
        ]
    rows += [
        ("Last heartbeat", escape(details.get("last_heartbeat") or "none")),
        ("Integration", escape(details.get("integration_version") or "unknown")),
        ("Home Assistant", escape(details.get("ha_version") or "unknown")),
    ]
    return table(rows)


def details_html(issue: ReportIssue) -> str:
    if issue.type == "accumulated_system_log_problems":
        return log_events_html(issue.details)
    if issue.type == "entities_health_problems":
        return entities_html(issue.details)
    if issue.type == "host_health":
        return host_health_html(issue.details)
    if issue.type in (SITE_SILENT, SITE_BACK):
        return site_signal_html(issue)
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


def _grouped(items: dict[str, str], limit: int) -> str:
    """Entities by device (`entity → device`), the way the first report lists them."""

    by_device: dict[str, list[str]] = {}
    for entity, device in items.items():
        by_device.setdefault(device, []).append(entity)
    lines = []
    for device, entities in list(by_device.items())[:limit]:
        names = ", ".join(escape(entity) for entity in sorted(entities))
        lines.append(f"<li>{escape(device) + ': ' if device else ''}{names}</li>")
    hidden = max(len(by_device) - limit, 0)
    more = f"<li>… and {hidden} more</li>" if hidden else ""
    return f"<ul>{''.join(lines)}{more}</ul>"


def _messages(items: dict[str, str], limit: int) -> str:
    lines = [
        f"<li>{escape(truncate(label, MAX_MESSAGE_LENGTH))}</li>"
        for label in list(items.values())[:limit]
    ]
    hidden = max(len(items) - limit, 0)
    more = f"<li>… and {hidden} more</li>" if hidden else ""
    return f"<ul>{''.join(lines)}{more}</ul>"


def changes_html(issue: ReportIssue, changes: Changes) -> str:
    """What moved since the ticket's last report, instead of the whole list."""

    entities = issue.type == ENTITIES
    noun = "unavailable" if entities else "messages"
    if changes.none:
        return (
            "<p><b>No change since the last report</b>: "
            f"{changes.unchanged} {noun}, the same as before.</p>"
        )
    render = (
        (lambda items: _grouped(items, MAX_DEVICES))
        if entities
        else (lambda items: _messages(items, MAX_TOP_EVENTS))
    )
    parts = ["<p><b>Since the last report</b></p>"]
    if changes.new:
        title = "Newly unavailable" if entities else "New messages"
        parts.append(f"<p>{title} ({len(changes.new)}):</p>{render(changes.new)}")
    if changes.gone:
        title = "Back to normal" if entities else "No longer seen"
        parts.append(f"<p>{title} ({len(changes.gone)}):</p>{render(changes.gone)}")
    still = "Still unavailable" if entities else "Seen again"
    parts.append(f"<p>{still}: {changes.unchanged}, as before.</p>")
    return "".join(parts)


def build_repeat_note(
    report: Report,
    count: int,
    files: list[str] | None = None,
    changes: Changes | None = None,
) -> str:
    """An internal note added when the same problem is reported again.

    `files` says what was trimmed or left out of the attachments, so a reader
    knows the earlier reports hold the rest.
    """

    issue = report.issue or ReportIssue()
    headline = escape(issue.summary or issue_label(issue.type))
    files_html = ""
    if files:
        items = "".join(f"<li>{escape(line)}</li>" for line in files)
        files_html = f"<p><b>Attached files</b></p><ul>{items}</ul>"
    if changes is not None:
        body = changes_html(issue, changes)
    else:
        body = details_html(issue) if report.issue else ""
    lead = (
        "<b>The site is back</b>"
        if issue.type == SITE_BACK
        else f"<b>Reported again</b> (report {count} for this ticket)"
    )
    return (
        f"<p>{lead}: {headline}</p>"
        f"{report_facts(report)}{files_html}"
        f"{body}"
    )
