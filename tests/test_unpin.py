"""Unpinning: only our own reports, only when their ticket is closed or old."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ENTITIES_ISSUE, LOG_ISSUE, make_report

from rrs_helpdesk_bridge.dispatcher import run_once
from rrs_helpdesk_bridge.unpin import REASON_AGE, REASON_CLOSED, UnpinError

# make_report stamps records on 2026-09-14.
SOON = datetime(2026, 9, 20, tzinfo=UTC)
LATER = datetime(2026, 9, 14, 8, 30, tzinfo=UTC) + timedelta(days=183)


class FakePinata:
    def __init__(self, gone: set[str] = frozenset(), fails: bool = False) -> None:
        self.unpinned: list[str] = []
        self.gone = set(gone)
        self.fails = fails
        self.made = 0

    def __call__(self):
        self.made += 1
        return self

    def unpin(self, cid: str) -> bool:
        if self.fails:
            raise UnpinError("Pinata answered 500")
        self.unpinned.append(cid)
        return cid not in self.gone


@pytest.fixture
def enabled(settings):
    return settings.model_copy(update={"unpin_enabled": True})


def cid_of(directory) -> str:
    return json.loads((directory / "manifest.json").read_text())["cid"]


def ticket_of(odoo) -> int:
    return next(iter(odoo.tickets))


def test_an_open_recent_ticket_keeps_its_files(
    enabled, registry, odoo, store, reports_dir
):
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    pinata = FakePinata()

    run_once(enabled, registry, odoo, store, dry_run=False, unpinner=pinata, now=SOON)

    assert pinata.unpinned == [] and pinata.made == 0


def test_closing_the_ticket_unpins_every_report_filed_to_it(
    enabled, registry, odoo, store, reports_dir
):
    first = make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    second = make_report(reports_dir, 95, 1789117951000, LOG_ISSUE)
    run_once(enabled, registry, odoo, store, dry_run=False, now=SOON)
    odoo.close(ticket_of(odoo))
    pinata = FakePinata()

    result = run_once(
        enabled, registry, odoo, store, dry_run=False, unpinner=pinata, now=SOON
    )

    assert sorted(pinata.unpinned) == sorted([cid_of(first), cid_of(second)])
    assert result.unpinned == 2

    # Recorded: the next run does not unpin them again.
    again = FakePinata()
    run_once(enabled, registry, odoo, store, dry_run=False, unpinner=again, now=SOON)
    assert again.unpinned == []


def test_after_six_months_even_an_open_ticket_lets_go(
    enabled, registry, odoo, store, reports_dir
):
    kept = make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    logs_only = make_report(reports_dir, 96, 1789114352000, None)  # no ticket at all
    run_once(enabled, registry, odoo, store, dry_run=False, now=SOON)
    pinata = FakePinata()

    run_once(enabled, registry, odoo, store, dry_run=False, unpinner=pinata, now=LATER)

    assert sorted(pinata.unpinned) == sorted([cid_of(kept), cid_of(logs_only)])


def test_only_cids_from_our_manifests_are_touched(
    enabled, registry, odoo, store, reports_dir
):
    # Another project's files live in the same Pinata account: nothing that
    # did not come from a report manifest may ever reach the unpinner.
    ours = {
        cid_of(make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)),
        cid_of(make_report(reports_dir, 97, 1789114353000, ENTITIES_ISSUE)),
    }
    run_once(enabled, registry, odoo, store, dry_run=False, now=SOON)
    pinata = FakePinata()

    run_once(enabled, registry, odoo, store, dry_run=False, unpinner=pinata, now=LATER)

    assert set(pinata.unpinned) == ours


def test_without_the_switch_a_run_only_says_what_it_would_unpin(
    settings, registry, odoo, store, reports_dir
):
    make_report(reports_dir, 94, 1789114351000, LOG_ISSUE)
    run_once(settings, registry, odoo, store, dry_run=False, now=SOON)
    odoo.close(ticket_of(odoo))
    pinata = FakePinata()

    result = run_once(
        settings, registry, odoo, store, dry_run=False, unpinner=pinata, now=SOON
    )

    assert pinata.made == 0
    assert len(result.unpin_planned) == 1
    assert result.unpin_planned[0].endswith(REASON_CLOSED)


def test_a_dry_run_never_unpins(enabled, registry, odoo, store, reports_dir):
    make_report(reports_dir, 94, 1789114351000, None)
    run_once(enabled, registry, odoo, store, dry_run=False, now=SOON)
    pinata = FakePinata()

    result = run_once(
        enabled, registry, odoo, store, dry_run=True, unpinner=pinata, now=LATER
    )

    assert pinata.made == 0
    assert result.unpin_planned[0].endswith(REASON_AGE)


def test_a_file_pinata_no_longer_has_counts_as_done(
    enabled, registry, odoo, store, reports_dir
):
    cid = cid_of(make_report(reports_dir, 94, 1789114351000, None))
    run_once(enabled, registry, odoo, store, dry_run=False, now=SOON)

    run_once(
        enabled,
        registry,
        odoo,
        store,
        dry_run=False,
        unpinner=FakePinata({cid}),
        now=LATER,
    )
    again = FakePinata()
    run_once(enabled, registry, odoo, store, dry_run=False, unpinner=again, now=LATER)

    assert again.unpinned == []


def test_a_pinata_failure_is_retried_next_run(
    enabled, registry, odoo, store, reports_dir
):
    cid = cid_of(make_report(reports_dir, 94, 1789114351000, None))
    run_once(enabled, registry, odoo, store, dry_run=False, now=SOON)

    failed = run_once(
        enabled,
        registry,
        odoo,
        store,
        dry_run=False,
        unpinner=FakePinata(fails=True),
        now=LATER,
    )
    retry = FakePinata()
    run_once(enabled, registry, odoo, store, dry_run=False, unpinner=retry, now=LATER)

    assert failed.unpin_failed == 1
    assert retry.unpinned == [cid]


def test_reports_handled_before_the_cid_was_recorded_get_it_from_their_manifest(
    enabled, registry, odoo, store, reports_dir
):
    directory = make_report(reports_dir, 94, 1789114351000, None)
    run_once(enabled, registry, odoo, store, dry_run=False, now=SOON)
    with store._connect() as connection:  # as rows written by the previous release
        connection.execute(
            "UPDATE processed_reports SET cid = NULL, published_at = NULL"
        )
    pinata = FakePinata()

    run_once(enabled, registry, odoo, store, dry_run=False, unpinner=pinata, now=LATER)

    assert pinata.unpinned == [cid_of(directory)]


def test_the_pinata_client_tells_unpinned_from_already_gone(monkeypatch):
    import io
    import urllib.error

    from pydantic import SecretStr

    from rrs_helpdesk_bridge import unpin as module

    calls = []

    def answer(code, body=b""):
        def urlopen(request, timeout):
            calls.append(
                (request.get_method(), request.full_url, dict(request.headers))
            )
            if code == 200:
                return io.BytesIO(b"OK")
            raise urllib.error.HTTPError(
                request.full_url, code, "x", {}, io.BytesIO(body)
            )

        return urlopen

    client = module.PinataUnpinner(SecretStr("key"), SecretStr("secret"))

    monkeypatch.setattr(module.urllib.request, "urlopen", answer(200))
    assert client.unpin("QmOurs") is True
    method, url, headers = calls[-1]
    assert (method, url) == ("DELETE", "https://api.pinata.cloud/pinning/unpin/QmOurs")
    assert headers["Pinata_api_key"] == "key"

    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        answer(400, b"CURRENT_USER_HAS_NOT_PINNED_CID"),
    )
    assert client.unpin("QmGone") is False

    monkeypatch.setattr(module.urllib.request, "urlopen", answer(500))
    with pytest.raises(UnpinError):
        client.unpin("QmLater")
