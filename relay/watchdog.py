"""Periodic deterministic routing independent of Discord, Muse and any model API."""
import asyncio
import logging

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, store, project, dispatch=None, notify=None, *, worker=asyncio.to_thread):
        self.store = store
        self.project = project
        self.dispatch = dispatch
        self.notify = notify
        self.running = set()
        self.worker = worker

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
            if (await self.worker(self.store.get, job["id"]))["state"] == "RUNNING":
                await self.worker(self.store.fail, job["id"], job["provider"], job["lease_token"],
                                  error or "FORMAT", retry_after=getattr(error, "retry_after", None))
        except PermissionError:
            pass  # A timeout, pause or newer generation has fenced this process.

    def _poll(self):
        self.store.maintenance(self.project)
        self.store.route_notices(self.project)
        if self.notify:
            for receipt, message in self.store.notifications(self.project):
                self.notify(f"watchdog:{self.project}:{receipt}", message)
                self.store.acknowledge(receipt)

        return self.store.dispatchable(self.project) if self.dispatch else []

    async def tick(self):
        for job in await self.worker(self._poll):
            task = asyncio.create_task(self._execute(job))
            self.running.add(task)
            task.add_done_callback(self.running.discard)

    async def run(self):
        try:
            while True:
                try:
                    await self.tick()
                except Exception as exc:
                    log.warning("Watchdog check deferred (%s)", type(exc).__name__)
                await asyncio.sleep(self.store.config.watchdog.interval_seconds)
        finally:
            for task in tuple(self.running):
                task.cancel()
            await asyncio.gather(*self.running, return_exceptions=True)
