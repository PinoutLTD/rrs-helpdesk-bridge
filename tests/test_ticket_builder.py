import pytest
from conftest import CLIENT_ID, ENTITIES_ISSUE, LOG_ISSUE, make_report

from rrs_helpdesk_bridge.manifests import ReportIssue, load_report
from rrs_helpdesk_bridge.ticket_builder import (
    MAX_TITLE_LENGTH,
    build_ticket_values,
    last_occurred,
    ticket_description,
    ticket_priority,
    ticket_signature,
    ticket_title,
)


def report_for(reports_dir, issue, **kwargs):
    directory = make_report(reports_dir, 94, 1789114351000, issue, **kwargs)
    return load_report(directory / "manifest.json")


def test_title_is_the_site_and_the_summary() -> None:
    issue = ReportIssue.model_validate(LOG_ISSUE)

    assert ticket_title(CLIENT_ID, issue) == (
        f"[{CLIENT_ID}] System log: 2 unique issues, 12 occurrences (last 60 min)"
    )


def test_long_title_is_truncated() -> None:
    issue = ReportIssue.model_validate(dict(LOG_ISSUE, summary="x" * 300))

    title = ticket_title(CLIENT_ID, issue)

    assert len(title) == MAX_TITLE_LENGTH
    assert title.endswith("…")


def test_title_falls_back_to_the_issue_type() -> None:
    issue = ReportIssue.model_validate({"type": "entities_health_problems"})

    assert ticket_title(CLIENT_ID, issue) == f"[{CLIENT_ID}] Entities health"


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        ({"CRITICAL": 1, "ERROR": 4}, "3"),
        ({"ERROR": 4}, "2"),
        ({"WARNING": 4}, "1"),
        ({}, "1"),
    ],
)
def test_priority_follows_the_worst_level(levels: dict, expected: str) -> None:
    issue = ReportIssue.model_validate(
        {
            "type": "accumulated_system_log_problems",
            "details": {"by_level_events": levels},
        }
    )

    assert ticket_priority(issue) == expected


def test_unavailable_entities_are_high_priority() -> None:
    assert ticket_priority(ReportIssue.model_validate(ENTITIES_ISSUE)) == "2"


def test_signature_is_stable_per_site_and_type() -> None:
    log_issue = ReportIssue.model_validate(LOG_ISSUE)
    other_report = ReportIssue.model_validate(
        dict(LOG_ISSUE, summary="different numbers", details={})
    )
    entities_issue = ReportIssue.model_validate(ENTITIES_ISSUE)

    assert ticket_signature(CLIENT_ID, log_issue) == ticket_signature(
        CLIENT_ID, other_report
    )
    assert ticket_signature(CLIENT_ID, log_issue) != ticket_signature(
        "del-mar-24f", log_issue
    )
    assert ticket_signature(CLIENT_ID, log_issue) != ticket_signature(
        CLIENT_ID, entities_issue
    )


def test_last_occurred_uses_the_issue_period_end(reports_dir) -> None:
    report = report_for(reports_dir, LOG_ISSUE)

    assert last_occurred(report) == "2026-09-14 08:12:31"


def test_last_occurred_falls_back_to_the_datalog_time(reports_dir) -> None:
    report = report_for(reports_dir, dict(LOG_ISSUE, ts_end=None))

    assert last_occurred(report) == "2026-09-14 08:12:34"


def test_description_escapes_log_messages(reports_dir) -> None:
    dangerous = dict(LOG_ISSUE)
    dangerous["details"] = dict(LOG_ISSUE["details"])
    dangerous["details"]["top_events"] = [
        dict(LOG_ISSUE["details"]["top_events"][0], message="<script>alert(1)</script>")
    ]
    report = report_for(reports_dir, dangerous)

    description = ticket_description(report)

    assert "<script>" not in description
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in description


def test_description_lists_unavailable_devices(reports_dir) -> None:
    report = report_for(reports_dir, ENTITIES_ISSUE)

    description = ticket_description(report)

    assert "Kitchen sensor" in description
    assert "sensor.kitchen_temp" in description
    assert "light.hall" in description


def test_unknown_issue_type_keeps_its_details(reports_dir) -> None:
    report = report_for(
        reports_dir,
        {"type": "future_problem", "summary": "Something new", "details": {"a": 1}},
    )

    description = ticket_description(report)

    assert "Something new" in description
    assert "&quot;a&quot;: 1" in description or '"a": 1' in description


def test_values_never_carry_address_fields(reports_dir) -> None:
    report = report_for(reports_dir, LOG_ISSUE)

    values = build_ticket_values(report, 7, stage_id=1, channel_id=4, company_id=1)

    assert set(values) == {
        "name",
        "description",
        "company_id",
        "stage_id",
        "channel_id",
        "priority",
        "source",
        "count",
        "last_occurred",
        "partner_id",
    }


