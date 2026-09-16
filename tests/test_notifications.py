from conftest import CLIENT_ID, LOG_ISSUE, make_report

from rrs_helpdesk_bridge.config import ClientRegistry
from rrs_helpdesk_bridge.dispatcher import run_once

COLLEAGUE = "engineer@pinout.example"


def registry_notifying(*emails: str) -> ClientRegistry:
    return ClientRegistry.model_validate(
        {
            "clients": [{"client_id": CLIENT_ID, "odoo_partner_id": 7}],
            "notify_emails": list(emails),
        }
    )


def test_new_ticket_notifies_the_colleagues(settings, odoo, store, reports_dir) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)

    result = run_once(
        settings, registry_notifying(COLLEAGUE), odoo, store, dry_run=False
    )

    assert result.notified == 1
    (ticket_id, partner_ids, subtype_ids) = odoo.subscriptions[0]
    assert partner_ids == [11]
    # Subscribed to the creation subtype only, so later notes stay silent.
    assert subtype_ids == [odoo.created_subtype_id]
    assert odoo.announcements[0][0] == ticket_id
    assert CLIENT_ID in odoo.announcements[0][1]


def test_repeat_report_does_not_notify(settings, odoo, store, reports_dir) -> None:
    registry = registry_notifying(COLLEAGUE)
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    run_once(settings, registry, odoo, store, dry_run=False)
    make_report(reports_dir, 95, 1789117951000, LOG_ISSUE)

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.appended == 1
    assert result.notified == 0
    assert len(odoo.subscriptions) == 1
    assert len(odoo.announcements) == 1


def test_client_address_is_never_subscribed(settings, odoo, store, reports_dir) -> None:
    # The client's address belongs to no staff account, so it resolves to
    # nobody and the ticket is filed without notifying anyone.
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)

    result = run_once(
        settings,
        registry_notifying("client@example.com"),
        odoo,
        store,
        dry_run=False,
    )

    assert result.created == 1
    assert result.notified == 0
    assert odoo.subscriptions == []


def test_missing_created_subtype_blocks_subscription(
    settings, odoo, store, reports_dir
) -> None:
    odoo.created_subtype_id = None
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)

    result = run_once(
        settings, registry_notifying(COLLEAGUE), odoo, store, dry_run=False
    )

    assert result.created == 1
    assert result.notified == 0
    assert odoo.subscriptions == []


def test_nobody_is_notified_without_addresses(
    settings, registry, odoo, store, reports_dir
) -> None:
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)

    result = run_once(settings, registry, odoo, store, dry_run=False)

    assert result.created == 1
    assert result.notified == 0
    assert odoo.announcements == []
