import pytest

from rrs_helpdesk_bridge.odoo_client import (
    QUIET_CONTEXT,
    OdooClient,
    WriteDisabledError,
)


class RecordingClient(OdooClient):
    """An OdooClient with the XML-RPC connection replaced by a recorder."""

    def __init__(self, write_enabled: bool) -> None:
        self.write_enabled = write_enabled
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


def test_notes_are_internal() -> None:
    client = RecordingClient(write_enabled=True)

    client.post_note(5, "<p>again</p>")

    _, method, args, kwargs = client.calls[0]
    assert method == "message_post"
    assert kwargs["subtype_xmlid"] == "mail.mt_note"
    assert kwargs["context"] == QUIET_CONTEXT


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