def test_truncated_lists_say_how_much_is_hidden(reports_dir) -> None:
    devices = {
        f"dev{number}": {
            "device_name": f"Device {number}",
            "entities": [f"sensor.s{number}"],
        }
        for number in range(40)
    }
    issue = dict(ENTITIES_ISSUE)
    issue["details"] = {
        "unavailable_entities": {"devices": devices, "pure_entities": []}
    }
    report = report_for(reports_dir, issue)

    description = ticket_description(report)

    assert "Device 29" in description
    assert "Device 30" not in description
    assert "and 10 more unavailable" in description


def test_client_is_referenced_but_not_linked_by_default(reports_dir) -> None:
    report = report_for(reports_dir, LOG_ISSUE)

    values = build_ticket_values(
        report, None, stage_id=1, channel_id=4, company_id=1, client_partner_id=7
    )

    assert "partner_id" not in values
    assert "id=7&model=res.partner" in values["description"]


# What the integration sends (rrs-ha-integration host_health.py, schema 1).
HOST_MEMORY_ISSUE = {
    "type": "host_health",
    "schema_version": 1,
    "ts_start": "2026-10-08T10:00:00+00:00",
    "ts_end": "2026-10-08T10:40:00+00:00",
    "summary": "Host health: memory above 90% for 30 min (peak 96%)",
    "details": {
        "memory": {
            "finding": "memory",
            "limit_percent": 90.0,
            "above_since": "2026-10-08T10:00:00+00:00",
            "peak_percent": 96.2,
            "peak_at": "2026-10-08T10:30:00+00:00",
            "used_percent": 95.1,
            "total_mib": 3793,
            "available_mib": 186,
            "swap_used_mib": 2499,
            "swap_total_mib": 2560,
            "change_percent": 21.4,
            "change_since": "2026-10-07T10:40:00+00:00",
        },
        "containers": [
            {"name": "Home Assistant Core", "memory_mib": 1210, "memory_percent": 31.9},
            {"name": "<Mosquitto>", "memory_mib": 40, "memory_percent": 1.1},
        ],
    },
}

HOST_SHUTDOWN_ISSUE = {
    "type": "host_health",
    "schema_version": 1,
    "ts_start": "2026-09-28T19:15:54+00:00",
    "ts_end": "2026-09-29T04:49:30+00:00",
    "summary": "Host health: host was down 9 h 34 min, shutdown not clean",
    "details": {
        "shutdown": {
            "finding": "shutdown",
            "clean": False,
            "last_seen": "2026-09-28T19:15:54+00:00",
            "started": "2026-09-29T04:49:30+00:00",
            "down_minutes": 574,
            "host_booted": "2026-09-29T04:48:00+00:00",
            "host_rebooted": True,
        }
    },
}


def test_host_memory_report_reads_as_tables(reports_dir) -> None:
    description = ticket_description(report_for(reports_dir, HOST_MEMORY_ISSUE))

    assert "<b>Memory</b>" in description
    assert "96.2% at 2026-10-08T10:30:00+00:00" in description
    assert "2499 MiB of 2560 MiB" in description
    assert "Home Assistant Core" in description and "1210 MiB" in description
    # Container names come from the site: escaped like any other text.
    assert "&lt;Mosquitto&gt;" in description and "<Mosquitto>" not in description
    assert "&quot;finding&quot;" not in description and '"finding"' not in description


def test_unclean_shutdown_report_says_how_long_and_what_restarted(reports_dir) -> None:
    description = ticket_description(report_for(reports_dir, HOST_SHUTDOWN_ISSUE))

    assert "Shutdown was not clean" in description
    assert "9 h 34 min" in description
    assert "Whole host restarted" in description and ">yes<" in description


def test_shutdown_without_a_supervisor_does_not_guess(reports_dir) -> None:
    issue = dict(HOST_SHUTDOWN_ISSUE)
    issue["details"] = {
        "shutdown": {
            k: v
            for k, v in HOST_SHUTDOWN_ISSUE["details"]["shutdown"].items()
            if k not in ("host_booted", "host_rebooted")
        }
    }

    description = ticket_description(report_for(reports_dir, issue))

    assert "unknown (no Supervisor)" in description
    assert "Host booted" not in description


def test_host_health_is_high_priority_and_keeps_unknown_sections(reports_dir) -> None:
    issue = dict(HOST_MEMORY_ISSUE)
    issue["details"] = {"backups": {"finding": "backups", "days": 40}}
    report = report_for(reports_dir, issue)

    assert ticket_priority(report.issue) == "2"
    assert "Other details" in ticket_description(report)
    assert "backups" in ticket_description(report)
