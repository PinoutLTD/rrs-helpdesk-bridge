import json
import subprocess

import pytest

from rrs_helpdesk_bridge import secrets
from rrs_helpdesk_bridge.secrets import (
    SecretUnavailableError,
    load_odoo_credentials,
)

VALUES = {
    "odoo_url": "http://127.0.0.1:8069/",
    "odoo_db": "pinout_test",
    "odoo_username": "rrs-bridge-test",
    "odoo_api_key": "key-value",
}
SHARE_LIST = json.dumps(
    {"shares": [{"id": "item-share-id", "name": "Odoo API Test", "share_type": "Item"}]}
)


class FakePassCli:
    """A pass-cli that can be told whether the vault is visible."""

    def __init__(self, vault_visible: bool, shares: str = SHARE_LIST) -> None:
        self.vault_visible = vault_visible
        self.shares = shares
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[1] == "share":
            return subprocess.CompletedProcess(command, 0, self.shares, "")
        if "--vault-name" in command and not self.vault_visible:
            return subprocess.CompletedProcess(
                command, 1, "", "Could not find vault Report Service"
            )
        field = command[command.index("--field") + 1]
        return subprocess.CompletedProcess(command, 0, VALUES[field] + "\n", "")


def install(monkeypatch, fake: FakePassCli) -> FakePassCli:
    monkeypatch.setattr(secrets.subprocess, "run", fake)
    return fake


def test_credentials_are_read_from_a_visible_vault(monkeypatch) -> None:
    fake = install(monkeypatch, FakePassCli(vault_visible=True))

    credentials = load_odoo_credentials("Report Service", "Odoo API Test")

    assert credentials.url == "http://127.0.0.1:8069"
    assert credentials.db == "pinout_test"
    assert credentials.api_key.get_secret_value() == "key-value"
    assert "key-value" not in repr(credentials)
    # No share listing is needed when the vault itself is reachable.
    assert all(call[1] != "share" for call in fake.calls)


def test_item_granted_token_reads_through_its_own_share(monkeypatch) -> None:
    fake = install(monkeypatch, FakePassCli(vault_visible=False))

    credentials = load_odoo_credentials("Report Service", "Odoo API Test")

    assert credentials.db == "pinout_test"
    # The share is resolved once, then reused for the remaining fields.
    assert sum(call[1] == "share" for call in fake.calls) == 1
    assert all(
        "--share-id" in call
        for call in fake.calls
        if call[1] == "item" and call is not fake.calls[0]
    )


def test_token_without_access_is_reported(monkeypatch) -> None:
    install(
        monkeypatch,
        FakePassCli(vault_visible=False, shares=json.dumps({"shares": []})),
    )

    with pytest.raises(SecretUnavailableError, match="access to the item"):
        load_odoo_credentials("Report Service", "Odoo API Test")


def test_missing_pass_cli_is_reported(monkeypatch) -> None:
    def missing(command, **kwargs):
        raise FileNotFoundError("pass-cli")

    monkeypatch.setattr(secrets.subprocess, "run", missing)

    with pytest.raises(SecretUnavailableError, match="not installed"):
        load_odoo_credentials("Report Service", "Odoo API Test")
