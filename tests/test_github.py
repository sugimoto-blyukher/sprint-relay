import base64
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from relay.config import Config
from relay.core import Task
from relay.github import Conflict, GitHub, GitHubError


class FakeAPI:
    """Exercise REST/GraphQL payloads and simulate a lost response after an issue POST."""
    def __init__(self):
        self.files = {}
        self.issues = {}
        self.comments = {}
        self.calls = []
        self.private = True
        self.lose_response = False
        self.concurrent_change = False
        self.project_error = False

    def __call__(self, method, path, data=None):
        self.calls.append((method, path, deepcopy(data)))
        route = path.split("?")[0]
        if route == "/graphql":
            if self.project_error:
                return {"errors": [{"type": "FORBIDDEN"}]}
            query = data["query"]
            if "addProjectV2ItemById" in query:
                return {"data": {"addProjectV2ItemById": {"item": {"id": "item"}}}}
            if "fields(first:" in query:
                fields = [{"name": "Status", "id": "status", "options": [{"id": "todo", "name": "Todo"}]},
                          {"name": "Start date", "id": "start", "dataType": "DATE"}]
                return {"data": {"node": {"fields": {"nodes": fields}}}}
            return {"data": {}}
        if route == "/repos/owner/manage":
            return {"private": self.private}
        if "/contents/" in route:
            if method == "GET":
                if route not in self.files:
                    raise GitHubError(404, "no file")
                return self.files[route]
            previous = self.files.get(route, {})
            if self.concurrent_change or data.get("sha") != previous.get("sha"):
                raise GitHubError(409, "stale")
            self.files[route] = {"content": data["content"], "sha": str(int(previous.get("sha", 0)) + 1)}
            return {"content": self.files[route]}
        if route.endswith("/actions/runs"):
            return {"workflow_runs": [{"id": 123, "html_url": "https://example.org/check"}]}
        if route.endswith("/comments"):
            return deepcopy(self.comments.get(int(route.split("/")[-2]), []))
        if route.endswith("/issues"):
            if method == "GET":
                return deepcopy(list(self.issues.values()))
            number = len(self.issues) + 1
            issue = {**data, "number": number, "state": "open", "node_id": f"node-{number}",
                     "assignees": [{"login": login} for login in data["assignees"]]}
            self.issues[number] = issue
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError("lost POST response")
            return deepcopy(issue)
        if "/issues/" in route:
            issue = self.issues[int(route.split("/")[-1])]
            if method == "PATCH":
                issue.update(data)
                if "assignees" in data:
                    issue["assignees"] = [{"login": login} for login in data["assignees"]]
            return deepcopy(issue)
        raise AssertionError((method, path, data))


def task(key="T1", **kw):
    return replace(Task(key, "Write docs", "Explain the tool", "backend", 1, 1,
                        requirement_ids=["REQ-1"], deliverables=["guide"], acceptance=["builds"]), **kw)


@pytest.fixture
def service(tmp_path):
    api = FakeAPI()
    cfg = Config({"1": "backend", "2": "design"}, {"2"}, {"1": "Alice", "2": "Bob"},
                 "owner/product", "owner/manage", {})
    return GitHub(cfg, "", tmp_path / "github.db", request=api), api


def test_publish_recovers_after_lost_issue_post(service):
    svc, api = service
    api.lose_response = True
    with pytest.raises(TimeoutError):
        svc.publish([task()])
    result = svc.publish([task()])[0]
    assert result == svc.publish([task()])[0]
    assert len(api.issues) == 1
    assert result.issue == 1 and result.assignee == "1"


def test_invalid_graph_creates_neither_issue_nor_reservation(service):
    svc, api = service
    with pytest.raises(ValueError, match="cycle"):
        svc.publish([task(dependencies=["T2"]), task("T2", dependencies=["T1"])])
    assert not api.files and not api.issues


def test_publish_changes_validates_final_graph_atomically(service):
    svc, api = service
    a, b = svc.publish([task("A", dependencies=["B"]), task("B")])
    with pytest.raises(ValueError, match="cycle"):
        svc.update(replace(b, dependencies=["A"]), b)
    c, revised_b, revised_a = svc.publish_changes([task("C")], [
        (b, replace(b, dependencies=["A"])), (a, replace(a, dependencies=["C"]))])
    assert revised_b.dependencies == ["A"]
    assert revised_a.dependencies == ["C"]
    assert c.issue and len(api.issues) == 3


def test_closure_requires_acceptance_and_comments_are_idempotent(service):
    svc, api = service
    svc.publish([task()])
    api.issues[1]["state"] = "closed"
    api.comments[1] = [
        {"id": 1, "body": "!relay done pretend", "user": {"login": "stranger"}},
        {"id": 2, "body": "!relay start", "user": {"login": "alice"}},
        {"id": 3, "body": "!relay review https://example.org/pull/7", "user": {"login": "ALICE"}},
        {"id": 4, "body": "!relay done skip", "user": {"login": "Alice"}},
    ]
    svc.poll()
    assert api.issues[1]["state"] == "open"
    assert svc.tasks()[0].status == "In Review"
    notices = svc.notifications()
    assert len(notices) == 3  # review, rejected command and CI
    for key, _ in notices:
        svc.ack(key)
    svc.poll()
    assert svc.notifications() == []
    api.comments[1].append({"id": 5, "body": "!relay done reviewed documentation",
                            "user": {"login": "Bob"}})
    svc.poll()
    assert svc.tasks()[0].status == "Done" and api.issues[1]["state"] == "closed"
    assert "https://example.org/pull/7" in api.issues[1]["body"]


def test_stale_updates_and_blob_conflicts_leave_current_state(service):
    svc, api = service
    t = svc.publish([task()])[0]
    progress = svc.action(t.key, "start", "1")
    with pytest.raises(Conflict):
        svc.update(replace(t, title="old edit"), expected=t)
    api.concurrent_change = True
    with pytest.raises(Conflict):
        svc.update(replace(progress, title="concurrent"), expected=progress)
    assert svc.tasks()[0] == progress


def test_idempotent_private_spec_archive(service):
    svc, api = service
    digest = "a" * 64
    api.private = False
    with pytest.raises(ValueError, match="private"):
        svc.archive_spec("meeting", digest, "approved")
    api.private = True
    svc.archive_spec("meeting", digest, "approved")
    svc.archive_spec("meeting", digest, "approved")
    with pytest.raises(Conflict):
        svc.archive_spec("meeting", digest, "changed")
    assert len(api.files) == 1


def test_project_fallback_and_retry(service):
    svc, api = service
    svc.config.project_id = "project"
    api.project_error = True
    created = svc.publish([task()])[0]
    assert created.issue and "Projects sync delayed" in svc.notifications()[0][1]
    api.project_error = False
    svc.poll()
    gql = [data for _, path, data in api.calls if path == "/graphql"]
    assert any(data["variables"].get("v") == {"singleSelectOptionId": "todo"} for data in gql)
    count = len(gql)
    svc.poll()
    assert sum(path == "/graphql" for _, path, _ in api.calls) == count


def test_start_checks_dependencies_and_design_claims(service):
    svc, api = service
    svc.publish([task("A"), task("B", dependencies=["A"], role="design")])
    assert svc.action("B", "claim", "2").assignee == "2"
    with pytest.raises(ValueError, match="dependencies"):
        svc.action("B", "start", "2")
    svc.action("A", "cancel", "2", "obsolete")
    with pytest.raises(ValueError, match="cancelled"):
        svc.action("B", "start", "2")
