# Linux / LXC deployment and recovery

## Core and credentials

Use one Core per management repository. The GitHub ledger contains published tasks, issue bindings and scheduling history. The private local directory contains SQLite, optional calendar overrides and the service lock. GitHub App installations scoped independently to the management and product repositories are preferred. Supply current installation tokens from your credential manager and refresh them before expiry. For a small deployment, two distinct fine-grained PATs scoped to the appropriate repositories are supported. Automatic GitHub App private-key exchange is outside the service.

Install the package into `/opt/sprintrelay/.venv` from the reviewed checkout. Keep that checkout, the virtual environment, and the unit files administrator-owned. Create an unprivileged system account named `sprintrelay`; provide its group read access to the operational TOML only. Create `/etc/sprintrelay/core.env` as a root-owned 0600 systemd EnvironmentFile containing the three values in `.env.example`. Scope product permissions to the selected repositories. Ordinary PR discussion comments use the Issues API. Project mutations require access to the selected Project. Keep the management repository private and writable by this identity.

Install `deploy/sprintrelay.service` under `/etc/systemd/system`. Put the configured TOML at `/etc/sprintrelay/config.toml`. For example, from the deployment checkout:

```sh
sudo install -m 0644 deploy/sprintrelay.service /etc/systemd/system/sprintrelay.service
sudo systemctl daemon-reload
sudo -u sprintrelay /opt/sprintrelay/.venv/bin/python -m relay check --config /etc/sprintrelay/config.toml
sudo systemctl enable --now sprintrelay.service
```

For AI workers, use the separate gateway socket/service templates and per-vendor login instructions. A missing provider or unavailable isolation remains disabled or degraded. Keep the official user login exclusively with its dedicated worker account. Confirm permissible headless use and the exact CLI tool restrictions with synthetic data before enabling the provider. [Worker provisioning and common prompts](agents/)

## MCP exposure

The local MCP endpoint defaults to `http://127.0.0.1:8371/mcp`. Each local launch receives a fresh job-bound credential, expiry and lease. The Core exposes the same domain services to Discord and MCP. The service starts with remote access disabled.

Remote Muse access additionally requires an OAuth authorization server that supports the connector's flow, a registered client, exact subject, issuer, HTTPS JWKS URL, resource audience and `sprintrelay:management` scope. Set all `mcp` fields in the example configuration. Route **only** the remote loopback listener (default 8372) through an HTTPS reverse proxy. Overwrite the inbound Host and X-Forwarded-Proto headers; never proxy the local worker port. Restrict network access to the public TLS port. The private worker service accounts must have no access to other local services or the Core state directory. Example nginx locations inside a TLS-enabled virtual host for the configured resource:

```nginx
location = /mcp {
    client_max_body_size 5m;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_set_header Host relay.example;
    proxy_set_header X-Forwarded-Proto https;
    proxy_pass http://127.0.0.1:8372;
}
location = /.well-known/oauth-protected-resource/mcp {
    proxy_set_header Host relay.example;
    proxy_set_header X-Forwarded-Proto https;
    proxy_pass http://127.0.0.1:8372;
}
```

Replace the example host with the exact configured resource hostname. Configure the proxy to omit Authorization headers, query strings, request bodies and private results from logs. Validate TLS, OAuth discovery, rejection of a wrong audience/subject and authorized `tools/list` from a synthetic connector. Only enable real Muse scheduling after checking a complete 45-minute patrol, renewal, checkpoint and a deliberately missed slot. If the account cannot perform this flow, keep Muse unavailable.

## Existing installation

Stop the v1 service before upgrading. Retain the original binary/environment and copy the **entire private state directory** (including SQLite sidecars and calendar overrides) to a private backup. Preserve the existing bot IDs, named GitHub repositories and project. The v2 project boundary is the management repository name. A v1 blanket consent does not authorize named external processors in v2.

```sh
python -m relay migrate --config config.toml --state-dir state --dry-run
python -m relay migrate --config config.toml --state-dir state
python -m relay status --config config.toml --state-dir state
python -m relay doctor --config config.toml
```

The dry-run uses an in-memory copy. The first real upgrade creates a verified `pre-v2-*.sqlite3` online SQLite backup (including committed WAL pages), then applies additive tables and legacy workflow backfill in a transaction. Repeating the command preserves the existing version. Known historical actors and digests remain intact; unknown historical timestamps stay unknown. Reuse of the published GitHub issue numbers and the private specification archive requires no GitHub migration. Remove obsolete AI credentials from the service environment at deployment.

To make another database backup, stop the service and run `python -m relay backup --config config.toml --state-dir state --output /private/backup/relay.sqlite3`. The destination directory must be 0700 and the destination filename unused. For rollback, stop all writers, quarantine the current state directory, restore the full backup to a **fresh** 0700 directory with its original owner, then start the matching software version. To restore just the SQLite backup, put it in the fresh directory as `sprint-relay.sqlite3` (0600) and restore the matching calendar overrides separately. Inspect GitHub for activity since that backup before resuming publication. A database rollback cannot undo Issues or comments already posted.

## Operation and trust boundaries

- `/relay status` shows routing, health and patrol state. Only approved operators can pause/resume AI processing. Pending jobs retain their IDs across failover. A provider whose authorization expires requires ordinary account reauthentication. Review synthetic proof and subsequent job receipts in the configured deployment.
- Save TOML changes atomically. Agent enablement, project-data recipients, patrol period, watchdog, job timeout, polling and retention settings are reread at runtime. Identity/endpoint changes require a restart. Update the Muse-side schedule after changing the patrol interval. Use Discord's schedule update command for event period and availability.
- The management agent can request an audit, propose tasks, submit spec artifacts and use the restricted patrol actions. The audit agent reads only the assigned target. Component access checks use the verified identity, project, job, expected revision and current lease. Human acceptance and approval enter through authenticated human operations.
- The product credential stays in Core. The adapter constrains GitHub resource paths and operations; GitHub must independently grant **no product Contents write** to that credential. CLI workers receive no Core credential. The worker sandbox uses a fresh filesystem, fixed native client, separate OS identity and bounded process group. The shipped configuration provides read-only audit evidence through MCP and does not run repository tests. Host administrators, root-managed images, the OAuth issuer and explicitly authorized data processors are within the trust boundary.
- Raw transcripts expire according to `privacy.raw_retention_days`. Scope, protect and expire backups independently. If the SQLite volume is unavailable, stop the service and recover the state before restarting. If GitHub is unavailable, leave operations pending. Reconcile with the existing job/receipt on recovery. Notify operators if deployment access to the Project, Issues or Discord channel is revoked.
