"""Durable queue, provider transitions and restart tests with a synthetic clock."""
import asyncio
from pathlib import Path

import pytest

from relay.config import Config
from relay.jobs import JobStore
from relay.watchdog import Watchdog


PROJECT = "example/relay-management"
POLICY = ("muse", "claude_code", "codex", "antigravity")


class Clock:
    def __init__(self):
        self.now = 1_000_000
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def queue(tmp_path):
    config = Config.load(Path(__file__).resolve().parents[1] / "config.example.toml")
    for provider in POLICY:
        config.agents[provider] = config.agents[provider].model_copy(update={
            "enabled": True, "verified": True, "terms_confirmed": True, "schedule_confirmed": True})
    config.privacy = config.privacy.model_copy(update={"project_providers": POLICY})
    return JobStore(tmp_path / "state.db", config, clock=Clock())


def verify_cli(store):
    """Exercise the synthetic MCP submission required before any real CLI work."""
    store.maintenance(PROJECT)
    for _ in range(4):
        for role in ("management", "audit"):
            for provider in POLICY[1:]:
                for job in store.list_jobs(PROJECT, kinds=["probe"], role=role, provider=provider):
                    claimed = store.claim(job["id"], provider)
                    store.complete(job["id"], provider, claimed["lease_token"],
                                   {"challenge": job["payload"]["challenge"]})


def enqueue(store, key, *, kind="minutes", allowed=POLICY):
    role = "audit" if kind == "audit" else "management"
    return store.enqueue(PROJECT, kind, role, key, {"reference": key}, allowed)


def failure(store, claim, reason, **kwargs):
    return store.fail(claim["id"], claim["provider"], claim["lease_token"], reason, **kwargs)


def complete(store, claim, result):
    return store.complete(claim["id"], claim["provider"], claim["lease_token"], result)


def test_a07_restarts_checkpoint_and_atomic_slot(queue):
    store = queue
    first = store.patrol(PROJECT)
    assert store.patrol(PROJECT, force=True) == first
    lease = store.claim(first, "muse")
    with pytest.raises(PermissionError):
        store.claim(first, "muse")
    with pytest.raises(ValueError):
        complete(store, lease, {})
    complete(store, lease, {"cursor": "snapshot-99"})
    restarted = JobStore(store.path, store.config, clock=store.clock)
    assert restarted.checkpoint(PROJECT) == "snapshot-99"
    restarted.clock.advance(45 * 60 - 1)
    assert restarted.patrol(PROJECT) == first
    restarted.clock.advance(1)
    next_slot = restarted.patrol(PROJECT)
    assert next_slot != first
    assert restarted.get(next_slot)["payload"]["slot"] == 2


def test_a09_provider_sequence_and_shared_quota(queue):
    verify_cli(queue)
    job = enqueue(queue, "meeting-A")
    assert failure(queue, queue.claim(job, "muse"), "QUOTA") == "RETRY_WAIT"
    failure(queue, queue.claim(job, "claude_code"), "AUTH")
    failure(queue, queue.claim(job, "codex"), "QUOTA", retry_after=7200)
    status = queue.status(PROJECT)
    assert status["active_providers"] == {"management": None, "audit": "antigravity"}
    audit = enqueue(queue, "sha", kind="audit")
    failure(queue, queue.claim(audit, "antigravity"), "UNAVAILABLE")
    assert not any(queue.status(PROJECT)["active_providers"].values())
    assert queue.get(job)["state"] == "RETRY_WAIT"
    assert queue.get(audit)["state"] == "RETRY_WAIT"
    assert next(h for h in status["health"] if h["provider"] == "codex")["retry_at"] >= queue.clock() + 7200


