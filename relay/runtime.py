"""One Core, shared by Discord, both MCP listeners and the watchdog."""
import asyncio
import logging
import time
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path

import uvicorn

from .bot import Service, create_client
from .changes import ChangeManager
from .config import load_config
from .github import Conflict, GitHub, GitHubError
from .patrol import Patrol
from .probe_diagnostics import emit
from .security import OAuthVerifier, Principal, TokenVault
from .workflow import Workflow


log = logging.getLogger(__name__)
DYNAMIC = ("agents", "patrol", "watchdog", "jobs", "github_sync", "privacy")


class Control:
    def __init__(self, config, store, wake=None):
        self.config, self.store, self.wake = config, store, wake
        self.project = config.management_repo

    def status(self):
        return self.store.status(self.project)

    def jobs(self):
        visible = ("id", "kind", "role", "state", "provider", "attempts", "created", "finished")
        return [{k: row[k] for k in visible if k in row}
                for row in self.store.list_jobs(self.project, limit=50)]

    def _authorize(self, actor):
        if str(actor) not in self.config.approvers:
            raise PermissionError("Configured human administrator required")

    def pause(self, actor):
        self._authorize(actor)
        self.store.set_paused(self.project, True, actor=str(actor))

    def resume(self, actor):
        self._authorize(actor)
        self.store.set_paused(self.project, False, actor=str(actor))

    def retry(self, actor, provider, role=None):
        self._authorize(actor)
        receipts = self.store.retry_probe(self.project, provider, role, actor=str(actor))
        if self.wake and any(item["state"] in {"queued", "already_queued"} for item in receipts):
            self.wake()
        return receipts

    def patrol(self, actor):
        self._authorize(actor)
        return self.store.patrol(self.project, force=True, actor=str(actor))


class ConfigurationFile:
    """Reload operational limits and consent policy as a complete validated file."""
    def __init__(self, path, config, notify):
        self.path, self.config, self.notify = Path(path), config, notify
        self.signature = self.path.stat().st_mtime_ns

    def refresh(self):
        stamp = self.path.stat().st_mtime_ns
        if stamp == self.signature:
            return
        updated = load_config(self.path)
        for f in fields(self.config):
            # Calendar can also be changed in Discord. File changes take effect on restart.
            if f.name not in (*DYNAMIC, "calendar") and getattr(updated, f.name) != getattr(self.config, f.name):
                raise ValueError("Restart required for changed identity or listener settings")
        changed_interval = updated.patrol.interval_minutes != self.config.patrol.interval_minutes
        for name in DYNAMIC:
            setattr(self.config, name, getattr(updated, name))
        self.signature = stamp
        if changed_interval:
            self.notify(f"patrol-config:{stamp}", "Patrol interval changed. Update the Muse schedule to match.")


class Listener(uvicorn.Server):
    # asyncio.run owns interruption and the shared service shutdown.
    @contextmanager
    def capture_signals(self):
        yield


