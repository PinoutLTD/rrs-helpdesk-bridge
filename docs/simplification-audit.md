# Simplification audit

October 2026, at `c76b843`. Read-only pass over `src/` hunting over-engineering
only: what to delete, simplify, or replace with the standard library. Nothing
here is applied yet; pick items by number.

The bridge is the leanest of the three repositories: two runtime dependencies
besides pydantic, and Odoo, SQLite and Pinata are reached through the standard
library (`xmlrpc`, `sqlite3`, `urllib`). `attachments`, `changes`, `unpin` and
`manifests` are left alone.

Considered and kept: `secrets.py` repeats the connector's `proton_pass.py`
(pass-cli and the item-share fallback), and `config.py` repeats its
`normalize_path`, `load_yaml_file` and the client slug pattern. Removing that
needs a shared package for a few dozen lines across two repositories: not worth
it.

## Findings, biggest cut first

1. **shrink** the two skip branches of `dispatch_report` (logs only; site back
   without an open ticket) repeat log, `skipped += 1` and
   `store.record(… Action.SKIPPED …)` unless a dry run. One `_skip(report,
   signature, detail)` (~15 lines). [dispatcher.py:206-240]
2. **reuse** the issue types are spelled as literals across `ticket_builder`
   (`ISSUE_LABELS`, `ticket_priority`, `details_html`) while `changes.py` holds
   `ENTITIES` and `LOGS` and `ticket_builder` holds `SITE_*`. One set of
   constants next to `ReportIssue` in `manifests.py`; no lines saved, one place
   for a new type. [ticket_builder.py, changes.py]
3. **reuse** the device list `<li>device: entity, entity</li>` is built three
   times: `without_data_html`, the device loop of `entities_html`, `_grouped`.
   One helper over `{device: [entities]}` (~10 lines). [ticket_builder.py]
4. **yagni** `issue_fingerprint` always returns `""`, with a docstring for a
   version that never came. Inline it, keeping the trailing `|` in the
   signature source: the hash is the `source` of open tickets in Odoo and must
   not change (~10 lines). [ticket_builder.py:46-62]
5. **stdlib** `find_created_subtype_id` and `find_note_subtype_id` cache by hand
   (`_created_subtype_id`, `_note_subtype_id`, `if None`) →
   `functools.cached_property` (~10 lines). [odoo_client.py:155-185]
6. **yagni** `StateStore.get` and `ProcessedReport` are read only by tests; a
   test can query the table itself (~25 lines). [state.py:225-241]
7. **delete** `logging_config.py`, a file for one `basicConfig` call; inline it in
   `main` (−1 file, ~8 lines). The connector has the same file.
8. **yagni** the one-implementation Protocols `Unpinner` and `ClosedTickets`,
   there for test fakes (~8 lines). [unpin.py:51-58]
9. **shrink** small things: `server_version` builds a second `common`
   `ServerProxy` instead of keeping the one from `__init__`; `self.username` is
   kept only for that one `authenticate`; `Report` is a pydantic model with
   `arbitrary_types_allowed` where a dataclass does (~8 lines).
   [odoo_client.py, manifests.py:68]

net: about −95 lines, no dependency to drop.

## Suggested grouping

All of it fits one small PR; item 4 needs a test that the signature of an
existing ticket does not change.
