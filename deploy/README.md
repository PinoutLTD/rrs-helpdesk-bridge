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
- `/etc/rrs/bridge.healthcheck` owned `root:rrs-bridge`, mode `0640`, holding the
  Healthchecks.io ping URL of this service (see below);
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

The ping URL is a secret: whoever knows it can report "all is well". It lives in Proton Pass (vault "Report Service", item "rrs-bridge (Healthchecks)", field Password) and on the host in a file readable by the service's group only. Copy it from Proton Pass to the host without printing it:

```bash
pass-cli item view --vault-name "Report Service" --item-title "rrs-bridge (Healthchecks)" --field password \
  | ssh <host> 'umask 037 && cat > /etc/rrs/bridge.healthcheck && chgrp rrs-bridge /etc/rrs/bridge.healthcheck && chmod 640 /etc/rrs/bridge.healthcheck'
```

`/etc/rrs` itself is root-only (`0700`), so the service user cannot even reach the file in it. Let it pass through the directory, and only that, with an ACL on the directory (no listing, and the token files stay root-only):

```bash
setfacl -m u:rrs-bridge:x /etc/rrs
```

Without this the script finds no readable file and says so in the journal.

The script reads that file itself (the unit passes its path), and it is not handed over with `LoadCredential=` like the token: on systemd 249 (Ubuntu 22.04) the credentials directory cannot be read in `ExecStopPost=`, and the script used to exit there without a word, so for the first weeks no ping was ever sent.

Every run leaves one line in the journal from `healthcheck.sh`: `reported result=…`, or why nothing was sent (no readable file, an empty file, Healthchecks.io unreachable). A missing file or an unreachable Healthchecks.io does not fail a run, but it shows there. After setting this up, check `journalctl -u rrs-helpdesk-bridge -n 20` for `healthcheck: reported result=success`, and the check's "last ping" in Healthchecks.io.
