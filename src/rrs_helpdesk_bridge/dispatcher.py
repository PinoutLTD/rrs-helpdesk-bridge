"""From a report directory to a helpdesk ticket.

One run reads every manifest it has not handled yet, oldest first, and either
opens a ticket or appends to the open ticket carrying the same problem
signature. Dry run is the default: it reads Odoo, decides what it would do,
prints it, and records nothing.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rrs_helpdesk_bridge.attachments import AttachmentPlan, plan_attachments
from rrs_helpdesk_bridge.config import ClientRegistry, EnvSettings
from rrs_helpdesk_bridge.manifests import (
    ManifestError,
    Report,
    find_manifests,
    load_report,
    read_manifest,
)
from rrs_helpdesk_bridge.odoo_client import OdooClient, TicketSummary
from rrs_helpdesk_bridge.state import Action, StateStore
from rrs_helpdesk_bridge.ticket_builder import (
    SITE_BACK,
    build_new_ticket_notice,
    build_repeat_note,
    build_ticket_values,
    last_occurred,
    ticket_signature,
)
from rrs_helpdesk_bridge.unpin import Unpinner, unpin_reports

LOGGER = logging.getLogger(__name__)

NO_ISSUE_DETAIL = "report carries logs only, without an issue"
NO_SILENCE_DETAIL = "site back, and no open ticket says it was silent"
NO_PARTNER_DETAIL = "client_id is not in the registry, ticket has no partner"


@dataclass
class Recipients:
    """Colleagues notified about new tickets, resolved once per run."""

    partner_ids: list[int] = field(default_factory=list)
    subtype_id: int | None = None

    @property
    def usable(self) -> bool:
        return bool(self.partner_ids) and self.subtype_id is not None


@dataclass
class RunResult:
    seen: int = 0
    handled: int = 0
    created: int = 0
    appended: int = 0
    skipped: int = 0
    failed: int = 0
    unreadable: int = 0
    notified: int = 0
    unpinned: int = 0
    unpin_failed: int = 0
    unpin_planned: list[str] = field(default_factory=list)
    dry_run: bool = True
    planned: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 3 if self.failed or self.unreadable else 0


def published_at(manifest) -> str | None:
    if manifest.datalog_timestamp is None:
        return None
    return manifest.datalog_timestamp.astimezone(UTC).isoformat()


def source(manifest) -> dict[str, str | None]:
    """What unpinning needs later, after the connector deleted the report.

    A service report of the connector has nothing pinned: no CID.
    """

    return {"cid": manifest.cid, "published_at": published_at(manifest)}


def attach_files(
    odoo: OdooClient, ticket_id: int, report: Report, plan: AttachmentPlan
) -> None:
    for name, data in plan.payloads:
        odoo.attach_file(ticket_id, name, data)
    if plan.skipped:
        LOGGER.warning(
            "Report %s: not attached: %s",
            report.manifest.report_id,
            ", ".join(plan.skipped),
        )


def resolve_recipients(odoo: OdooClient, registry: ClientRegistry) -> Recipients:
    """Who gets an e-mail about a new ticket, and through which subtype."""

    if not registry.notify_emails:
        return Recipients()

    found = odoo.find_internal_partners(registry.notify_emails)
    missing = [
        email for email in registry.notify_emails if email.strip().lower() not in found
    ]
    if missing:
        LOGGER.warning(
            "No internal Odoo user for %s: nobody will be notified at that "
            "address (only staff accounts can be subscribed)",
            ", ".join(missing),
        )
    if not found:
        return Recipients()

    subtype_id = odoo.find_created_subtype_id()
    if subtype_id is None:
        LOGGER.error(
            "The 'Ticket Created' subtype was not found: skipping "
            "notifications rather than subscribing to everything"
        )
        return Recipients()

    LOGGER.info("New tickets will notify: %s", ", ".join(sorted(found)))
    return Recipients(partner_ids=sorted(found.values()), subtype_id=subtype_id)


def notify_new_ticket(
    odoo: OdooClient, ticket_id: int, report: Report, recipients: Recipients
) -> bool:
    """Subscribe the colleagues and post the message that reaches their inbox.

    They are subscribed to the creation subtype only, so later notes on the
    same ticket stay silent.
    """

    if not recipients.usable:
        return False
    odoo.subscribe(ticket_id, recipients.partner_ids, [recipients.subtype_id])
    odoo.announce_ticket(ticket_id, build_new_ticket_notice(report))
    return True


def open_ticket(
    odoo: OdooClient,
    report: Report,
    registry: ClientRegistry,
    settings: EnvSettings,
) -> tuple[int, str | None]:
    partner_id = registry.partner_id(report.manifest.client_id)
    values = build_ticket_values(
        report,
        partner_id if settings.link_client_partner else None,
        stage_id=settings.stage_id,
        channel_id=settings.channel_id,
        company_id=settings.company_id,
        client_partner_id=partner_id,
    )
    ticket_id = odoo.create_ticket(values)
    if settings.attach_files:
        # The report that opens a ticket carries its whole history.
        attach_files(odoo, ticket_id, report, plan_attachments(report, settings))
    return ticket_id, None if partner_id else NO_PARTNER_DETAIL


def append_to_ticket(
    odoo: OdooClient,
    ticket: TicketSummary,
    report: Report,
    settings: EnvSettings,
) -> None:
    count = ticket.count + 1
    plan = AttachmentPlan()
    if settings.attach_files:
        on_ticket = odoo.attachment_checksums(ticket.id)
        plan = plan_attachments(report, settings, repeat=True, on_ticket=on_ticket)
    # The note goes first: if anything below fails, the retry starts from the
    # same ticket counter and cannot inflate it. A repeated note is visible,
    # a wrong number is not.
    odoo.post_note(ticket.id, build_repeat_note(report, count, plan.notes))
    odoo.update_ticket(
        ticket.id, {"count": count, "last_occurred": last_occurred(report)}
    )
    attach_files(odoo, ticket.id, report, plan)


def dispatch_report(
    report: Report,
    odoo: OdooClient,
    store: StateStore,
    registry: ClientRegistry,
    settings: EnvSettings,
    recipients: Recipients,
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
                **source(manifest),
            )
        return

    signature = ticket_signature(manifest.client_id, report.issue)
    existing = odoo.find_open_ticket(signature)

    # "Back" belongs in the ticket that said the site was silent; with that
    # ticket closed already, there is nobody left to tell.
    if report.issue.type == SITE_BACK and existing is None:
        LOGGER.info("Report %s: %s, no ticket", manifest.report_id, NO_SILENCE_DETAIL)
        result.skipped += 1
        if not dry_run:
            store.record(
                manifest.report_id,
                manifest.client_id,
                signature,
                Action.SKIPPED,
                detail=NO_SILENCE_DETAIL,
                **source(manifest),
            )
        return

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
        if notify_new_ticket(odoo, ticket_id, report, recipients):
            result.notified += 1
        store.record(
            manifest.report_id,
            manifest.client_id,
            signature,
            Action.CREATED,
            ticket_id,
            detail,
            **source(manifest),
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
        **source(manifest),
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
            # Whether it was handled is decided by the manifest alone: the
            # files of an old report may be gone by now, and they are not
            # needed to skip it.
            manifest = read_manifest(manifest_path)
            if store.is_handled(manifest.report_id):
                # Reports handled before the CID was recorded get it now, while
                # the connector still has their manifest (it keeps 30 days).
                if manifest.cid:
                    store.remember_source(
                        manifest.report_id, manifest.cid, published_at(manifest)
                    )
                result.handled += 1
                continue
            report = load_report(manifest_path, manifest)
        except ManifestError as e:
            LOGGER.error("Cannot read %s: %s", manifest_path, e)
            result.unreadable += 1
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
    unpinner: Callable[[], Unpinner] | None = None,
    now: datetime | None = None,
) -> RunResult:
    """File pending reports, then unpin what is due.

    `unpinner` makes the Pinata client; it is called only when something is
    due, and only when this is not a dry run and unpinning is enabled.
    """

    result = RunResult(dry_run=dry_run)
    reports = load_pending_reports(settings.reports_dir, store, result)
    recipients = resolve_recipients(odoo, registry) if reports else Recipients()

    for report in reports:
        try:
            dispatch_report(
                report, odoo, store, registry, settings, recipients, dry_run, result
            )
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

    try:
        unpin = unpin_reports(
            store,
            odoo,
            now or datetime.now(UTC),
            timedelta(days=settings.unpin_after_days),
            unpinner if settings.unpin_enabled and not dry_run else None,
        )
        result.unpinned = unpin.unpinned + unpin.already_gone
        result.unpin_failed = unpin.failed
        result.unpin_planned = unpin.planned
    except Exception:
        # Filing is the job; unpinning can wait for the next run.
        LOGGER.exception("Unpinning failed")
        result.unpin_failed += 1

    LOGGER.info(
        "Run finished%s: manifests=%d already handled=%d unreadable=%d; "
        "tickets created=%d appended=%d notified=%d; skipped=%d failed=%d; "
        "unpinned=%d unpin failed=%d would unpin=%d",
        " (dry run, nothing was written)" if dry_run else "",
        result.seen,
        result.handled,
        result.unreadable,
        result.created,
        result.appended,
        result.notified,
        result.skipped,
        result.failed,
        result.unpinned,
        result.unpin_failed,
        len(result.unpin_planned),
    )
    return result
