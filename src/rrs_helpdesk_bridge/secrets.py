"""Odoo credentials from Proton Pass via pass-cli; kept in memory only."""

import os
import subprocess
from dataclasses import dataclass

from pydantic import SecretStr

FIELDS = ("odoo_url", "odoo_db", "odoo_username", "odoo_api_key")


class SecretUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class OdooCredentials:
    url: str
    db: str
    username: str
    api_key: SecretStr


def read_pass_field(vault: str, item_title: str, field: str, reason: str) -> str:
    env = os.environ.copy()
    # Required by pass-cli when running under an agent token.
    env.setdefault("PROTON_PASS_AGENT_REASON", reason)
    try:
        result = subprocess.run(
            [
                "pass-cli",
                "item",
                "view",
                "--vault-name",
                vault,
                "--item-title",
                item_title,
                "--field",
                field,
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except FileNotFoundError as e:
        raise SecretUnavailableError("pass-cli is not installed") from e

    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise SecretUnavailableError(
            f"Cannot read field '{field}' of item '{item_title}' in vault "
            f"'{vault}' (pass-cli exit {result.returncode}); check `pass-cli login`"
        )
    return value


def load_odoo_credentials(vault: str, item_title: str) -> OdooCredentials:
    values = {
        field: read_pass_field(
            vault, item_title, field, reason="Create Odoo helpdesk tickets"
        )
        for field in FIELDS
    }
    return OdooCredentials(
        url=values["odoo_url"].rstrip("/"),
        db=values["odoo_db"],
        username=values["odoo_username"],
        api_key=SecretStr(values["odoo_api_key"]),
    )
