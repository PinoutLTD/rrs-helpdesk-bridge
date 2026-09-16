"""Odoo credentials from Proton Pass via pass-cli; kept in memory only."""

import json
import os
import subprocess
from dataclasses import dataclass

from pydantic import SecretStr

FIELDS = ("odoo_url", "odoo_db", "odoo_username", "odoo_api_key")
REASON = "Create Odoo helpdesk tickets"


class SecretUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class OdooCredentials:
    url: str
    db: str
    username: str
    api_key: SecretStr


def run_pass_cli(arguments: list[str], reason: str = REASON):
    env = os.environ.copy()
    # Required by pass-cli when running under an agent token.
    env.setdefault("PROTON_PASS_AGENT_REASON", reason)
    try:
        return subprocess.run(
            ["pass-cli", *arguments],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except FileNotFoundError as e:
        raise SecretUnavailableError("pass-cli is not installed") from e


def item_share_id(item_title: str) -> str | None:
    """The item's own share id, for a token granted just that one item.

    An agent token with item-level access cannot see the vault the item lives
    in, so the vault name is not an address it can use.
    """

    result = run_pass_cli(["share", "list", "--output", "json"])
    if result.returncode != 0:
        return None
    try:
        shares = json.loads(result.stdout)["shares"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    for share in shares:
        if share.get("share_type") == "Item" and share.get("name") == item_title:
            return share.get("id")
    return None


class PassItem:
    """One Proton Pass item, read field by field through the same address."""

    def __init__(self, vault: str, title: str) -> None:
        self.vault = vault
        self.title = title
        self._address = ["--vault-name", vault]
        self._address_checked = False

    def _view(self, address: list[str], field: str) -> str:
        result = run_pass_cli(
            ["item", "view", *address, "--item-title", self.title, "--field", field]
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def field(self, name: str) -> str:
        value = self._view(self._address, name)

        if not value and not self._address_checked:
            share_id = item_share_id(self.title)
            if share_id:
                self._address = ["--share-id", share_id]
                value = self._view(self._address, name)
        self._address_checked = True

        if not value:
            raise SecretUnavailableError(
                f"Cannot read field '{name}' of item '{self.title}' in vault "
                f"'{self.vault}'; check `pass-cli login` and that the token has "
                "access to the item"
            )
        return value


def load_odoo_credentials(vault: str, item_title: str) -> OdooCredentials:
    item = PassItem(vault, item_title)
    values = {field: item.field(field) for field in FIELDS}
    return OdooCredentials(
        url=values["odoo_url"].rstrip("/"),
        db=values["odoo_db"],
        username=values["odoo_username"],
        api_key=SecretStr(values["odoo_api_key"]),
    )
