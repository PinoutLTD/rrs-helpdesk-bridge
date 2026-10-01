# Deployment

The bridge runs as a systemd timer under its own `nologin` user, ten minutes
after the connector, with the Proton Pass agent token delivered by systemd and
never readable by that user.

```bash
sudo install -m 0644 deploy/rrs-helpdesk-bridge.service deploy/rrs-helpdesk-bridge.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start rrs-helpdesk-bridge.service   # dry run, watch the journal
sudo systemctl enable --now rrs-helpdesk-bridge.timer
```

The unit starts as a dry run on purpose: it reads Odoo, logs what it would
file, and writes nothing. Once the first tickets have been checked by hand,
add `--write` to `ExecStart`, `daemon-reload`, and run it again.

Expected around it:

- user `rrs-bridge` in the shared group `rrs-reports`, so it can read the
  connector's decrypted files (the connector needs
  `RRS_ARTIFACT_GROUP_READABLE=true`);
- `/etc/rrs/bridge.healthcheck` root-only, holding the Healthchecks.io ping URL
  of this service (see below);
- `/etc/rrs/bridge.token` root-only, holding the agent token granted the Odoo
  API item alone;
- `/var/lib/rrs-bridge/pass-session` owned by the service user;
- `pass-cli` in `/usr/local/bin`, and a `.venv` built from `uv sync`;
- `.env` pointing `RRSB_REPORTS_DIR` at the connector's `reports` directory,
  and `config/registry.yaml` with the sites and the colleagues to notify.

## Healthchecks.io

After every run, `deploy/healthcheck.sh` (the unit's `ExecStopPost=`) reports how the run ended to the Healthchecks.io check of this service:
- a successful run pings it;
- a failed run is only logged there.

The check is set to a 1 h period with 1 h grace. So the team gets an alert when no run has succeeded for two hours: two failures in a row, or the server down. A single passing failure wakes nobody.

The ping URL is a credential: whoever knows it can report "all is well". It lives in Proton Pass (vault "Report Service", item "rrs-bridge (Healthchecks)", field Password) and on the host in a root-only file, handed to the unit by `LoadCredential=`. Copy it from Proton Pass to the host without printing it:

```bash
pass-cli item view --vault-name "Report Service" --item-title "rrs-bridge (Healthchecks)" --field password \
  | ssh <host> 'umask 077 && cat > /etc/rrs/bridge.healthcheck'
```

The unit does not start without the file. Neither a missing URL inside it nor an unreachable Healthchecks.io fails a run.
