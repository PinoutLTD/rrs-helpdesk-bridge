import argparse
import logging
import sys

from rrs_helpdesk_bridge.config import ClientRegistry, EnvSettings, load_settings
from rrs_helpdesk_bridge.dispatcher import run_once
from rrs_helpdesk_bridge.logging_config import setup_logging
from rrs_helpdesk_bridge.manifests import ManifestError, find_manifests
from rrs_helpdesk_bridge.odoo_client import OdooClient
from rrs_helpdesk_bridge.secrets import load_odoo_credentials
from rrs_helpdesk_bridge.state import StateStore

LOGGER = logging.getLogger(__name__)


def connect(settings: EnvSettings, write_enabled: bool) -> OdooClient:
    vault, item = settings.secret_location
    LOGGER.info(
        "Profile %s: Odoo credentials from Proton Pass vault '%s', item '%s'",
        settings.profile,
        vault,
        item,
    )
    return OdooClient(
        load_odoo_credentials(vault, item),
        write_enabled,
        note_email_from=settings.note_email_from,
    )


def odoo_check(settings: EnvSettings, registry: ClientRegistry) -> int:
    odoo = connect(settings, write_enabled=False)
    LOGGER.info(
        "Connected to %s (db %s) as uid %d, server %s",
        odoo.url,
        odoo.db,
        odoo.uid,
        odoo.server_version(),
    )

    for stage in odoo.list_stages():
        mark = " [closing]" if stage.get("closed") else ""
        selected = (
            " <- tickets are created here" if stage["id"] == settings.stage_id else ""
        )
        LOGGER.info("Stage %s: %s%s%s", stage["id"], stage["name"], mark, selected)
    for channel in odoo.list_channels():
        selected = " <- used" if channel["id"] == settings.channel_id else ""
        LOGGER.info("Channel %s: %s%s", channel["id"], channel["name"], selected)

    for client in registry.clients:
        partner = odoo.read_partner(client.odoo_partner_id)
        if partner is None:
            LOGGER.error(
                "Client %s: partner %d not found in Odoo",
                client.client_id,
                client.odoo_partner_id,
            )
        else:
            LOGGER.info(
                "Client %s -> partner %d (%s)",
                client.client_id,
                partner["id"],
                partner["name"],
            )

    try:
        manifests = find_manifests(settings.reports_dir)
    except ManifestError as e:
        LOGGER.error("%s", e)
        return 3
    LOGGER.info(
        "Reports directory %s: %d manifest(s)", settings.reports_dir, len(manifests)
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="File reports prepared by rrs-connector as Odoo helpdesk tickets"
    )
    parser.add_argument(
        "--command", choices=["run-once", "odoo-check"], default="run-once"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="actually write to Odoo; without it the run is a dry run",
    )
    args = parser.parse_args()

    setup_logging()
    LOGGER.info("Starting rrs-helpdesk-bridge")

    try:
        settings, registry = load_settings()
    except Exception:
        LOGGER.exception("Application startup failed")
        return 1

    # Writing to production needs both the flag and the setting, so a stray
    # --write cannot reach the live helpdesk.
    write_enabled = args.write
    if write_enabled and settings.profile == "prod" and not settings.write_enabled:
        LOGGER.error(
            "Profile prod: --write also requires RRSB_WRITE_ENABLED=true; "
            "running as a dry run instead"
        )
        write_enabled = False

    try:
        if args.command == "odoo-check":
            return odoo_check(settings, registry)

        odoo = connect(settings, write_enabled)
        store = StateStore(settings.state_db)
        result = run_once(settings, registry, odoo, store, dry_run=not write_enabled)
        return result.exit_code
    except Exception:
        LOGGER.exception("Application failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
