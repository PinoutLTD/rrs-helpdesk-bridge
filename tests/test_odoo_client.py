import pytest

from rrs_helpdesk_bridge.odoo_client import (
    QUIET_CONTEXT,
    OdooClient,
    OdooError,
    WriteDisabledError,
)

NOTE_SUBTYPE_ID = 2
CREATED_SUBTYPE_ID = 4


class RecordingClient(OdooClient):
    """An OdooClient with the XML-RPC connection replaced by a recorder."""

    def __init__(self, write_enabled: bool) -> None:
        self.write_enabled = write_enabled
        self.note_email_from = ""
        self._created_subtype_id = CREATED_SUBTYPE_ID
        self._note_subtype_id = NOTE_SUBTYPE_ID
        self.calls: list[tuple] = []
        self.result = [1]

    def _call(self, model, method, *args, **kwargs):
        self.calls.append((model, method, args, kwargs))
        return self.result


def test_dry_run_blocks_every_mutating_call() -> None:
    client = RecordingClient(write_enabled=False)

    for call in (
        lambda: client.create_ticket({"name": "x"}),
        lambda: client.update_ticket(1, {"count": 2}),
        lambda: client.post_note(1, "note"),
        lambda: client.attach_file(1, "a.log", b"data"),
    ):
        with pytest.raises(WriteDisabledError):
            call()

    assert client.calls == []


def test_writes_carry_the_quiet_mail_context() -> None:
    client = RecordingClient(write_enabled=True)

    client.create_ticket({"name": "x"})

    model, method, args, kwargs = client.calls[0]
    assert (model, method) == ("helpdesk.ticket", "create")
    assert kwargs["context"] == QUIET_CONTEXT


def test_notes_are_internal_and_keep_their_html() -> None:
    client = RecordingClient(write_enabled=True)

    client.post_note(5, "<p>again</p>")

    model, method, args, kwargs = client.calls[0]
    values = args[0][0]
    # The composer's body is an Html field: message_post would escape a plain
    # string and the markup would show up in the ticket and in the e-mail.
    assert (model, method) == ("mail.compose.message", "create")
    assert values["body"] == "<p>again</p>"
    assert values["subtype_id"] == NOTE_SUBTYPE_ID
    assert values["res_ids"] == "[5]"
    assert kwargs["context"] == QUIET_CONTEXT
    assert client.calls[1][1] == "action_send_mail"


def test_open_tickets_only_are_deduplicated() -> None:
    client = RecordingClient(write_enabled=True)
    client.result = []

    assert client.find_open_ticket("abc123") is None

    _, method, args, kwargs = client.calls[0]
    assert method == "search_read"
    assert args[0] == [("source", "=", "abc123"), ("stage_id.closed", "=", False)]


def test_attachment_is_base64_encoded() -> None:
    client = RecordingClient(write_enabled=True)

    client.attach_file(5, "a.log", b"hello")

    _, _, args, _ = client.calls[0]
    values = args[0][0]
    assert values["datas"] == "aGVsbG8="
    assert values["res_model"] == "helpdesk.ticket"
    assert values["res_id"] == 5


def test_note_sender_is_added_when_configured() -> None:
    client = RecordingClient(write_enabled=True)
    client.note_email_from = "bridge@example.com"

    client.post_note(5, "<p>again</p>")

    _, _, args, _ = client.calls[0]
    assert args[0][0]["email_from"] == "bridge@example.com"


def test_missing_sender_address_is_explained() -> None:
    class FailingClient(RecordingClient):
        def _call(self, model, method, *args, **kwargs):
            raise OdooError("Unable to send message, configure the email address")

    client = FailingClient(write_enabled=True)
    client.note_email_from = ""

    with pytest.raises(OdooError, match="RRSB_NOTE_EMAIL_FROM"):
        client.post_note(5, "<p>again</p>")


def test_only_staff_accounts_can_be_notified() -> None:
    client = RecordingClient(write_enabled=True)
    client.result = []

    client.find_internal_partners(["Colleague@Example.com"])

    _, method, args, _ = client.calls[0]
    assert method == "search_read"
    assert ("share", "=", False) in args[0]
    assert ("email", "in", ["colleague@example.com"]) in args[0]


def test_creation_message_uses_the_helpdesk_subtype() -> None:
    client = RecordingClient(write_enabled=True)

    client.announce_ticket(5, "<p>new</p>")

    _, method, args, _ = client.calls[0]
    assert method == "create"
    assert args[0][0]["subtype_id"] == CREATED_SUBTYPE_ID
    assert args[0][0]["body"] == "<p>new</p>"


def test_missing_note_subtype_is_refused() -> None:
    client = RecordingClient(write_enabled=True)
    client._note_subtype_id = None
    client.result = []

    with pytest.raises(OdooError, match="Note"):
        client.post_note(5, "<p>again</p>")


def test_subtype_lookup_is_cached() -> None:
    client = RecordingClient(write_enabled=True)
    client._created_subtype_id = None
    client.result = [{"id": 7, "name": "Ticket Created"}]

    assert client.find_created_subtype_id() == 7
    assert client.find_created_subtype_id() == 7
    assert sum(call[1] == "search_read" for call in client.calls) == 1
