"""Periodic deterministic routing independent of Discord, Muse and any model API."""
import asyncio
import logging

from .probe_diagnostics import emit

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, store, project, dispatch=None, notify=None, *, worker=asyncio.to_thread):
        self.store = store
        self.project = project
        self.dispatch = dispatch
        self.notify = notify
        self.running = {}
        self.worker = worker
        self._wake = asyncio.Event()
        self._loop = None

    async def _execute(self, job):
        try:
            try:
                error = await self.dispatch(job, job["provider"])
            except (FileNotFoundError, PermissionError):
                error = "UNAVAILABLE"
            except Exception as exc:
                log.warning("Worker exited (%s)", type(exc).__name__)
                error = "TRANSIENT"
            # Only a transactionally validated MCP submission can complete work.
            state = (await self.worker(self.store.get, job["id"]))["state"]
            probe = job.get("kind") == "probe"
            enabled = (self.store.config.jobs.probe_diagnostics if probe else
                       job["provider"] == "codex" and self.store.config.jobs.job_diagnostics)
            if state == "COMPLETED" or (error is None and state == "RUNNING"):
                code = "CLI_EXITED_WITHOUT_MCP_COMPLETION"
                if state == "COMPLETED":
                    code = "PROBE_COMPLETED" if probe else "JOB_COMPLETED"
                emit(code, enabled=enabled, job_id=job["id"], provider=job["provider"], role=job["role"], probe=probe)
            if state == "RUNNING":
                await self.worker(self.store.fail, job["id"], job["provider"], job["lease_token"],
                                  error or "FORMAT", retry_after=getattr(error, "retry_after", None))
        except PermissionError:
            pass  # A timeout, pause or newer generation has fenced this process.

    def _poll(self, busy_providers):
        self.store.maintenance(self.project)
        self.store.route_notices(self.project)
        if self.notify:
            for receipt, message in self.store.notifications(self.project):
                self.notify(f"watchdog:{self.project}:{receipt}", message)
                self.store.acknowledge(receipt)

        return self.store.dispatchable(self.project, busy_providers=busy_providers) if self.dispatch else []

    async def tick(self):
        busy_providers = frozenset(self.running.values())
        for job in await self.worker(self._poll, busy_providers):
            task = asyncio.create_task(self._execute(job))
            self.running[task] = job["provider"]
            task.add_done_callback(self._released)

    def _released(self, task):
        self.running.pop(task, None)
        if self._loop is not None:
            self._wake.set()

    def wake(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        try:
            while True:
                self._wake.clear()
                try:
                    await self.tick()
                except Exception as exc:
                    log.warning("Watchdog check deferred (%s)", type(exc).__name__)
                try:
                    await asyncio.wait_for(self._wake.wait(), self.store.config.watchdog.interval_seconds)
                except TimeoutError:
                    pass
        finally:
            self._loop = None
            for task in tuple(self.running):
                task.cancel()
            await asyncio.gather(*self.running, return_exceptions=True)
