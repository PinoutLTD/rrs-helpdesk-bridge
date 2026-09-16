"""From a report directory to a helpdesk ticket.

One run reads every manifest it has not handled yet, oldest first, and either
opens a ticket or appends to the open ticket carrying the same problem
signature. Dry run is the default: it reads Odoo, decides what it would do,
prints it, and records nothing.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from rrs_helpdesk_bridge.config import ClientRegistry, EnvSettings
from rrs_helpdesk_bridge.manifests import (
    ManifestError,
    Report,
    find_manifests,
    load_report,
    resolve_inside,
)
from rrs_helpdesk_bridge.odoo_client import OdooClient, TicketSummary
from rrs_helpdesk_bridge.state import Action, StateStore
from rrs_helpdesk_bridge.ticket_builder import (
    build_repeat_note,
    build_ticket_values,
    last_occurred,
    ticket_signature,
)

LOGGER = logging.getLogger(__name__)

NO_ISSUE_DETAIL = "report carries logs only, without an issue"
NO_PARTNER_DETAIL = "client_id is not in the registry, ticket has no partner"


@dataclass
class RunResult:
    seen: int = 0
    handled: int = 0
    created: int = 0
    appended: int = 0
    skipped: int = 0
    failed: int = 0
    unreadable: int = 0
    dry_run: bool = True
    planned: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 3 if self.failed or self.unreadable else 0


def attachment_payloads(
    report: Report, settings: EnvSettings
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Read the decrypted files that fit the size limit."""

    payloads: list[tuple[str, bytes]] = []
    skipped: list[str] = []
    for file in report.manifest.files:
        if file.size_bytes > settings.max_attachment_bytes:
            skipped.append(file.name)
            continue
        path = resolve_inside(report.directory, file.path)
        payloads.append((f"{report.directory.name}-{file.name}", path.read_bytes()))
    return payloads, skipped


def attach_files(
    odoo: OdooClient, ticket_id: int, report: Report, settings: EnvSettings
) -> None:
    if not settings.attach_files:
        return
    payloads, skipped = attachment_payloads(report, settings)
    for name, data in payloads:
        odoo.attach_file(ticket_id, name, data)
    if skipped:
        LOGGER.warning(
            "Report %s: files over the attachment limit were left on disk: %s",
            report.manifest.report_id,
            ", ".join(skipped),
        )


def open_ticket(
    odoo: OdooClient,
    report: Report,
    registry: ClientRegistry,
    settings: EnvSettings,
) -> tuple[int, str | None]:
    partner_id = registry.partner_id(report.manifest.client_id)
    values = build_ticket_values(
        report,
        partner_id,
        stage_id=settings.stage_id,
        channel_id=settings.channel_id,
        company_id=settings.company_id,
    )
    ticket_id = odoo.create_ticket(values)
    attach_files(odoo, ticket_id, report, settings)
    return ticket_id, None if partner_id else NO_PARTNER_DETAIL


def append_to_ticket(
    odoo: OdooClient,
    ticket: TicketSummary,
    report: Report,
    settings: EnvSettings,
) -> None:
    count = ticket.count + 1
    odoo.update_ticket(
        ticket.id, {"count": count, "last_occurred": last_occurred(report)}
    )
    odoo.post_note(ticket.id, build_repeat_note(report, count))
    attach_files(odoo, ticket.id, report, settings)


def dispatch_report(
    report: Report,
    odoo: OdooClient,
    store: StateStore,
    registry: ClientRegistry,
    settings: EnvSettings,
    dry_run: bool,
    result: RunResult,
) -> None:
    manifest = report.manifest

    if report.issue is None:
        LOGGER.info("Report %s: %s, no ticket", manifest.report_id, NO_ISSUE_DETAIL)
        result.skipped += 1
        if not dry_run:
            store.record(
                manifest.report_id,
                manifest.client_id,
                "",
                Action.SKIPPED,
                detail=NO_ISSUE_DETAIL,
            )
        return

    signature = ticket_signature(manifest.client_id, report.issue)
    existing = odoo.find_open_ticket(signature)

    if dry_run:
        action = f"append to {existing.number}" if existing else "create a ticket"
        LOGGER.info(
            "Report %s (%s): would %s [signature %s]",
            manifest.report_id,
            report.issue.type,
            action,
            signature,
        )
        result.planned.append(f"{manifest.report_id}: {action}")
        result.appended += 1 if existing else 0
        result.created += 0 if existing else 1
        return

    if existing is None:
        ticket_id, detail = open_ticket(odoo, report, registry, settings)
        store.record(
            manifest.report_id,
            manifest.client_id,
            signature,
            Action.CREATED,
            ticket_id,
            detail,
        )
        result.created += 1
        LOGGER.info(
            "Report %s: created ticket %d [signature %s]",
            manifest.report_id,
            ticket_id,
            signature,
        )
        return

    append_to_ticket(odoo, existing, report, settings)
    store.record(
        manifest.report_id,
        manifest.client_id,
        signature,
        Action.APPENDED,
        existing.id,
    )
    result.appended += 1
    LOGGER.info(
        "Report %s: appended to ticket %s [signature %s]",
        manifest.report_id,
        existing.number or existing.id,
        signature,
    )


def load_pending_reports(
    reports_dir: Path, store: StateStore, result: RunResult
) -> list[Report]:
    reports: list[Report] = []
    for manifest_path in find_manifests(reports_dir):
        result.seen += 1
        try:
            report = load_report(manifest_path)
        except ManifestError as e:
            LOGGER.error("Cannot read %s: %s", manifest_path, e)
            result.unreadable += 1
            continue
        if store.is_handled(report.manifest.report_id):
            result.handled += 1
            continue
        reports.append(report)
    # Oldest report first, so a ticket's notes read in chronological order.
    return sorted(reports, key=lambda report: report.manifest.processed_at)


def run_once(
    settings: EnvSettings,
    registry: ClientRegistry,
    odoo: OdooClient,
    store: StateStore,
    dry_run: bool = True,
) -> RunResult:
    result = RunResult(dry_run=dry_run)
    reports = load_pending_reports(settings.reports_dir, store, result)

    for report in reports:
        try:
            dispatch_report(report, odoo, store, registry, settings, dry_run, result)
        except Exception as e:
            LOGGER.exception("Failed to file report %s", report.manifest.report_id)
            result.failed += 1
            if not dry_run:
                store.record(
                    report.manifest.report_id,
                    report.manifest.client_id,
                    "",
                    Action.FAILED,
                    detail=str(e),
                )

    LOGGER.info(
        "Run finished%s: manifests=%d already handled=%d unreadable=%d; "
        "tickets created=%d appended=%d; skipped=%d failed=%d",
        " (dry run, nothing was written)" if dry_run else "",
        result.seen,
        result.handled,
        result.unreadable,
        result.created,
        result.appended,
        result.skipped,
        result.failed,
    )
    return result