def test_a09_retry_once_before_role_local_fallback_and_no_unsafe_retry(queue):
    verify_cli(queue)
    audit = enqueue(queue, "audit-A", kind="audit")
    failure(queue, queue.claim(audit, "codex"), "TIMEOUT")
    queue.clock.advance(1)
    failure(queue, queue.claim(audit, "codex"), "FORMAT")
    assert queue.status(PROJECT)["active_providers"]["audit"] == "antigravity"
    assert failure(queue, queue.claim(audit, "antigravity"), "PERMISSION") == "FAILED"
    # A role-specific output failure does not disable Codex management.
    assert queue.claim(enqueue(queue, "other", allowed=["codex"]), "codex")["state"] == "RUNNING"


def test_a10_cli_synthetic_failback_respects_running_lease(queue):
    verify_cli(queue)
    audit = enqueue(queue, "sha1", kind="audit")
    failure(queue, queue.claim(audit, "codex"), "OUTAGE")
    until = next(h["retry_at"] for h in queue.status(PROJECT)["health"] if h["provider"] == "codex")
    queue.clock.advance(until - queue.clock())
    running = queue.claim(audit, "antigravity")
    queue.maintenance(PROJECT)
    # Same role cannot overlap. Management can probe the shared Codex account.
    probe = queue.list_jobs(PROJECT, kinds=["probe"], role="management", provider="codex")[0]
    challenged = queue.claim(probe["id"], "codex")
    with pytest.raises(ValueError):
        complete(queue, challenged, {"challenge": "unrelated"})
    complete(queue, challenged, {"challenge": probe["payload"]["challenge"]})
    busy = enqueue(queue, "sha2", kind="audit")
    with pytest.raises(PermissionError):
        queue.claim(busy, "codex")
    complete(queue, running, {"findings": []})
    assert queue.claim(busy, "codex")["provider"] == "codex"


def test_a10_muse_absent_failover_late_return_and_grace(queue):
    verify_cli(queue)
    slot = queue.patrol(PROJECT)
    queue.clock.advance(10 * 60 - 1)
    queue.maintenance(PROJECT)
    with pytest.raises(PermissionError):
        queue.claim(slot, "claude_code")
    queue.clock.advance(1)
    queue.maintenance(PROJECT)
    fallback = queue.claim(slot, "claude_code")
    with pytest.raises(PermissionError):
        queue.claim(slot, "muse")
    complete(queue, fallback, {"cursor": "safe-boundary"})
    wait = next(h["retry_at"] for h in queue.status(PROJECT)["health"] if h["provider"] == "muse")
    queue.clock.advance(wait - queue.clock())
    queue.probe_read(PROJECT, "muse", "management")
    queue.maintenance(PROJECT)
    next_slot = queue.patrol(PROJECT)
    assert next_slot != slot
    assert queue.claim(next_slot, "muse")["provider"] == "muse"


def test_a11_expired_fence_checkpoint_and_artifact_rollback(queue):
    verify_cli(queue)
    slot = queue.patrol(PROJECT)
    muse = queue.claim(slot, "muse")
    queue.clock.advance(60)
    assert queue.heartbeat(slot, "muse", muse["lease_token"]) > muse["lease_until"]
    queue.clock.advance(121)
    queue.maintenance(PROJECT)
    next_attempt = queue.claim(slot, "claude_code")
    assert next_attempt["fence_generation"] > muse["fence_generation"]
    for action in (lambda: complete(queue, muse, {"cursor": "stale"}),
                   lambda: queue.heartbeat(slot, "muse", muse["lease_token"])):
        with pytest.raises(PermissionError):
            action()
    def reject(db, job, provider, result):
        raise ValueError("Snapshot validation failed")
    with pytest.raises(ValueError):
        queue.complete(slot, "claude_code", next_attempt["lease_token"], {"cursor": "bad"}, on_submit=reject)
    assert queue.checkpoint(PROJECT) is None
    final = complete(queue, next_attempt, {"cursor": "good"})
    assert queue.artifact(final) == {"cursor": "good"}
    assert queue.checkpoint(PROJECT) == "good"


