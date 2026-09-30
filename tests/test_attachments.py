"""A report appended to a ticket attaches only what the ticket does not hold."""

import json
from datetime import datetime

from conftest import LOG_ISSUE, make_report

from rrs_helpdesk_bridge.attachments import lines_in_period
from rrs_helpdesk_bridge.dispatcher import run_once

DAY_1 = {
    **LOG_ISSUE,
    "ts_start": "2026-09-13T08:00:00+00:00",
    "ts_end": "2026-09-14T08:00:00+00:00",
}
DAY_2 = {
    **LOG_ISSUE,
    "ts_start": "2026-09-14T08:00:00+00:00",
    "ts_end": "2026-09-15T08:00:00+00:00",
}
TRACES = b'{"trace": "unchanged between reports"}'


def record(ts: str, message: str) -> bytes:
    return (
        json.dumps(
            {"ts": ts, "level": "ERROR", "name": "x", "message": [message]}
        ).encode()
        + b"\n"
    )


# The integration's log is cumulative: day 2 still holds day 1's lines.
LOG_DAY_1 = record("2026-09-13T20:00:00+00:00", "old") + record(
    "2026-09-14T07:00:00+00:00", "day 1"
)
LOG_DAY_2 = LOG_DAY_1 + record("2026-09-14T21:00:00+00:00", "day 2")


def files(log: bytes) -> dict[str, bytes]:
    return {"home-assistant.log": log, "trace.saved_traces": TRACES}


def attached(odoo, ticket_id=None):
    return [
        (name.split("-", 1)[1], data)
        for tid, name, data in odoo.attached_data
        if ticket_id is None or tid == ticket_id
    ]


def test_only_records_inside_the_period_are_kept():
    start = datetime.fromisoformat("2026-09-14T08:00:00+00:00")
    end = datetime.fromisoformat("2026-09-15T08:00:00+00:00")

    kept, count, total = lines_in_period(LOG_DAY_2 + b"not a record\n", start, end)

    assert kept == record("2026-09-14T21:00:00+00:00", "day 2")
    assert (count, total) == (1, 3)


def test_a_file_without_timestamped_records_is_not_guessed_at():
    start = datetime.fromisoformat("2026-09-14T08:00:00+00:00")

    assert lines_in_period(b"plain text log\n", start, start) is None


def test_the_first_report_attaches_everything(
    settings, registry, odoo, store, reports_dir
):
    make_report(reports_dir, 94, 1789114351000, DAY_1, extra_files=files(LOG_DAY_1))

    run_once(settings, registry, odoo, store, dry_run=False)

    by_name = dict(attached(odoo))
    assert by_name["home-assistant.log"] == LOG_DAY_1
    assert by_name["trace.saved_traces"] == TRACES


def test_a_repeat_attaches_its_own_period_and_skips_repeated_files(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, DAY_1, extra_files=files(LOG_DAY_1))
    run_once(settings, registry, odoo, store, dry_run=False)
    make_report(reports_dir, 95, 1789200751000, DAY_2, extra_files=files(LOG_DAY_2))
    before = len(odoo.attached_data)

    run_once(settings, registry, odoo, store, dry_run=False)

    new = dict(
        (name.split("-", 1)[1], data) for _, name, data in odoo.attached_data[before:]
    )
    assert new["home-assistant.log"] == record("2026-09-14T21:00:00+00:00", "day 2")
    assert "trace.saved_traces" not in new  # the same bytes are on the ticket
    assert "issue_description.json" in new
    note = odoo.notes[-1][1]
    assert "home-assistant.log: 1 of 3 lines, those of this report's period" in note
    assert "trace.saved_traces: unchanged since an earlier report" in note


def test_a_repeat_with_no_new_lines_attaches_no_log(
    settings, registry, odoo, store, reports_dir
):
    make_report(reports_dir, 94, 1789114351000, DAY_1, extra_files=files(LOG_DAY_1))
    run_once(settings, registry, odoo, store, dry_run=False)
    make_report(reports_dir, 95, 1789200751000, DAY_2, extra_files=files(LOG_DAY_1))
    before = len(odoo.attached_data)

    run_once(settings, registry, odoo, store, dry_run=False)

    new = [name.split("-", 1)[1] for _, name, _ in odoo.attached_data[before:]]
    assert new == ["issue_description.json"]
    assert "home-assistant.log: no lines in this report's period" in odoo.notes[-1][1]


def test_trimming_can_be_turned_off(settings, registry, odoo, store, reports_dir):
    settings = settings.model_copy(update={"trim_repeated_logs": False})
    make_report(reports_dir, 94, 1789114351000, DAY_1, extra_files=files(LOG_DAY_1))
    run_once(settings, registry, odoo, store, dry_run=False)
    make_report(reports_dir, 95, 1789200751000, DAY_2, extra_files=files(LOG_DAY_2))
    before = len(odoo.attached_data)

    run_once(settings, registry, odoo, store, dry_run=False)

    new = dict(
        (name.split("-", 1)[1], data) for _, name, data in odoo.attached_data[before:]
    )
    assert new["home-assistant.log"] == LOG_DAY_2
