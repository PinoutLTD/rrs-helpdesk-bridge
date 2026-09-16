"""Reading the connector's file contract.

`manifest.json` in a report directory means the report is complete (see
rrs-connector's README, "Contract with the admin layer"). This module only
reads: it never writes into the connector's data directory.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

LOGGER = logging.getLogger(__name__)

SUPPORTED_CONTRACT_VERSION = 1
MANIFEST_FILE_NAME = "manifest.json"
# Read limits for files produced on a client's machine.
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ISSUE_BYTES = 8 * 1024 * 1024


class ManifestError(RuntimeError):
    pass


class ManifestFile(BaseModel):
    name: str
    path: str
    size_bytes: int


class ReportManifest(BaseModel):
    # Unknown fields are allowed: a later contract version may add some.
    model_config = ConfigDict(extra="allow")

    contract_version: int
    report_id: str
    client_id: str
    sender_address: str
    datalog_index: int
    datalog_timestamp: datetime
    cid: str
    processed_at: datetime
    issue_file: str | None = None
    files: list[ManifestFile] = []


class ReportIssue(BaseModel):
    """The issue built by rrs-ha-integration; fields are best-effort."""

    model_config = ConfigDict(extra="allow")

    type: str = "unknown"
    schema_version: int | None = None
    ts_start: str | None = None
    ts_end: str | None = None
    summary: str | None = None
    details: dict = {}


class Report(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    manifest: ReportManifest
    directory: Path
    issue: ReportIssue | None


def read_json(path: Path, max_bytes: int) -> dict:
    size = path.stat().st_size
    if size > max_bytes:
        raise ManifestError(f"{path.name}: {size} bytes exceeds {max_bytes}")
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ManifestError(f"{path.name}: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"{path.name}: expected a JSON object")
    return data


def resolve_inside(directory: Path, relative_path: str) -> Path:
    """Resolve a manifest path, refusing anything outside the report."""

    path = (directory / relative_path).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ManifestError(f"path escapes the report directory: {relative_path!r}")
    return path


def load_report(manifest_path: Path) -> Report:
    directory = manifest_path.parent
    try:
        manifest = ReportManifest.model_validate(
            read_json(manifest_path, MAX_MANIFEST_BYTES)
        )
    except ValidationError as e:
        raise ManifestError(f"{manifest_path.name}: {e}") from e

    if manifest.contract_version != SUPPORTED_CONTRACT_VERSION:
        raise ManifestError(
            f"unsupported contract_version {manifest.contract_version}, "
            f"this bridge reads version {SUPPORTED_CONTRACT_VERSION}"
        )

    issue = None
    if manifest.issue_file:
        issue_path = resolve_inside(directory, manifest.issue_file)
        issue = ReportIssue.model_validate(read_json(issue_path, MAX_ISSUE_BYTES))

    return Report(manifest=manifest, directory=directory, issue=issue)


def find_manifests(reports_dir: Path) -> list[Path]:
    """Every `<client_id>/<report>/manifest.json`, oldest report first."""

    if not reports_dir.exists():
        raise ManifestError(f"reports directory does not exist: {reports_dir}")
    return sorted(reports_dir.glob(f"*/*/{MANIFEST_FILE_NAME}"))
