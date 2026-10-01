"""Service reports of the connector: a site gone silent, and back."""

import json
from pathlib import Path

from conftest import CLIENT_ID, LOG_ISSUE, SENDER_ADDRESS, make_report

from rrs_helpdesk_bridge.dispatcher import run_once
from rrs_helpdesk_bridge.state import Action

SILENT = {
    "type": "site_silent",
    "schema_version": 1,
    "ts_start": "2026-10-01T12:37:48+00:00",
    "ts_end": "2026-10-04T15:01:00+00:00",
    "summary": "Site silent: no signal for 74 h",
    "details": {
        "last_signal": "2026-10-01T12:37:48+00:00",
        "silent_hours": 74,
        "silent_after_hours": 72,
        "last_heartbeat": "2026-10-01T12:37:48+00:00",
        "integration_version": "1.1.0-beta.8",
        "ha_version": "2026.1.2",
    },
}

BACK = {
    "type": "site_back",
    "schema_version": 1,
    "ts_start": "2026-10-01T12:37:48+00:00",
    "ts_end": "2026-10-05T09:20:00+00:00",
    "summary": "Site back: silent for 92 h",
    "details": {
        "silent_since": "2026-10-01T12:37:48+00:00",
        "back_at": "2026-10-05T09:20:00+00:00",
        "silent_hours": 92,
        "last_heartbeat": "2026-10-05T09:20:00+00:00",
        "integration_version": "1.1.0-beta.8",
        "ha_version": "2026.1.2",
    },
}


def make_service_report(reports_dir: Path, issue: dict, stamp: str) -> Path:
    """Exactly what rrs-connector's watchdog.write_service_report leaves."""

    directory = reports_dir / CLIENT_ID / f"service_{issue['type']}_{stamp}"
    directory.mkdir(parents=True)
    (directory / "issue_description.json").write_text(json.dumps(issue))
    manifest = {
        "contract_version": 1,
        "report_id": f"{CLIENT_ID}/{directory.name}",
        "client_id": CLIENT_ID,
        "sender_address": SENDER_ADDRESS,
        "source": "connector",
        "datalog_index": None,
        "datalog_timestamp": None,
        "cid": None,
        "processed_at": issue["ts_end"],
        "archive": None,
        "decrypted_dir": None,
        "issue_file": "issue_description.json",
        "files": [],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def test_a_silent_site_gets_a_high_priority_ticket(
    settings, registry, odoo, store, reports_dir
) -> None:
    directory = make_service_report(reports_dir, SILENT, "20261004T150100Z")

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.created == 1 and result.exit_code == 0
    (ticket,) = odoo.tickets.values()
    assert ticket["name"] == f"[{CLIENT_ID}] Site silent: no signal for 74 h"
    assert ticket["priority"] == "2"
    assert ticket["last_occurred"] == "2026-10-04 15:01:00"
    assert "Report Service connector" in ticket["description"]
    assert "IPFS CID" not in ticket["description"]
    assert "1.1.0-beta.8" in ticket["description"]
    assert odoo.attachments == []
    recorded = store.get(f"{CLIENT_ID}/{directory.name}")
    assert recorded.action is Action.CREATED
    # Nothing is pinned behind it, so nothing will ever be unpinned.
    assert store.pinned_reports() == []


def test_a_site_back_is_told_in_the_silence_ticket(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_service_report(reports_dir, SILENT, "20261004T150100Z")
    run_once(settings, registry, odoo, store, dry_run=False)
    make_service_report(reports_dir, BACK, "20261005T100100Z")

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.appended == 1 and result.created == 0
    assert len(odoo.tickets) == 1
    (note,) = odoo.notes
    assert "The site is back" in note[1]
    assert "92 h" in note[1]


def test_a_site_back_without_an_open_ticket_files_nothing(
    settings, registry, odoo, store, reports_dir
) -> None:
    directory = make_service_report(reports_dir, BACK, "20261005T100100Z")

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.skipped == 1 and result.created == 0
    assert odoo.tickets == {}
    assert store.get(f"{CLIENT_ID}/{directory.name}").action is Action.SKIPPED


def test_silence_has_its_own_ticket_beside_the_site_reports(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    make_service_report(reports_dir, SILENT, "20261004T150100Z")

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.created == 2
    assert len({t["source"] for t in odoo.tickets.values()}) == 2
