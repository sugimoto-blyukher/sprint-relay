from dataclasses import replace
from types import SimpleNamespace

import pytest

from relay.workflow import CHECKS, Workflow


class Issues:
    def __init__(self):
        self.issues = {}
        self.archived = {}
        self.fail = False

    def list_tasks(self):
        return list(self.issues.values())

    def archive_spec(self, meeting, digest, markdown):
        self.archived[digest] = markdown

    def publish(self, tasks):
        for task in tasks:
            self.issues.setdefault(task.key, replace(task, issue=len(self.issues) + 1))
            if self.fail:
                self.fail = False
                raise OSError("Connection reset after issue creation")
        return self.list_tasks()


SPEC = "## Purpose\nShip the prototype\n## Requirements\nREQ-001: export a report.\n"
MINUTES = {"proposals": [], "decisions": ["Export a report"], "open_questions": []}


def proposal(key="export"):
    return {"key": key, "role": "backend", "title": "Export report",
            "description": "Download the approved report.", "estimated_hours": 2,
            "acceptance": ["Export contains the expected headings"],
            "deliverables": ["Report endpoint"], "requirement_ids": ["REQ-001"]}


def report(digest):
    return {"provider": "offline-reviewer", "revision": digest, "processing": "local",
            "checks": {k: "Reviewed the export against the recorded decision" for k in CHECKS},
            "blocking_questions": []}


@pytest.fixture
def flow(tmp_path):
    config = SimpleNamespace(members={"22": "backend"}, approvers={"33"},
                             default_repo="team/product", product_repos=["team/product"])
    return Workflow(tmp_path / "private" / "meeting.sqlite3", config, Issues())


def revised(f):
    meeting = f.import_meeting("discord://fixture/1", "We agreed to export a report.", "22")
    head = f.draft(meeting, "22", markdown=SPEC, minutes=MINUTES)
    return meeting, f.revise(meeting, "22", SPEC + "Use CSV.\n", "Record human decision", based_on=head)


def test_revision_approval_and_recovery(flow):
    meeting, digest = revised(flow)
    assert "Use CSV" in flow.show(meeting, "22")["diff"]
    with pytest.raises(ValueError):
        flow.approve(meeting, "33", digest, tasks=[proposal()])
    flow.validate(meeting, "33", digest, report=report(digest))
    with pytest.raises(PermissionError):
        flow.approve(meeting, "22", digest, tasks=[proposal()])
    flow.github.fail = True
    with pytest.raises(OSError):
        flow.approve(meeting, "33", digest, tasks=[proposal(), proposal("verify")])
    with pytest.raises(ValueError):
        flow.revise(meeting, "22", SPEC, "Revise before publishing")
    tasks = flow.approve(meeting, "33", digest, tasks=[proposal("substitution")])
    assert [(t.key, t.issue) for t in tasks] == [("export", 1), ("verify", 2)]
    assert len(flow.approve(meeting, "33", digest)) == 2


def test_changed_revision_invalidates_review_and_requires_resolution(flow):
    meeting, head = revised(flow)
    blockers = {**report(head), "blocking_questions": ["Which encoding?"]}
    flow.validate(meeting, "33", head, report=blockers)
    with pytest.raises(ValueError):
        flow.approve(meeting, "33", head, tasks=[proposal()])
    digest = flow.revise(meeting, "22", SPEC + "UTF-8", "Resolve encoding", based_on=head)
    for stale in (head, digest):
        with pytest.raises(ValueError):
            flow.approve(meeting, "33", stale, tasks=[proposal()])
    with pytest.raises(ValueError):
        flow.validate(meeting, "33", digest, report=blockers)
    assert not flow.github.archived


def test_validation_rejects_unrevised_or_incomplete_reviews(flow):
    meeting = flow.import_meeting("fixture", "sample", "22")
    head = flow.draft(meeting, "22", markdown=SPEC, minutes=MINUTES)
    with pytest.raises(ValueError):
        flow.validate(meeting, "33", head, report=report(head))
    digest = flow.revise(meeting, "22", SPEC, "Reviewed without changes")
    with pytest.raises(PermissionError):
        flow.validate(meeting, "22", digest, report=report(digest))
    with pytest.raises(ValueError):
        flow.validate(meeting, "33", digest, report={**report(digest), "checks": {}})


def test_no_publication_before_graph_validation(flow):
    meeting, digest = revised(flow)
    flow.validate(meeting, "33", digest, report=report(digest))
    for tasks in ([{**proposal(), "dependencies": ["export"]}], [proposal(), proposal()],
                  [{**proposal(), "status": "Done"}], [{**proposal(), "requirement_ids": ["REQ-999"]}]):
        with pytest.raises(ValueError):
            flow.approve(meeting, "33", digest, tasks=tasks)
    assert flow.github.archived == flow.github.issues == {}


def test_private_import_is_idempotent_and_consent_enforced(flow):
    meeting = flow.import_meeting("source", "sample", "22")
    assert flow.import_meeting("source", "sample", "22") == meeting
    with pytest.raises(ValueError):
        flow.import_meeting("source", "sample", "22", True)
    with pytest.raises(PermissionError):
        flow.draft(meeting, "22")
    with pytest.raises(PermissionError):
        flow.show(meeting, "outsider")
    assert flow.path.stat().st_mode & 0o077 == 0
