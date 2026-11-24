"""The complete agent capability surface. Nothing here accepts paths or commands."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


ID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$")]
Repo = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=201)]
Text = Annotated[str, Field(min_length=1, max_length=2048)]
Short = Annotated[str, Field(min_length=1, max_length=600)]
Markdown = Annotated[str, Field(min_length=1, max_length=1_048_576)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SHA = Annotated[str, Field(pattern=r"^([0-9a-f]{40}|[0-9a-f]{64})$")]
Texts = Annotated[list[Text], Field(max_length=128)]
Refs = Annotated[list[ID], Field(max_length=128)]
Cursor = Annotated[int, Field(ge=0, le=10_000_000)]
Limit = Annotated[int, Field(ge=1, le=100)]
Status = Literal["Todo", "In Progress", "In Review", "Blocked", "Done", "Cancelled"]
Role = Literal["frontend", "backend", "design", "hardware"]
Kind = Literal["minutes", "specification", "validation", "task_plan", "patrol"]


class Project(Record):
    project_id: Repo


class Job(Project):
    job_id: ID


class Lease(Job):
    lease_token: Annotated[str, Field(min_length=1, max_length=256)]


class Decision(Record):
    text: Text
    quote: Text
    rationale: Text
    impact: Text


class Minutes(Record):
    summary: Text
    decisions: Annotated[list[Decision], Field(max_length=128)]
    proposals: Texts
    open_questions: Texts


class Checks(Record):
    technical: Text
    logical: Text
    cross_team: Text
    completeness: Text
    feasibility: Text
    traceability: Text


class TaskProposal(Record):
    key: ID
    title: Short
    description: Text
    role: Role
    estimated_hours: Annotated[float, Field(ge=1, le=4, allow_inf_nan=False)]
    priority: int = 0
    dependencies: Refs
    requirement_ids: Refs
    deliverables: Texts
    acceptance: Texts


class Finding(Record):
    evidence_id: ID
    category: Literal["correctness", "compatibility", "test_coverage", "ci_failure", "feasibility"]
    severity: Literal["low", "medium", "high"]
    summary: Short
    impact: Short
    acceptance: Short


def model(name, base, **fields):
    return create_model(name, __base__=base, **fields)


class ContextOut(Record):
    project_id: str
    roles: list[str]
    constraints: list[str]
    job: dict[str, str] | None = None
    probe: dict[str, str | float] | None = None


class JobSummary(Record):
    job_id: str
    kind: str
    state: str


class WorkflowResult(Record):
    meeting: str
    state: str
    digest: str | None = None
    job_id: str | None = None


class WorkflowContext(Record):
    meeting: str
    state: str
    source: str | None = None
    digest: str | None = None
    markdown: str | None = None
    minutes: dict[str, JsonValue] | None = None
    approval: dict[str, JsonValue] | None = None
    audit_result: dict[str, JsonValue] | None = None


class LeaseOut(Record):
    job_id: str
    lease_token: str
    input_ref: str
    deadline: float
    kind: str
    snapshot_cursor: str | None = None


def out(name, **fields):
    return model(name, Record, **{k: (v, ...) for k, v in fields.items()})


def inp(name, base, **fields):
    return model(name, base, **{k: (v if isinstance(v, tuple) else (v, ...)) for k, v in fields.items()})


# Dynamic field lists keep the small command contracts auditable together. The
# public JSON schemas include required fields, enum values and wire limits.
T = {
    "get_project_context": (inp("ProjectContext", Project, probe_answer=(Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None, None)), ContextOut),
    "list_workflow_jobs": (inp("ListJobs", Project, cursor=(ID | None, None), limit=(Limit, 50),
                               kinds=(list[Kind], [])), out("Jobs", jobs=list[JobSummary], next_cursor=str | None)),
    "claim_job": (Job, LeaseOut),
    "heartbeat_job": (inp("Heartbeat", Lease, probe_answer=(Short | None, None)),
                      out("HeartbeatReceipt", deadline=float | None, completed=bool)),
    "get_transcript": (Lease, out("Transcript", transcript=str, source=str)),
    "submit_minutes": (inp("SubmitMinutes", Lease, minutes=Minutes), WorkflowResult),
    "get_specification": (Lease, WorkflowContext),
    "submit_specification": (inp("SubmitSpecification", Lease, markdown=Markdown, evidence=Texts), WorkflowResult),
    "submit_validation": (inp("SubmitValidation", Lease, digest=Digest, checks=Checks, blocking_questions=Texts,
                              minor_revision=(Markdown | None, None), audit_job_id=(ID | None, None)), WorkflowResult),
    "submit_task_plan": (inp("SubmitTaskPlan", Lease, digest=Digest,
                             tasks=Annotated[list[TaskProposal], Field(min_length=1, max_length=200)]), WorkflowResult),
    "claim_patrol": (inp("ClaimPatrol", Project, slot_id=(ID, "current")), LeaseOut),
    "get_patrol_delta": (inp("PatrolDelta", Lease, cursor=(Cursor, 0), limit=(Limit, 50)),
                         out("Delta", changes=list[JsonValue], overdue=list[JsonValue], blocked=list[JsonValue],
                             remaining_hours=JsonValue, pending_jobs=list[JsonValue], completed_audits=list[JsonValue],
                             next_cursor=int | None, snapshot_cursor=str)),
    "get_tasks": (inp("ListTasks", Project, area=(Role | None, None), status=(Status | None, None),
                      cursor=(Cursor, 0), limit=(Limit, 50)), out("Tasks", tasks=list[dict[str, JsonValue]],
                                                                 next_cursor=int | None)),
    "get_schedule": (Project, out("Schedule", tasks=list[dict[str, JsonValue]], duration_hours=float | None,
                                 finish=str | None, critical_paths=list[list[str]], risks=list[str])),
    "get_progress": (Project, out("Progress", counts=dict[str, int], remaining_hours=float, unknown_estimates=int,
                                 blockers=list[str])),
    "request_audit": (inp("RequestAudit", Lease, kind=Literal["pr", "ci", "spec"], repo=Repo, target=ID,
                          sha=SHA, question=Short), out("AuditRequest", job_id=str, deduplicated=bool)),
    "get_audit_status": (inp("AuditStatus", Project, audit_id=ID),
                         out("AuditState", job_id=str, state=str, created_at=float, finished_at=float | None)),
    "get_audit_context": (Lease, out("AuditContext", job_id=str, kind=str, repo=str, target=str, sha=str,
                                    question=str, evidence=list[dict[str, JsonValue]], truncated=bool)),
    "submit_audit_result": (inp("AuditResult", Lease, findings=Annotated[list[Finding], Field(max_length=15)]),
                            out("AuditReceipt", result_id=str, audit_id=str, sha=str, findings=list[dict[str, JsonValue]])),
    "get_audit_result": (inp("GetResult", Project, audit_id=ID),
                         out("StoredResult", result_id=str, audit_id=str, sha=str, findings=list[dict[str, JsonValue]])),
    "propose_or_create_issue": (inp("Repair", Lease, audit_id=ID, finding_id=ID),
                                out("IssueReceipt", receipt=str, status=str, repo=str, number=int | None)),
    "post_review_comment": (inp("ReviewComment", Lease, audit_id=ID, finding_id=ID),
                            out("CommentReceipt", receipt=str, status=str, repo=str, number=int | None)),
    "update_task_priority": (inp("UpdatePriority", Lease, audit_id=ID, finding_id=ID, task_id=ID,
                                 expected_version=Digest, priority=Annotated[int, Field(ge=-100, le=100)], reason=Short),
                             out("PriorityReceipt", receipt=str, status=str)),
    "publish_patrol_report": (inp("Report", Lease, summary=Text, references=Refs),
                              out("ReportReceipt", receipt=str, status=str)),
    "complete_patrol": (inp("CompletePatrol", Lease, snapshot_cursor=ID), out("Checkpoint", checkpoint=str)),
    "report_job_failure": (inp("ReportFailure", Lease, reason=Literal["invalid_input", "permission", "conflict",
                                "timeout", "quota", "auth", "unavailable", "dependency", "invalid_output"]),
                           out("FailureReceipt", state=str)),
}

SHARED = {"get_project_context", "heartbeat_job", "get_specification", "get_tasks", "get_schedule",
          "get_progress", "get_audit_status", "report_job_failure"}
AUDIT_ONLY = {"get_audit_context", "submit_audit_result"}
ALLOWED = {"management": set(T) - AUDIT_ONLY, "audit": SHARED | AUDIT_ONLY}
