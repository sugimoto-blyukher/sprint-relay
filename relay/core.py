"""Task records and deterministic, side-effect-free workflow rules."""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
from urllib.parse import urlsplit

from .config import Config, ROLES
from .issue_format import render_issue

STATUSES = ("Todo", "In Progress", "In Review", "Blocked", "Done", "Cancelled")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


@dataclass
class Task:
    key: str
    title: str
    description: str
    role: str
    estimated_hours: float
    remaining_hours: float | None = None
    dependencies: list[str] = field(default_factory=list)
    requirement_ids: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    assignee: str | None = None
    status: str = "Todo"
    priority: int = 0
    deadline: str | None = None
    baseline_start: str | None = None
    baseline_end: str | None = None
    forecast_start: str | None = None
    forecast_end: str | None = None
    actual_start: str | None = None
    actual_end: str | None = None
    repo: str | None = None
    issue: int | None = None
    metrics: dict = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    block_reason: str | None = None
    resume_status: str | None = None
    decision_reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        if not isinstance(data, dict):
            raise ValueError("task must be an object")
        unknown = data.keys() - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown task fields: {', '.join(sorted(unknown))}")
        values = dict(data)
        values.setdefault("remaining_hours", values.get("estimated_hours"))
        try:
            return cls(**values)
        except TypeError as exc:
            raise ValueError(f"invalid task fields: {exc}") from exc

    def to_dict(self) -> dict:
        return asdict(self)


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _url(value) -> str:
    if not _nonempty(value) or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError("provide a deliverable, design or pull-request HTTPS URL")
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("provide a deliverable, design or pull-request HTTPS URL")
    return value


def validate_task(task: Task) -> None:
    """Check a single stored record without assuming other records have loaded."""
    if not isinstance(task.key, str) or not IDENTIFIER.fullmatch(task.key):
        raise ValueError("invalid task key")
    if not _nonempty(task.title) or not _nonempty(task.description):
        raise ValueError(f"{task.key}: title and description required")
    if task.role not in ROLES or task.status not in STATUSES:
        raise ValueError(f"{task.key}: invalid role or status")
    if not _number(task.estimated_hours) or not 1 <= task.estimated_hours <= 4:
        raise ValueError(f"{task.key}: estimated effort must be 1–4 hours")
    if task.remaining_hours is not None and (
        not _number(task.remaining_hours) or task.remaining_hours < 0
    ):
        raise ValueError(f"{task.key}: remaining effort must be nonnegative or unknown")
    if type(task.priority) is not int:
        raise ValueError(f"{task.key}: priority must be an integer")
    for name in ("dependencies", "requirement_ids", "deliverables", "acceptance", "links"):
        values = getattr(task, name)
        if not isinstance(values, list) or not all(_nonempty(v) for v in values):
            raise ValueError(f"{task.key}: {name} must be a list of nonempty strings")
        if name in ("requirement_ids", "deliverables", "acceptance") and not values:
            raise ValueError(f"{task.key}: {name} required")
        if len(values) != len(set(values)):
            raise ValueError(f"{task.key}: duplicate item in {name}")
    for link in task.links:
        _url(link)
    if task.assignee is not None and not _nonempty(task.assignee):
        raise ValueError(f"{task.key}: invalid assignee")
    if task.issue is not None and (type(task.issue) is not int or task.issue < 1):
        raise ValueError(f"{task.key}: issue must be a positive issue number")
    for name in ("deadline", "baseline_start", "baseline_end", "forecast_start",
                 "forecast_end", "actual_start", "actual_end"):
        value = getattr(task, name)
        if value is not None and (
            not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() is None
        ):
            raise ValueError(f"{task.key}: {name} must include a UTC offset")

    render_issue(task)  # Preflight the same text that will be sent to GitHub.


def validate_tasks(tasks: list[Task]) -> None:
    """Validate the entire dependency graph before any external publication."""
    graph = {}
    for task in tasks:
        validate_task(task)
        if task.key in graph:
            raise ValueError(f"duplicate task key: {task.key}")
        graph[task.key] = task.dependencies
    for key, deps in graph.items():
        missing = set(deps) - graph.keys()
        if missing:
            raise ValueError(f"{key}: unknown dependencies: {', '.join(sorted(missing))}")
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError(f"dependency cycle: {' -> '.join(exc.args[1])}") from exc


