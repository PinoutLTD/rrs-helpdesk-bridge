import json
from pathlib import Path

from conftest import CLIENT_ID, ENTITIES_ISSUE, LOG_ISSUE, PARTNER_ID, make_report

from rrs_helpdesk_bridge.dispatcher import run_once
from rrs_helpdesk_bridge.state import Action


def test_report_becomes_a_ticket(settings, registry, odoo, store, reports_dir) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.created == 1
    assert result.exit_code == 0
    (ticket,) = odoo.tickets.values()
    assert ticket["name"].startswith(f"[{CLIENT_ID}] System log:")
    assert ticket["partner_id"] == PARTNER_ID
    assert ticket["stage_id"] == settings.stage_id
    assert ticket["channel_id"] == settings.channel_id
    assert ticket["priority"] == "2"
    assert ticket["count"] == 1
    assert ticket["last_occurred"] == "2026-09-14 08:12:31"
    assert [name for _, name, _ in odoo.attachments] == [
        "datalog_94_1789114351000-home-assistant.log",
        "datalog_94_1789114351000-issue_description.json",
    ]
    recorded = store.get(f"{CLIENT_ID}/datalog_94_1789114351000")
    assert recorded.action is Action.CREATED
    assert recorded.odoo_ticket_id == ticket_id_of(odoo)


def ticket_id_of(odoo) -> int:
    (ticket_id,) = odoo.tickets
    return ticket_id


def test_no_mail_fields_are_ever_written(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)

    run_once(settings, registry, odoo, store, dry_run=False)

    (ticket,) = odoo.tickets.values()
    assert "partner_email" not in ticket
    assert "email_cc" not in ticket
    # The opening stage only; closing stages carry a customer e-mail template.
    assert ticket["stage_id"] == 1


def test_same_problem_appends_to_the_open_ticket(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    run_once(settings, registry, odoo, store, dry_run=False)
    later = dict(LOG_ISSUE, ts_end="2026-09-14T09:12:31+00:00")
    make_report(reports_dir, 95, 1789117951000, later)

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.appended == 1
    assert len(odoo.tickets) == 1
    ticket_id = ticket_id_of(odoo)
    assert odoo.tickets[ticket_id]["count"] == 2
    assert odoo.tickets[ticket_id]["last_occurred"] == "2026-09-14 09:12:31"
    assert len(odoo.notes) == 1
    assert "Reported again" in odoo.notes[0][1]


def test_closed_ticket_is_not_reused(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    run_once(settings, registry, odoo, store, dry_run=False)
    odoo.close(ticket_id_of(odoo))
    make_report(reports_dir, 95, 1789117951000, LOG_ISSUE)

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.created == 1
    assert len(odoo.tickets) == 2


def test_different_issue_types_get_separate_tickets(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    make_report(reports_dir, 95, 1789117951000, ENTITIES_ISSUE)

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.created == 2
    sources = {ticket["source"] for ticket in odoo.tickets.values()}
    assert len(sources) == 2


def test_handled_report_is_not_filed_again(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    run_once(settings, registry, odoo, store, dry_run=False)

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.handled == 1
    assert result.created == 0
    assert len(odoo.tickets) == 1
    assert odoo.notes == []


def test_crash_before_recording_does_not_duplicate_the_ticket(
    settings, registry, odoo, store, reports_dir
) -> None:
    # The ticket exists in Odoo but the local record was lost.
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    run_once(settings, registry, odoo, store, dry_run=False)
    store.path.unlink()
    fresh_store = type(store)(store.path)

    result = run_once(settings, registry, odoo, fresh_store, dry_run=False)

    assert result.created == 0
    assert result.appended == 1
    assert len(odoo.tickets) == 1


def test_dry_run_writes_nothing(settings, registry, store, reports_dir) -> None:
    from conftest import FakeOdoo

    odoo = FakeOdoo(write_enabled=False)
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)

    result = run_once(settings, registry, odoo, store, dry_run=True)

    assert result.created == 1
    assert result.planned == [f"{CLIENT_ID}/datalog_94_1789114351000: create a ticket"]
    assert odoo.tickets == {}
    assert store.get(f"{CLIENT_ID}/datalog_94_1789114351000") is None


def test_report_without_an_issue_is_skipped(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, issue=None)

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.skipped == 1
    assert odoo.tickets == {}
    assert store.get(f"{CLIENT_ID}/datalog_94_1789114351000").action is Action.SKIPPED


def test_unknown_client_still_gets_a_ticket_without_a_partner(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE, client_id="unknown-site")

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.created == 1
    (ticket,) = odoo.tickets.values()
    assert "partner_id" not in ticket
    assert store.get("unknown-site/datalog_94_1789114351000").detail


def test_future_contract_version_is_refused(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE, contract_version=2)

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.unreadable == 1
    assert result.exit_code == 3
    assert odoo.tickets == {}


def test_issue_path_outside_the_report_is_refused(
    settings, registry, odoo, store, reports_dir
) -> None:
    directory = make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["issue_file"] = "../../../../etc/passwd"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.unreadable == 1
    assert odoo.tickets == {}


def test_oversized_file_is_not_attached(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE, log_bytes=8192)
    settings = settings.model_copy(update={"max_attachment_bytes": 4096})

    run_once(settings, registry, odoo, store, dry_run=False)

    assert [name for _, name, _ in odoo.attachments] == [
        "datalog_94_1789114351000-issue_description.json"
    ]


def test_missing_reports_directory_is_an_error(
    settings, registry, odoo, store, tmp_path: Path
) -> None:
    settings = settings.model_copy(update={"reports_dir": tmp_path / "absent"})

    try:
        run_once(settings, registry, odoo, store, dry_run=False)
    except Exception as e:
        assert "does not exist" in str(e)
    else:
        raise AssertionError("a missing reports directory must be reported")
