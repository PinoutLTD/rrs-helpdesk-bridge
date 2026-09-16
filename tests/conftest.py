import json
from pathlib import Path

import pytest

from rrs_helpdesk_bridge.config import ClientRegistry, EnvSettings
from rrs_helpdesk_bridge.odoo_client import TicketSummary, WriteDisabledError
from rrs_helpdesk_bridge.state import StateStore

CLIENT_ID = "qube-block-a-301"
SENDER_ADDRESS = "4GRQ4FJABmthGeggRa5CCbBEY5WhnNzKcopjf2kfBmyUcyRk"
PARTNER_ID = 7

LOG_ISSUE = {
    "type": "accumulated_system_log_problems",
    "email": "client@example.com",
    "schema_version": 1,
    "ts_start": "2026-09-14T07:12:31+00:00",
    "ts_end": "2026-09-14T08:12:31+00:00",
    "summary": "System log: 2 unique issues, 12 occurrences (last 60 min)",
    "details": {
        "logs_timeout_minutes": 60,
        "total_events": 12,
        "unique_events": 2,
        "by_level_events": {"ERROR": 10, "WARNING": 2},
        "top_events": [
            {
                "signature": "a1b2c3d4",
                "count": 10,
                "first_seen": "2026-09-14T07:20:00+00:00",
                "last_seen": "2026-09-14T08:10:00+00:00",
                "level": "ERROR",
                "name": "homeassistant.components.zha",
                "source": ["zha/core.py", 120],
                "message": "Failed to connect to coordinator",
            }
        ],
    },
}

ENTITIES_ISSUE = {
    "type": "entities_health_problems",
    "schema_version": 1,
    "ts_start": "2026-09-14T07:00:00+00:00",
    "ts_end": "2026-09-14T08:00:00+00:00",
    "summary": "Entities health: 3 unavailable (1 devices) (interval 60 min)",
    "details": {
        "counts": {"unavailable_counts": {"devices": 1, "entities": 3}},
        "unavailable_entities": {
            "devices": {
                "dev1": {
                    "device_name": "Kitchen sensor",
                    "entities": ["sensor.kitchen_temp", "sensor.kitchen_hum"],
                }
            },
            "pure_entities": ["light.hall"],
        },
    },
}