async def serve(config, config_path, path, management_token, product_token, discord_token):
    from .gateway import Gateway
    from .jobs import JobStore
    from .mcp_server import create_app
    from .watchdog import Watchdog
    from .worker import run_cli

    # The SDK can include malformed input in validation warnings. Diagnostics
    # use only the allowlisted events emitted by the relay instead.
    for name in ("mcp.server.streamable_http", "mcp.server.transport_security",
                 "mcp.server.lowlevel.server", "mcp.server.session",
                 "mcp.server.streamable_http_manager", "mcp.shared.session"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    project = config.management_repo
    github = GitHub(config, management_token, path, product_token=product_token)
    if not await asyncio.to_thread(github.is_private, project):
        raise ValueError("The management repository must be private")
    jobs = JobStore(path, config)
    workflow = Workflow(path, config, github, changes=ChangeManager(path, config, github),
                        project=project, jobs=jobs)
    patrol = Patrol(config, github, jobs, path, workflow=workflow)
    gateway = Gateway(project, config, jobs, workflow, patrol, None, github)
    vault = TokenVault()

    async def dispatch(job, provider):
        probe = job["kind"] == "probe"
        diagnose = config.jobs.probe_diagnostics if probe else provider == "codex" and config.jobs.job_diagnostics
        identity = Principal(f"worker:{job['id']}", provider, job["role"], project, str(job["id"]),
                             synthetic_probe=probe, diagnose=diagnose)
        ttl = config.jobs.timeout_seconds
        snapshot = None
        if job["kind"] == "audit" and job["payload"].get("kind") in {"pr", "ci"}:
            try:
                snapshot = await asyncio.to_thread(patrol.cli_snapshot, str(job["id"]),
                                                   provider=provider, lease_token=job["lease_token"])
                # A GitHub read may have consumed the last seconds of a lease.
                await asyncio.to_thread(jobs.require_lease, job["id"], provider, job["lease_token"])
            except (PermissionError, Conflict, ValueError, GitHubError, OSError) as exc:
                if isinstance(exc, PermissionError):
                    code, failure = "AUDIT_SNAPSHOT_DENIED", "PERMISSION"
                elif isinstance(exc, (Conflict, ValueError)):
                    code, failure = "AUDIT_SNAPSHOT_INVALID", "INPUT"
                else:
                    code, failure = "AUDIT_SNAPSHOT_DEPENDENCY_FAILED", "DEPENDENCY"
                emit(code, enabled=diagnose, probe=False, job_id=job["id"], provider=provider, role=job["role"])
                return failure
        token = vault.issue(identity, ttl, lease_token=job["lease_token"])
        try:
            return await run_cli(provider, project=project, role=job["role"], job_id=str(job["id"]),
                                 token=token, mcp_url=f"http://127.0.0.1:{config.mcp.local_port}/mcp",
                                 timeout_seconds=ttl, model=config.agents[provider].model, snapshot=snapshot,
                                 kind=job["kind"], diagnose=diagnose,
                                 spec_audit=job["kind"] == "audit" and job["payload"].get("kind") == "spec")
        finally:
            vault.revoke(token)

    watchdog = Watchdog(jobs, project, dispatch=dispatch, notify=github.notify)
    control = Control(config, jobs, wake=watchdog.wake)
    service = Service(config, github, workflow, control, path)
    client = create_client(service)
    cfg_file = ConfigurationFile(config_path, config, github.notify)
    endpoints = [(create_app(gateway, local_tokens=vault), config.mcp.local_port)]
    if config.mcp.remote_enabled:
        m = config.mcp
        auth = OAuthVerifier(m.issuer, m.jwks_url, m.resource, m.client_id, m.subject, project)
        endpoints.append((create_app(gateway, oauth=auth), m.remote_port))

    async def maintain():
        last = None
        while True:
            try:
                await asyncio.to_thread(cfg_file.refresh)
                if last is None or time.monotonic() - last >= 86400:
                    await asyncio.to_thread(workflow.purge_raw, config.privacy.raw_retention_days)
                    last = time.monotonic()
            except (OSError, ValueError) as exc:
                log.warning("Configuration or retention check deferred (%s)", type(exc).__name__)
            await asyncio.sleep(config.watchdog.interval_seconds)

    servers = [Listener(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False,
                       proxy_headers=port == config.mcp.remote_port and config.mcp.remote_enabled,
                       forwarded_allow_ips="127.0.0.1", server_header=False, log_config=None))
               for app, port in endpoints]
    async def run_watchdog():
        # Probe clients may be launched immediately on a fresh or restarted
        # Core. Wait for the local socket and SDK lifespan before dispatching.
        while not servers[0].started:
            await asyncio.sleep(0.05)
        await watchdog.run()

    workers = [asyncio.create_task(server.serve()) for server in servers]
    workers.extend([asyncio.create_task(client.start(discord_token)),
                    asyncio.create_task(run_watchdog()), asyncio.create_task(maintain())])
    try:
        done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for server in servers:
            server.should_exit = True
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await client.close()
