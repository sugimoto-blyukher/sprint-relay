from dataclasses import replace
from types import SimpleNamespace

import pytest

from relay.changes import ChangeManager
from relay.core import Task, validate_tasks


def task(key, **changes):
    base = Task(key, "Build " + key, "Initial scope", "backend", 2, 2,
                assignee="owner", acceptance=["Returns a CSV"], deliverables=["Code"],
                requirement_ids=["REQ-001"], repo="team/product")
    return replace(base, **changes)


class Ledger:
    def __init__(self, tasks):
        self.data = {t.key: t for t in tasks}
        self.notices = {}

    def list_tasks(self):
        return list(self.data.values())

    def publish_changes(self, new, updates, *, operation_id=None):
        records = dict(self.data)
        for t in new:
            records.setdefault(t.key, replace(t, issue=len(records) + 1))
        for before, after in updates:
            assert records[before.key] in (before, after)
            records[before.key] = after
        validate_tasks(list(records.values()))
        self.data = records

    def update(self, task, expected):
        self.publish_changes([], [(expected, task)])

    def notify(self, event, message):
        self.notices[event] = message


def test_changes_by_status_and_retry(tmp_path):
    old = [task(key) for key in ["todo", "active", "review", "done", "obsolete"]]
    live = [replace(t, status=status, issue=i) for i, (t, status) in
            enumerate(zip(old, ["Todo", "In Progress", "In Review", "Done", "Todo"]), 1)]
    gh = Ledger(live)
    manager = ChangeManager(tmp_path / "state", SimpleNamespace(approvers={"lead"}, members={}), gh)
    new = [replace(t, title="Revised " + t.key) for t in old[:-1]]
    published = manager.reconcile("m", "1", old, new)
    assert gh.data["todo"].title == new[0].title
    assert gh.data["active"].title == old[1].title
    assert gh.data["done"] == live[3]
    follow = next(t for t in published if t.key.startswith("done-follow"))
    assert follow.issue != live[3].issue and follow.dependencies == ["done"]
    assert len(gh.data) == 6
    assert manager.reconcile("m", "1", old, new) == published
    pending = {v["kind"]: v for v in manager.pending("m")}
    assert set(pending) == {"owner", "review", "cancel"}
    with pytest.raises(PermissionError):
        manager.resolve(pending["owner"]["id"], "lead")
    manager.resolve(pending["owner"]["id"], "owner")
    manager.resolve(pending["cancel"]["id"], "lead")
    assert gh.data["active"].title == new[1].title
    assert gh.data["obsolete"].status == "Cancelled"
    assert len(gh.notices) == 3
    manager.reconcile("m", "2", new, new)
    assert len(gh.data) == 6
    assert manager.pending("m")[0]["kind"] == "review"
    with pytest.raises(ValueError):
        manager.resolve(pending["review"]["id"], "owner")


def test_role_transfer_rejected_before_writes_and_deadline_needs_decision(tmp_path):
    old = [task("export")]
    gh = Ledger(old)
    manager = ChangeManager(tmp_path / "state", SimpleNamespace(approvers={"lead"}), gh)
    with pytest.raises(ValueError, match="new task key"):
        manager.preview("m", "bad", old, [replace(old[0], role="design")])
    new = [replace(old[0], deadline="2030-02-01T09:30:00+09:00")]
    manager.reconcile("m", "ok", old, new)
    assert gh.data["export"].deadline is None
    [change] = manager.pending("m")
    manager.resolve(change["id"], "lead")
    assert gh.data["export"].deadline == new[0].deadline


def test_retry_reclassifies_task_that_started_during_approval(tmp_path, monkeypatch):
    from relay.github import PlanConflict

    old = [task("export")]
    new = [replace(old[0], title="Updated")]
    gh = Ledger(old)
    manager = ChangeManager(tmp_path / "state", SimpleNamespace(approvers={"lead"}), gh)
    publish = gh.publish_changes

    def concurrent_start(*args, **kwargs):
        gh.data["export"] = replace(old[0], status="In Progress")
        raise PlanConflict("CAS conflict")

    monkeypatch.setattr(gh, "publish_changes", concurrent_start)
    with pytest.raises(PlanConflict):
        manager.reconcile("m", "rev", old, new)
    monkeypatch.setattr(gh, "publish_changes", publish)
    manager.reconcile("m", "rev", old, new)
    assert gh.data["export"].title == old[0].title
    assert manager.pending("m")[0]["kind"] == "owner"


def test_physical_followup_title_limit_checked_in_change_preview(tmp_path):
    item = task("x")
    gh = Ledger([replace(item, status="Done", issue=1)])
    manager = ChangeManager(tmp_path / "state", SimpleNamespace(approvers={"lead"}), gh)
    # Logical title fits exactly, but the physical follow-up issue key is longer.
    revised = replace(item, title="x" * 252)
    validate_tasks([revised])
    with pytest.raises(ValueError, match="rendered GitHub issue"):
        manager.preview("meeting", "approved-change", [item], [revised])
    assert len(gh.data) == 1
