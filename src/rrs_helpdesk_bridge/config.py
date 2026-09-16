"""Settings, the client registry, and the two Odoo profiles.

Nothing secret is configured here: the Odoo URL, database, user, and API key
are read from Proton Pass at runtime (see `secrets.py`).
"""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, PositiveInt, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

# The site slug shared with rrs-connector and the field engineer's repository.
CLIENT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

Profile = Literal["test", "prod"]

# Which Proton Pass item holds the credentials of each Odoo instance.
PROFILE_SECRETS: dict[str, tuple[str, str]] = {
    "test": ("Report Service", "Odoo API Test"),
    "prod": ("Smart Home Agent", "Odoo API"),
}


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE, env_file_encoding="utf-8", env_prefix="RRSB_"
    )

    profile: Profile = "test"
    # Writing to Odoo is opt-in; the CLI flag --write turns it on for one run.
    write_enabled: bool = False
    # `<RRS_DATA_DIR>/reports` of the connector, usually a read-only mount.
    reports_dir: Path
    state_db: Path
    registry_file: Path
    # Stage "New" and the channel used for tickets opened by this service.
    # Closing stages carry a customer e-mail template and are never set here.
    stage_id: PositiveInt = 1
    channel_id: PositiveInt = 4
    company_id: PositiveInt = 1
    # Odoo refuses message_post when the API user has no e-mail address: set
    # one on that user, or put a sender address here.
    note_email_from: str = ""
    attach_files: bool = True
    # Odoo stores attachments base64-encoded in the database.
    max_attachment_bytes: PositiveInt = 5 * 1024 * 1024

    @property
    def secret_location(self) -> tuple[str, str]:
        return PROFILE_SECRETS[self.profile]


class ClientConfig(BaseModel):
    client_id: str
    odoo_partner_id: PositiveInt
    description: str = ""

    @field_validator("client_id", mode="after")
    @classmethod
    def is_client_slug(cls, client_id: str) -> str:
        if not CLIENT_ID_PATTERN.match(client_id):
            raise ValueError(
                f"{client_id!r} is not a valid client_id: use the site slug in "
                "lowercase with dashes, for example 'qube-block-a-301'"
            )
        return client_id


class ClientRegistry(BaseModel):
    clients: list[ClientConfig]

    def partner_id(self, client_id: str) -> int | None:
        for client in self.clients:
            if client.client_id == client_id:
                return client.odoo_partner_id
        return None


def normalize_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def load_yaml_file(path: Path) -> dict:
    with open(path, encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if data is None:
        raise ValueError(f"YAML file is empty: {path}")
    if not isinstance(data, dict):
        raise ValueError(f"YAML file must contain a mapping at top level: {path}")

    return data


def load_settings() -> tuple[EnvSettings, ClientRegistry]:
    env_settings = EnvSettings()
    env_settings = env_settings.model_copy(
        update={
            "reports_dir": normalize_path(env_settings.reports_dir),
            "state_db": normalize_path(env_settings.state_db),
            "registry_file": normalize_path(env_settings.registry_file),
        }
    )

    registry = ClientRegistry.model_validate(load_yaml_file(env_settings.registry_file))

    return env_settings, registry
