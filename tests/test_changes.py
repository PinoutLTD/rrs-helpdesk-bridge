"""A repeat says what changed since the ticket's last report."""

import copy

from conftest import ENTITIES_ISSUE, LOG_ISSUE, make_report

from rrs_helpdesk_bridge.changes import compare, snapshot_items
from rrs_helpdesk_bridge.dispatcher import run_once
from rrs_helpdesk_bridge.manifests import ReportIssue


def entities_issue(
    devices: dict[str, list[str]], pure: list[str] | None = None
) -> dict:
    issue = copy.deepcopy(ENTITIES_ISSUE)
    issue["details"]["unavailable_entities"] = {
        "devices": {
            f"dev{i}": {"device_name": name, "entities": entities}
            for i, (name, entities) in enumerate(devices.items())
        },
        "pure_entities": pure or [],
    }
    return issue


def log_issue(messages: list[str]) -> dict:
    issue = copy.deepcopy(LOG_ISSUE)
    issue["details"]["top_events"] = [
        {
            "level": "ERROR",
            "name": "roombapy",
            "count": 3,
            "last_seen": "",
            "message": m,
        }
        for m in messages
    ]
    return issue


def test_entities_are_compared_by_id_and_labelled_with_their_device():
    before = snapshot_items(
        ReportIssue(**entities_issue({"Roomba": ["vacuum.r", "sensor.b"]}))
    )
    after = snapshot_items(
        ReportIssue(**entities_issue({"Roomba": ["vacuum.r"], "Lamp": ["light.l"]}))
    )

    changes = compare(before, after)

    assert changes.new == {"light.l": "Lamp"}
    assert changes.gone == {"sensor.b": "Roomba"}
    assert changes.unchanged == 1


def test_numbers_in_a_log_message_do_not_make_it_new():
    before = snapshot_items(ReportIssue(**log_issue(["Can't connect to 192.168.1.83"])))
    after = snapshot_items(ReportIssue(**log_issue(["Can't connect to 192.168.1.84"])))

    assert compare(before, after).none


def test_other_issue_types_are_not_compared():
    assert snapshot_items(ReportIssue(type="host_health", details={})) is None
    assert snapshot_items(None) is None


def run(settings, registry, odoo, store):
    return run_once(settings, registry, odoo, store, dry_run=False)


def test_a_repeat_lists_only_what_changed(settings, registry, odoo, store, reports_dir):
    make_report(
        reports_dir,
        94,
        1789114351000,
        entities_issue({"Roomba": ["vacuum.r", "sensor.b"]}),
    )
    run(settings, registry, odoo, store)
    make_report(
        reports_dir,
        95,
        1789117951000,
        entities_issue(
            {"Roomba": ["vacuum.r"], "Lamp": ["light.l"]}, ["sensor.orphan"]
        ),
    )

    run(settings, registry, odoo, store)

    (note,) = [text for _, text in odoo.notes]
    assert "Since the last report" in note
    assert "Newly unavailable (2)" in note and "Lamp: light.l" in note
    assert "sensor.orphan" in note
    assert "Back to normal (1)" in note and "Roomba: sensor.b" in note
    assert "Still unavailable: 1, as before." in note
    # The full list is not repeated: vacuum.r appears only in the count.
    assert "vacuum.r" not in note


def test_an_unchanged_repeat_is_one_line(settings, registry, odoo, store, reports_dir):
    same = entities_issue({"Roomba": ["vacuum.r", "sensor.b"]})
    make_report(reports_dir, 94, 1789114351000, same)
    run(settings, registry, odoo, store)
    make_report(reports_dir, 95, 1789117951000, same)

    run(settings, registry, odoo, store)

    (note,) = [text for _, text in odoo.notes]
    assert "No change since the last report</b>: 2 unavailable" in note
    assert "vacuum.r" not in note


def test_a_ticket_without_a_snapshot_gets_the_full_list_once(
    settings, registry, odoo, store, reports_dir
):
    make_report(
        reports_dir, 94, 1789114351000, entities_issue({"Roomba": ["vacuum.r"]})
    )
    run(settings, registry, odoo, store)
    ticket_id = next(iter(odoo.tickets))
    # As a ticket opened before snapshots existed.
    with store._connect() as connection:
        connection.execute("DELETE FROM ticket_snapshots")
    make_report(
        reports_dir, 95, 1789117951000, entities_issue({"Roomba": ["vacuum.r"]})
    )
    make_report(
        reports_dir, 96, 1789121551000, entities_issue({"Roomba": ["vacuum.r"]})
    )

    run(settings, registry, odoo, store)

    first, second = [text for _, text in odoo.notes]
    assert "vacuum.r" in first and "Since the last report" not in first
    assert "No change since the last report" in second
    assert store.snapshot(ticket_id) == {"vacuum.r": "Roomba"}


def test_log_repeats_are_compared_too(settings, registry, odoo, store, reports_dir):
    make_report(
        reports_dir, 94, 1789114351000, log_issue(["Can't connect to 10.0.0.1"])
    )
    run(settings, registry, odoo, store)
    make_report(
        reports_dir,
        95,
        1789117951000,
        log_issue(["Can't connect to 10.0.0.2", "Unknown disconnection error: ID=16"]),
    )

    run(settings, registry, odoo, store)

    (note,) = [text for _, text in odoo.notes]
    assert "New messages (1)" in note and "Unknown disconnection error" in note
    assert "Seen again: 1, as before." in note


def test_a_dry_run_keeps_no_snapshot(settings, registry, store, reports_dir):
    from conftest import FakeOdoo

    make_report(
        reports_dir, 94, 1789114351000, entities_issue({"Roomba": ["vacuum.r"]})
    )

    run_once(settings, registry, FakeOdoo(write_enabled=False), store, dry_run=True)

    with store._connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM ticket_snapshots").fetchone()[0]
            == 0
        )