def make_report(
    reports_dir: Path,
    index: int,
    timestamp_ms: int,
    issue: dict | None = None,
    client_id: str = CLIENT_ID,
    contract_version: int = 1,
    log_bytes: int = 64,
    issue_file: str | None = "decrypted/issue_description.json",
) -> Path:
    """Write a report directory exactly as rrs-connector leaves it."""

    directory = reports_dir / client_id / f"datalog_{index}_{timestamp_ms}"
    decrypted = directory / "decrypted"
    decrypted.mkdir(parents=True)
    (directory / "archive.zip").write_bytes(b"encrypted archive")
    (decrypted / "home-assistant.log").write_bytes(b"x" * log_bytes)

    files = [
        {
            "name": "home-assistant.log",
            "path": "decrypted/home-assistant.log",
            "size_bytes": log_bytes,
        }
    ]
    if issue is not None:
        payload = json.dumps(issue, ensure_ascii=False).encode("utf-8")
        (decrypted / "issue_description.json").write_bytes(payload)
        files.append(
            {
                "name": "issue_description.json",
                "path": "decrypted/issue_description.json",
                "size_bytes": len(payload),
            }
        )

    timestamp = f"2026-09-14T08:12:{index % 60:02d}+00:00"
    manifest = {
        "contract_version": contract_version,
        "report_id": f"{client_id}/{directory.name}",
        "client_id": client_id,
        "sender_address": SENDER_ADDRESS,
        "datalog_index": index,
        "datalog_timestamp": timestamp,
        "cid": f"QmWue3YfuZvuRvgcNb4vZuheX9TaZ9E1b8aCdxSoaGTbV{index % 10}",
        "processed_at": timestamp,
        "archive": {"path": "archive.zip", "size_bytes": 17},
        "decrypted_dir": "decrypted",
        "issue_file": issue_file if issue is not None else None,
        "files": sorted(files, key=lambda file: file["name"]),
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return directory


class FakeOdoo:
    """Records every call and enforces the same dry-run guard as the real client."""

    def __init__(self, write_enabled: bool = True) -> None:
        self.write_enabled = write_enabled
        # Only staff accounts exist here, as in Odoo with share = False.
        self.internal_users = {"engineer@pinout.example": 11}
        self.created_subtype_id: int | None = 4
        self.subscriptions: list[tuple[int, list[int], list[int]]] = []
        self.announcements: list[tuple[int, str]] = []
        self.tickets: dict[int, dict] = {}
        self.open_by_signature: dict[str, int] = {}
        self.notes: list[tuple[int, str]] = []
        self.attachments: list[tuple[int, str, int]] = []
        self.updates: list[tuple[int, dict]] = []
        self._next_id = 100

    def _guard(self, method: str) -> None:
        if not self.write_enabled:
            raise WriteDisabledError(f"{method} blocked: this run is a dry run")

    def find_internal_partners(self, emails: list[str]) -> dict[str, int]:
        wanted = [email.strip().lower() for email in emails]
        return {
            email: partner
            for email, partner in self.internal_users.items()
            if email in wanted
        }

    def find_created_subtype_id(self) -> int | None:
        return self.created_subtype_id

    def subscribe(
        self, ticket_id: int, partner_ids: list[int], subtype_ids: list[int]
    ) -> None:
        self._guard("message_subscribe")
        self.subscriptions.append((ticket_id, partner_ids, subtype_ids))

    def announce_ticket(self, ticket_id: int, body: str) -> None:
        self._guard("message_post")
        self.announcements.append((ticket_id, body))

    def find_open_ticket(self, signature: str) -> TicketSummary | None:
        ticket_id = self.open_by_signature.get(signature)
        if ticket_id is None:
            return None
        ticket = self.tickets[ticket_id]
        return TicketSummary(
            id=ticket_id,
            number=f"T{ticket_id}",
            name=ticket["name"],
            count=ticket.get("count", 0),
        )

    def create_ticket(self, values: dict) -> int:
        self._guard("create")
        self._next_id += 1
        self.tickets[self._next_id] = dict(values)
        self.open_by_signature[values["source"]] = self._next_id
        return self._next_id

    def update_ticket(self, ticket_id: int, values: dict) -> None:
        self._guard("write")
        self.tickets[ticket_id].update(values)
        self.updates.append((ticket_id, dict(values)))

    def post_note(self, ticket_id: int, body: str) -> None:
        self._guard("message_post")
        self.notes.append((ticket_id, body))

    def attach_file(self, ticket_id: int, name: str, data: bytes) -> int:
        self._guard("attachment")
        self.attachments.append((ticket_id, name, len(data)))
        return len(self.attachments)

    def close(self, ticket_id: int) -> None:
        """Simulate a human closing the ticket in Odoo."""

        signature = self.tickets[ticket_id]["source"]
        self.open_by_signature.pop(signature, None)


@pytest.fixture
def reports_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "connector-data" / "reports"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def settings(tmp_path: Path, reports_dir: Path) -> EnvSettings:
    return EnvSettings(
        _env_file=None,
        profile="test",
        reports_dir=reports_dir,
        state_db=tmp_path / "bridge" / "state.sqlite3",
        registry_file=tmp_path / "registry.yaml",
    )


@pytest.fixture
def registry() -> ClientRegistry:
    return ClientRegistry.model_validate(
        {
            "clients": [
                {
                    "client_id": CLIENT_ID,
                    "odoo_partner_id": PARTNER_ID,
                    "description": "Test Client A",
                }
            ]
        }
    )


@pytest.fixture
def store(settings: EnvSettings) -> StateStore:
    return StateStore(settings.state_db)


@pytest.fixture
def odoo() -> FakeOdoo:
    return FakeOdoo()
