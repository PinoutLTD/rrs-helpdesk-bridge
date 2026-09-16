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
# Messages are posted through the composer wizard, not message_post: over
# XML-RPC a body can only be a plain string, and message_post escapes those,
# so the HTML arrived in the ticket and in the e-mail as visible markup. The
# composer's body is an Html field and is stored as written.
COMPOSER_MODEL = "mail.compose.message"
SUBTYPE_MODEL = "mail.message.subtype"
# The subtype helpdesk_mgmt posts on ticket creation. Followers are subscribed
# to this one only, so repeat notes stay silent.
CREATED_SUBTYPE_NAME = "created"
NOTE_SUBTYPE_NAME = "note"

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
        self._created_subtype_id: int | None = None
        self._note_subtype_id: int | None = None
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

    def find_internal_partners(self, emails: list[str]) -> dict[str, int]:
        """Map each address to the partner of an internal Odoo user.

        Portal and public users are excluded by `share = False`, so only
        colleagues with a staff account can ever be notified.
        """

        if not emails:
            return {}
        wanted = [email.strip().lower() for email in emails if email.strip()]
        users = self._call(
            "res.users",
            "search_read",
            [
                "&",
                ("share", "=", False),
                "|",
                ("login", "in", wanted),
                ("email", "in", wanted),
            ],
            fields=["login", "email", "partner_id"],
        )
        found: dict[str, int] = {}
        for user in users:
            for value in (user.get("email"), user.get("login")):
                if isinstance(value, str) and value.lower() in wanted:
                    found[value.lower()] = user["partner_id"][0]
        return found

    def find_created_subtype_id(self) -> int | None:
        """The "Ticket Created" subtype, looked up by model and name."""

        if self._created_subtype_id is None:
            self._created_subtype_id = self._find_subtype_id(
                TICKET_MODEL, CREATED_SUBTYPE_NAME
            )
        return self._created_subtype_id

    def find_note_subtype_id(self) -> int | None:
        """The internal "Note" subtype, which notifies nobody by itself."""

        if self._note_subtype_id is None:
            self._note_subtype_id = self._find_subtype_id(False, NOTE_SUBTYPE_NAME)
        return self._note_subtype_id

    def _find_subtype_id(self, res_model: str | bool, name: str) -> int | None:
        subtypes = self._call(
            SUBTYPE_MODEL,
            "search_read",
            [("res_model", "=", res_model)],
            fields=["id", "name"],
        )
        for subtype in subtypes:
            if name in str(subtype.get("name", "")).lower():
                return subtype["id"]
        return None

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
        """Add an internal note; followers of other subtypes stay undisturbed."""

        subtype_id = self.find_note_subtype_id()
        if subtype_id is None:
            raise OdooError("The internal 'Note' subtype was not found")
        self._post_html(ticket_id, body, subtype_id)

    def _post_html(self, ticket_id: int, body: str, subtype_id: int) -> None:
        values = {
            "model": TICKET_MODEL,
            "res_ids": f"[{ticket_id}]",
            "body": body,
            "subtype_id": subtype_id,
            "composition_mode": "comment",
            "message_type": "comment",
        }
        if self.note_email_from:
            values["email_from"] = self.note_email_from
        try:
            composer = self._write(COMPOSER_MODEL, "create", [values])
            composer_id = composer[0] if isinstance(composer, list) else composer
            self._write(COMPOSER_MODEL, "action_send_mail", [composer_id])
        except OdooError as e:
            if not self.note_email_from and "email address" in str(e):
                raise OdooError(
                    f"{e}. The Odoo user behind the API key has no e-mail "
                    "address: give it one, or set RRSB_NOTE_EMAIL_FROM"
                ) from e
            raise

    def subscribe(
        self, ticket_id: int, partner_ids: list[int], subtype_ids: list[int]
    ) -> None:
        self._write(
            TICKET_MODEL,
            "message_subscribe",
            [ticket_id],
            partner_ids=partner_ids,
            subtype_ids=subtype_ids,
        )

    def announce_ticket(self, ticket_id: int, body: str) -> None:
        """Post the creation message that notifies the subscribed colleagues."""

        subtype_id = self.find_created_subtype_id()
        if subtype_id is None:
            raise OdooError("The 'Ticket Created' subtype was not found")
        self._post_html(ticket_id, body, subtype_id)

    def attach_file(self, ticket_id: int, name: str, data: bytes) -> int:
        """Attach a file to the ticket without posting a message about it."""

        values = {
            "name": name,
            "res_model": TICKET_MODEL,
            "res_id": ticket_id,
            "datas": base64.b64encode(data).decode("ascii"),
        }
        return int(self._write(ATTACHMENT_MODEL, "create", [values])[0])