def test_a15_all_unavailable_recovers_after_explicit_consent_and_verification(queue):
    queue.config.privacy = queue.config.privacy.model_copy(update={"project_providers": ("codex",)})
    queue.config.agents["codex"] = queue.config.agents["codex"].model_copy(update={"verified": False})
    job = enqueue(queue, "approved", allowed=["codex"])
    assert queue.status(PROJECT)["degraded"]
    assert queue.list_jobs(PROJECT, provider="muse") == []
    assert not queue.dispatchable(PROJECT)
    queue.config.agents["codex"] = queue.config.agents["codex"].model_copy(update={"verified": True})
    with pytest.raises(PermissionError):  # enabler alone cannot replace a successful handshake
        queue.claim(job, "codex")
    verify_cli(queue)
    claimed = queue.claim(job, "codex")
    queue.config.privacy = queue.config.privacy.model_copy(update={"project_providers": ()})
    with pytest.raises(PermissionError):
        complete(queue, claimed, {"unexpected": "consent revoked"})


def test_a08_stdout_only_and_a11_cli_capacity(queue, inline_worker):
    verify_cli(queue)
    job = enqueue(queue, "audit", kind="audit", allowed=["codex"])
    work = enqueue(queue, "management", allowed=["codex"])
    launches = []
    async def stdout_only(claim, provider):
        launches.append((claim["id"], provider))
        return None
    async def run():
        watcher = Watchdog(queue, PROJECT, stdout_only, worker=inline_worker)
        await watcher.tick()
        await asyncio.gather(*watcher.running)
    asyncio.run(run())
    assert len(launches) == 1
    used = launches[0][0]
    assert used in (job, work)
    assert queue.get(used)["state"] == "RETRY_WAIT"
    assert queue.get(used)["error_class"] == "FORMAT"


def test_pause_cancellation_duplicate_keys_and_artifact_retention(queue):
    job = enqueue(queue, "m")
    assert job == enqueue(queue, "m")
    with pytest.raises(ValueError):
        queue.enqueue(PROJECT, "minutes", "management", "m", {"different": True}, POLICY)
    claim = queue.claim(job, "muse")
    queue.set_paused(PROJECT, True)
    with pytest.raises(PermissionError):
        complete(queue, claim, {})
    queue.set_paused(PROJECT, False)
    queue.cancel(job, "Human revised")
    with pytest.raises(PermissionError):
        complete(queue, claim, {})
    next_job = enqueue(queue, "m2")
    ref = complete(queue, queue.claim(next_job, "muse"), {"minutes": ["fixture"]})
    queue.clock.advance(32 * 86400)
    queue.maintenance(PROJECT)
    with pytest.raises(ValueError, match="expired"):
        queue.artifact(ref)
    assert queue.get(next_job)["result_ref"] == ref


def test_delayed_callback_or_failure_cannot_advance_workflow(queue):
    job = enqueue(queue, "slow")
    lease = queue.claim(job, "muse")
    def delayed(db, job, provider, data):
        queue.clock.advance(1000)
    with pytest.raises(PermissionError):
        queue.complete(job, "muse", lease["lease_token"], {}, on_submit=delayed)
    assert queue.get(job)["result_ref"] is None
    queue.maintenance(PROJECT)
    assert queue.get(job)["state"] == "RETRY_WAIT"


def test_failed_probe_keeps_fallback_available(queue):
    verify_cli(queue)
    job = enqueue(queue, "audit", kind="audit")
    failure(queue, queue.claim(job, "codex"), "CAPABILITY")
    delay = next(h["retry_at"] for h in queue.status(PROJECT)["health"] if h["provider"] == "codex")
    queue.clock.advance(delay - queue.clock())
    queue.maintenance(PROJECT)
    probe = queue.list_jobs(PROJECT, kinds=["probe"], provider="codex")[0]
    failure(queue, queue.claim(probe["id"], "codex"), "AUTH")
    assert queue.get(probe["id"])["state"] == "FAILED"
    assert queue.claim(job, "antigravity")["provider"] == "antigravity"
