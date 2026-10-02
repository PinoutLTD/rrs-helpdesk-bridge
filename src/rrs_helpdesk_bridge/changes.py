"""What changed since the ticket's last report.

A site reports the same problem every day, and every repeat used to carry the
whole list again: a ticket with weeks of daily repeats is a wall of identical
lists, and what moved has to be found by eye. A repeat now says only what
changed — new, gone, how much stayed — and the ticket's first report keeps
the full list.

The comparison is against a snapshot the bridge keeps per ticket: the items of
the last report it filed there. A ticket opened before snapshots existed gets
the full list once more, which becomes its first snapshot.
"""

import re
from dataclasses import dataclass

from rrs_helpdesk_bridge.manifests import ReportIssue

ENTITIES = "entities_health_problems"
LOGS = "accumulated_system_log_problems"
COMPARED_TYPES = (ENTITIES, LOGS)

# Numbers in a log message (addresses, counters, durations) differ from one
# day to the next; the message is the same problem.
NUMBERS = re.compile(r"\d+")
MAX_MESSAGE_KEY = 160


@dataclass(frozen=True)
class Changes:
    """New and gone items, each `key → label`, and how many stayed."""

    new: dict[str, str]
    gone: dict[str, str]
    unchanged: int

    @property
    def none(self) -> bool:
        return not self.new and not self.gone


def log_key(event: dict) -> str:
    message = NUMBERS.sub("#", str(event.get("message", "")))[:MAX_MESSAGE_KEY]
    return f"{event.get('level', '')} {event.get('name', '')}: {message}"


def snapshot_items(issue: ReportIssue | None) -> dict[str, str] | None:
    """The comparable items of an issue, `key → label`; None for other types.

    Entities: the entity id, labelled with its device. Logs: the level, logger
    and message with numbers masked, labelled with the message as it was.
    """

    if issue is None or issue.type not in COMPARED_TYPES:
        return None
    details = issue.details or {}
    items: dict[str, str] = {}
    if issue.type == ENTITIES:
        unavailable = details.get("unavailable_entities") or {}
        if not isinstance(unavailable, dict):
            return items
        for device in (unavailable.get("devices") or {}).values():
            if not isinstance(device, dict):
                continue
            name = str(device.get("device_name") or "Unknown device")
            for entity in device.get("entities") or []:
                items[str(entity)] = name
        for entity in unavailable.get("pure_entities") or []:
            items[str(entity)] = ""
    else:
        for event in details.get("top_events") or []:
            if isinstance(event, dict):
                items[log_key(event)] = str(event.get("message", ""))
    return items


def compare(previous: dict[str, str], current: dict[str, str]) -> Changes:
    return Changes(
        new={key: label for key, label in current.items() if key not in previous},
        gone={key: label for key, label in previous.items() if key not in current},
        unchanged=sum(1 for key in current if key in previous),
    )
