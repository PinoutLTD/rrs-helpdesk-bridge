"""A thin XML-RPC client for Odoo Helpdesk.

Every mutating call goes through `_write`, which refuses to run unless the
client was built with `write_enabled`. Mutations also carry a context that
keeps Odoo's mail engine quiet: the production database has a live SMTP
server, and a ticket opened by this service is an internal signal, not a
message to the client.
"""

import base64
import logging
import xmlrpc.client
from dataclasses import dataclass

from rrs_helpdesk_bridge.secrets import OdooCredentials

LOGGER = logging.getLogger(__name__)

TICKET_MODEL = "helpdesk.ticket"
ATTACHMENT_MODEL = "ir.attachment"
NOTE_SUBTYPE = "mail.mt_note"

# Suppress tracking messages, automatic followers, and creation logs.
QUIET_CONTEXT = {
    "tracking_disable": True,
    "mail_create_nosubscribe": True,
    "mail_create_nolog": True,
    "mail_notrack": True,
}


class OdooError(RuntimeError):
    pass


class WriteDisabledError(OdooError):
    """Raised when a mutating call is made in dry-run mode."""


@dataclass(frozen=True)
class TicketSummary:
    id: int
    number: str
    name: str
    count: int


class OdooClient:
    def __init__(
        self,
        credentials: OdooCredentials,
        write_enabled: bool,
        note_email_from: str = "",
    ) -> None:
        self.note_email_from = note_email_from
        self.url = credentials.url
        self.db = credentials.db
        self.username = credentials.username
        self._api_key = credentials.api_key
        self.write_enabled = write_enabled

        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        try:
            self.uid = common.authenticate(
                self.db, self.username, self._api_key.get_secret_value(), {}
            )
        except (xmlrpc.client.Error, OSError) as e:
            raise OdooError(f"Cannot reach Odoo at {self.url}: {e}") from e
        if not self.uid:
            raise OdooError(
                "Odoo rejected the credentials; check the database, user, and "
                "API key in Proton Pass"
            )
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def _call(self, model: str, method: str, *args, **kwargs):
        try:
            return self.models.execute_kw(
                self.db,
                self.uid,
                self._api_key.get_secret_value(),
                model,
                method,
                list(args),
                kwargs,
            )
        except (xmlrpc.client.Error, OSError) as e:
            raise OdooError(f"{model}.{method} failed: {e}") from e

    def _write(self, model: str, method: str, *args, **kwargs):
        if not self.write_enabled:
            raise WriteDisabledError(
                f"{model}.{method} blocked: this run is a dry run (use --write)"
            )
        context = dict(QUIET_CONTEXT)
        context.update(kwargs.pop("context", {}))
        return self._call(model, method, *args, context=context, **kwargs)

    # Reading

    def server_version(self) -> str:
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        try:
            return str(common.version().get("server_version", "unknown"))
        except (xmlrpc.client.Error, OSError) as e:
            raise OdooError(f"Cannot read the Odoo version: {e}") from e

    def list_stages(self) -> list[dict]:
        return self._call(
            "helpdesk.ticket.stage",
            "search_read",
            [],
            fields=["id", "name", "closed", "unattended"],
            order="sequence, id",
        )

    def list_channels(self) -> list[dict]:
        return self._call(
            "helpdesk.ticket.channel", "search_read", [], fields=["id", "name"]
        )

    def read_partner(self, partner_id: int) -> dict | None:
        records = self._call(
            "res.partner", "read", [partner_id], fields=["id", "name", "email"]
        )
        return records[0] if records else None

    def find_open_ticket(self, signature: str) -> TicketSummary | None:
        """The open ticket carrying this problem signature, if there is one.

        Odoo is the source of truth for deduplication: a closed ticket is
        never reused, so a problem that comes back opens a new ticket.
        """

        records = self._call(
            TICKET_MODEL,
            "search_read",
            [("source", "=", signature), ("stage_id.closed", "=", False)],
            fields=["id", "number", "name", "count"],
            order="id desc",
            limit=1,
        )
        if not records:
            return None
        record = records[0]
        return TicketSummary(
            id=record["id"],
            number=record.get("number") or "",
            name=record.get("name") or "",
            count=record.get("count") or 0,
        )

    # Writing

    def create_ticket(self, values: dict) -> int:
        return int(self._write(TICKET_MODEL, "create", [values])[0])

    def update_ticket(self, ticket_id: int, values: dict) -> None:
        self._write(TICKET_MODEL, "write", [ticket_id], values)

    def post_note(self, ticket_id: int, body: str) -> None:
        arguments = {"body": body, "subtype_xmlid": NOTE_SUBTYPE}
        if self.note_email_from:
            arguments["email_from"] = self.note_email_from
        try:
            self._write(TICKET_MODEL, "message_post", [ticket_id], **arguments)
        except OdooError as e:
            if not self.note_email_from and "email address" in str(e):
                raise OdooError(
                    f"{e}. The Odoo user behind the API key has no e-mail "
                    "address: give it one, or set RRSB_NOTE_EMAIL_FROM"
                ) from e
            raise

    def attach_file(self, ticket_id: int, name: str, data: bytes) -> int:
        """Attach a file to the ticket without posting a message about it."""

        values = {
            "name": name,
            "res_model": TICKET_MODEL,
            "res_id": ticket_id,
            "datas": base64.b64encode(data).decode("ascii"),
        }
        return int(self._write(ATTACHMENT_MODEL, "create", [values])[0])
