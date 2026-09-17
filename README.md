# rrs-helpdesk-bridge

`rrs-helpdesk-bridge` turns reports prepared by
[rrs-connector](https://github.com/PinoutLTD/rrs-connector) into tickets in
Odoo Helpdesk (`helpdesk_mgmt`). It is the administrative half of the Robonomics
Report Service: the connector collects, downloads, and decrypts reports from
clients' Home Assistant servers, this service files them where the company
already works with its clients.

> **Current status:** verified end to end against the test Odoo database on
> 2026-09-16 with real reports — three tickets, attachments, a repeat note, and
> no mail queued, no followers created. Not deployed yet. Writing is off by
> default — every run is a dry run unless `--write` is given.

## Scope

```text
rrs-connector                      rrs-helpdesk-bridge
 data/reports/<client>/<report>/ --> read manifest.json
   manifest.json                     |
   archive.zip                       v
   decrypted/                     deduplicate by problem signature
                                     |
                                     v
                                  Odoo Helpdesk: new ticket or a note
                                  on the open one, with attachments
```

The connector's SQLite is private to the connector: the two services meet only
through the file contract (`manifest.json`, contract version 1). This service
never writes into the connector's data directory and can read it from a
read-only mount.

## Architecture

- `config.py` — settings, the two profiles (`test`, `prod`), and the client
  registry (`client_id` → `res.partner`);
- `secrets.py` — Odoo URL, database, user, and API key from Proton Pass through
  `pass-cli`, kept in memory only;
- `manifests.py` — reading and validating the connector's contract;
- `ticket_builder.py` — pure functions: problem signature, title, HTML
  description, ticket values;
- `odoo_client.py` — XML-RPC, with every mutating call guarded and quiet;
- `state.py` — a small SQLite database of reports already filed;
- `dispatcher.py` — one run: read, decide, create or append;
- `main.py` — CLI and exit codes.

## Decisions

### Colleagues are notified, clients are not

A new ticket is an early warning for the team, so the people in
`notify_emails` (in the registry file) receive an e-mail when one is opened:

- an address is only ever subscribed if it belongs to an **internal** Odoo user
  (`share = False`), so a client address in that list resolves to nobody
  instead of turning into a message to the client;
- they are subscribed to the "Ticket Created" subtype **only**, so the repeat
  notes on the same ticket stay silent — one e-mail per new problem, not per
  report;
- if that subtype cannot be found, nobody is subscribed at all rather than
  being subscribed to everything.

### Nothing reaches the client by e-mail

The production helpdesk has a live SMTP server, and stages 4–6 carry a customer
notification template. A ticket opened here is an internal early warning, not a
message to the client, so:

- tickets are always created in the opening stage (`RRSB_STAGE_ID`, "New"), and
  this service never sets a closing stage;
- `partner_email` and `email_cc` are never written, and the client partner is
  never made a follower;
- tickets are **not linked** to the client's contact (`partner_id`) unless
  `RRSB_LINK_CLIENT_PARTNER=true`. The closing stages' template writes to the
  ticket's partner, so a linked ticket closed by a person would e-mail the
  client. The description points at the contact instead, which reaches no one;
- every mutating call carries `tracking_disable`, `mail_create_nosubscribe`,
  `mail_create_nolog`, and `mail_notrack`;
- repeat occurrences are added as internal notes.

Messages are posted through the `mail.compose.message` wizard rather than
`message_post`: over XML-RPC a body can only be a plain string, and
`message_post` escapes those, so the HTML reached the ticket and the
notification e-mail as visible markup. The composer's body is an Html field
and is stored as written.

### Odoo is the source of truth for deduplication

A problem signature is `sha256(client_id | issue type | fingerprint)[:12]`,
stored in the ticket's `source` field. Before filing a report the bridge asks
Odoo for an **open** ticket with that signature:

- no open ticket → a new one is created;
- an open ticket → `count` and `last_occurred` are updated and the details are
  added as a note with the new files attached;
- a ticket closed by a human is never reopened: the next report of the same
  problem opens a new ticket, which is how a recurring fault becomes visible.

The fingerprint is empty in version 1, so reports of one type fold into one open
ticket per site: the concrete errors rotate while the underlying fault does not.
It is a separate function so per-type fingerprints can be added later.

The local SQLite only records which report has already been filed. If the
service crashes between creating a ticket and recording it, the next run finds
the open ticket by its signature and appends a note instead of creating a
duplicate.

### Dry run by default

`run-once` reads Odoo, decides what it would do, logs it, and changes nothing.
`--write` performs the writes. On the `prod` profile `--write` is not enough on
its own: `RRSB_WRITE_ENABLED=true` must also be set, so a stray flag cannot
reach the live helpdesk.

### What is filed and what is not

- A report whose manifest has no `issue_file` carries logs only (a manual
  report) and is recorded as skipped — there is no problem to describe.
- A manifest with an unknown `contract_version` is refused loudly; the run ends
  with exit code `3` and the report stays for a newer version of this service.
- A `client_id` missing from the registry still gets a ticket, without
  `partner_id` and with a warning: an unknown site is exactly the case that
  must not be silently dropped.
- Decrypted files are attached to the ticket unless they exceed
  `RRSB_MAX_ATTACHMENT_BYTES` (Odoo stores attachments base64-encoded in the
  database); oversized files are left on disk and named in a warning.

## Configuration

Python `>=3.13` and [uv](https://docs.astral.sh/uv/). Environment variables,
usually in a local `.env` (see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `RRSB_PROFILE` | `test` or `prod`; selects the Proton Pass item with the Odoo credentials |
| `RRSB_WRITE_ENABLED` | second key required for writing on the `prod` profile |
| `RRSB_REPORTS_DIR` | the connector's `<RRS_DATA_DIR>/reports` |
| `RRSB_STATE_DB` | path to this service's SQLite database |
| `RRSB_REGISTRY_FILE` | path to the client registry YAML |
| `RRSB_STAGE_ID` | opening stage for new tickets (default `1`) |
| `RRSB_CHANNEL_ID` | ticket channel (default `4`) |
| `RRSB_COMPANY_ID` | company (default `1`) |
| `RRSB_NOTE_EMAIL_FROM` | sender address for notes, when the API user has no e-mail |
| `RRSB_LINK_CLIENT_PARTNER` | link tickets to the client's contact; off, so closing a ticket mails no client (default `false`) |
| `RRSB_ATTACH_FILES` | attach decrypted files to tickets (default `true`) |
| `RRSB_MAX_ATTACHMENT_BYTES` | per-file attachment limit (default 5 MiB) |

The registry maps a site to a partner and lists the colleagues to notify; its
format is in `config/registry.example.yaml`. `client_id` is the site slug shared with the
connector and the field engineer's repository (`qube-block-a-301`), so a report,
its ticket, and the site card are found by one key.

### Secrets

No credentials live on disk. The profile selects a Proton Pass item read through
`pass-cli` at runtime, with the fields `odoo_url`, `odoo_db`, `odoo_username`,
`odoo_api_key`:

| Profile | Vault | Item |
| --- | --- | --- |
| `test` | `Report Service` | `Odoo API Test` |
| `prod` | `Smart Home Agent` | `Odoo API` |

Odoo refuses `message_post` when the user behind the API key has no e-mail
address. Give that user one, or set `RRSB_NOTE_EMAIL_FROM`.

Locally an interactive `pass-cli login` session is enough. On a server, use a
Proton Pass agent token limited to that item; `PROTON_PASS_AGENT_REASON` is set
automatically unless provided. Such a token cannot see the vault that holds the
item, so when the lookup by vault name fails the item is found in
`pass-cli share list` by title and read through its own share; the vault in the
table above still describes where it lives for a human session. The Odoo user behind the API key only needs
helpdesk rights (read, write, create — no delete).

## Usage

```bash
uv sync --extra dev
```

Check the connection, the stages, the channels, and the registry without
touching anything:

```bash
uv run rrs-helpdesk-bridge --command odoo-check
```

See what would be filed:

```bash
uv run rrs-helpdesk-bridge --command run-once
```

File it for real:

```bash
uv run rrs-helpdesk-bridge --command run-once --write
```

Exit codes: `0` success, `1` startup or unexpected failure, `3` some reports
could not be read or filed.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests run against a fake Odoo that enforces the same dry-run guard as the real
client, and against report directories written exactly as the connector leaves
them.

## Not done yet

- a run against the production Odoo (`odoo-check` and a dry run on the `prod`
  profile);
- deployment: service user, Proton Pass agent token, systemd timer next to the
  connector's;
- what happens to a ticket when the connector deletes the report files
  (retention is still an open question on the connector's side);
- liveness: a site that goes silent produces no reports, so no ticket — that
  alert belongs to the connector.
