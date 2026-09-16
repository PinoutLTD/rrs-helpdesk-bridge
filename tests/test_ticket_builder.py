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