def assign_owner(task: Task, config: Config) -> Task:
    if task.role not in ROLES:
        raise ValueError(f"unknown role: {task.role}")
    members = [user for user, role in config.members.items() if role == task.role]
    if task.assignee is not None and task.assignee not in members:
        raise ValueError(f"{task.key}: assignee does not hold {task.role} role")
    owner = task.assignee
    if task.role != "design":
        if len(members) != 1:
            raise ValueError(f"{task.role} requires one configured owner")
        owner = members[0]
    repo = task.repo or config.default_repo
    if repo not in config.product_repos:
        raise ValueError(f"unconfigured product repository: {repo}")
    return replace(task, repo=repo, assignee=owner)


def _rationale(value) -> str:
    if not _nonempty(value):
        raise ValueError("provide a reason or acceptance summary")
    return value.strip()


def _add_link(task: Task, value: str) -> list[str]:
    link = _url(value)
    return list(dict.fromkeys([*task.links, link]))


def apply_action(task: Task, action: str, actor: str, config: Config, value=None, *,
                 now: datetime | None = None) -> Task:
    """Return a new task after checking the actor and transition."""
    actor = str(actor)
    if actor not in config.members and actor not in config.approvers:
        raise PermissionError("unknown actor")
    if action in ("done", "reject", "cancel"):
        if actor not in config.approvers:
            raise PermissionError("configured approver required")
    elif action == "claim":
        if task.role != "design" or config.members.get(actor) != "design":
            raise PermissionError("design tasks can only be claimed by designers")
    elif task.assignee != actor:
        raise PermissionError("task owner required")

    allowed = {
        "claim": ("Todo",), "start": ("Todo",), "review": ("In Progress",),
        "done": ("In Review",), "reject": ("In Review",),
        "block": ("Todo", "In Progress", "In Review"), "unblock": ("Blocked",),
        "estimate": ("Todo", "In Progress", "In Review", "Blocked"),
        "link": ("Todo", "In Progress", "In Review", "Blocked"),
        "cancel": ("Todo", "In Progress", "In Review", "Blocked"),
    }
    if task.status not in allowed.get(action, ()):
        raise ValueError(f"cannot {action} a task in {task.status}")
    instant = now or datetime.now(timezone.utc)
    if instant.utcoffset() is None:
        raise ValueError("action time must include a UTC offset")
    stamp = instant.astimezone(timezone.utc).isoformat()
    if action == "claim":
        if task.assignee not in (None, actor):
            raise ValueError("task already claimed")
        return replace(task, assignee=actor)
    if action == "start":
        return replace(task, status="In Progress", actual_start=task.actual_start or stamp)
    if action == "link":
        return replace(task, links=_add_link(task, value))
    if action == "review":
        links = _add_link(task, value) if value else task.links
        if not links:
            raise ValueError("review requires a deliverable URL")
        return replace(task, status="In Review", links=links)
    if action == "done":
        if not task.links or not task.acceptance:
            raise ValueError("completion requires evidence and acceptance criteria")
        return replace(task, status="Done", remaining_hours=0, actual_end=stamp,
                       decision_reason=_rationale(value))
    if action == "reject":
        return replace(task, status="In Progress", decision_reason=_rationale(value))
    if action == "block":
        return replace(task, status="Blocked", block_reason=_rationale(value),
                       resume_status=task.status)
    if action == "unblock":
        if task.resume_status not in ("Todo", "In Progress", "In Review"):
            raise ValueError("blocked task lacks a valid pre-block status")
        return replace(task, status=task.resume_status, resume_status=None, block_reason=None)
    if action == "cancel":
        return replace(task, status="Cancelled", decision_reason=_rationale(value))
    if value is None or value == "unknown":
        return replace(task, remaining_hours=None)
    if isinstance(value, bool):
        raise ValueError("estimate must be nonnegative hours or unknown")
    try:
        hours = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("estimate must be nonnegative hours or unknown") from exc
    if not math.isfinite(hours) or hours < 0:
        raise ValueError("estimate must be nonnegative hours or unknown")
    return replace(task, remaining_hours=hours)


def new_task(data: dict, config: Config) -> Task:
    """Construct a fresh task from an untrusted proposal, with local authority."""
    proposal_fields = {
        "key", "title", "description", "role", "estimated_hours", "dependencies",
        "requirement_ids", "deliverables", "acceptance", "priority", "deadline", "repo",
    }
    if not isinstance(data, dict) or data.keys() - proposal_fields:
        raise ValueError("task proposal contains a non-editable field")
    task = Task.from_dict(data)
    validate_task(task)
    return assign_owner(task, config)
