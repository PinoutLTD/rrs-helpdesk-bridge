"""Unpinning a report's archive from Pinata once nobody needs it there.

A report's encrypted archive stays pinned on Pinata after the connector has
fetched it. It is unpinned
- when the ticket it was filed to is closed, or
- six months after the site published it, whatever the ticket's state; a
  report that opened no ticket (logs only) goes then too.

Only CIDs of our own reports are ever unpinned: each comes from a report
manifest, which the connector writes for records published by the sites in
its registry. The same Pinata account holds another project's files, and a
key cannot be limited to some of them; so nothing here lists the account,
filters by name or date, or unpins anything it was not given by a manifest.

The key used can unpin and do nothing else (`rrs-admin pinata-unpin-key`).
Unpinning is off unless RRSB_UNPIN_ENABLED is set; without it a run says what
it would unpin and why.
"""

import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import SecretStr

from rrs_helpdesk_bridge.state import Action, PinnedReport, StateStore

LOGGER = logging.getLogger(__name__)

UNPIN_URL = "https://api.pinata.cloud/pinning/unpin/"
REASON_CLOSED = "ticket closed"
REASON_AGE = "older than the retention period"


class UnpinError(RuntimeError):
    pass


class Unpinner(Protocol):
    def unpin(self, cid: str) -> bool:
        """Unpin; True if it was pinned, False if Pinata no longer had it."""


class ClosedTickets(Protocol):
    def closed_ticket_ids(self, ticket_ids: list[int]) -> set[int]: ...


@dataclass(frozen=True)
class PinataUnpinner:
    api_key: SecretStr
    api_secret: SecretStr
    timeout: float = 30

    def unpin(self, cid: str) -> bool:
        request = urllib.request.Request(
            UNPIN_URL + urllib.parse.quote(cid, safe=""),
            method="DELETE",
            headers={
                "pinata_api_key": self.api_key.get_secret_value(),
                "pinata_secret_api_key": self.api_secret.get_secret_value(),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return True
        except urllib.error.HTTPError as e:
            body = e.read(300).decode(errors="replace")
            # Unpinned already, by the site after a failed publish or by hand.
            if e.code == 404 or "NOT_PINNED" in body.upper():
                return False
            raise UnpinError(f"Pinata answered {e.code} to unpinning {cid}") from e
        except urllib.error.URLError as e:
            raise UnpinError(f"cannot reach Pinata: {e.reason}") from e


@dataclass
class UnpinResult:
    unpinned: int = 0
    already_gone: int = 0
    planned: list[str] = field(default_factory=list)
    failed: int = 0


def due(
    reports: list[PinnedReport], closed: set[int], now: datetime, keep: timedelta
) -> list[tuple[PinnedReport, str]]:
    """Which pinned reports should go now, and why."""

    chosen = []
    for report in reports:
        if report.odoo_ticket_id is not None and report.odoo_ticket_id in closed:
            chosen.append((report, REASON_CLOSED))
            continue
        try:
            published = datetime.fromisoformat(report.published_at)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is not None and now - published >= keep:
            chosen.append((report, REASON_AGE))
    return chosen


def unpin_reports(
    store: StateStore,
    odoo: ClosedTickets,
    now: datetime,
    keep: timedelta,
    unpinner: Callable[[], Unpinner] | None,
) -> UnpinResult:
    """Unpin what is due; with no unpinner, only say what would be unpinned."""

    result = UnpinResult()
    reports = store.pinned_reports()
    ticket_ids = sorted(
        {
            r.odoo_ticket_id
            for r in reports
            if r.odoo_ticket_id is not None
            and r.action in (Action.CREATED, Action.APPENDED)
        }
    )
    closed = odoo.closed_ticket_ids(ticket_ids) if ticket_ids else set()
    chosen = due(reports, closed, now, keep)
    if not chosen:
        return result

    if unpinner is None:
        for report, reason in chosen:
            result.planned.append(f"{report.cid} ({report.report_id}): {reason}")
            LOGGER.info(
                "Would unpin %s (%s): %s", report.cid, report.report_id, reason
            )
        return result

    client = unpinner()
    for report, reason in chosen:
        try:
            was_pinned = client.unpin(report.cid)
        except UnpinError as e:
            LOGGER.warning("Report %s: not unpinned yet: %s", report.report_id, e)
            result.failed += 1
            continue
        store.mark_unpinned(report.cid, report.report_id, reason)
        if was_pinned:
            result.unpinned += 1
            LOGGER.info("Unpinned %s (%s): %s", report.cid, report.report_id, reason)
        else:
            result.already_gone += 1
            LOGGER.info(
                "%s (%s) was no longer pinned: %s", report.cid, report.report_id, reason
            )
    return result
