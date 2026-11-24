"""The complete agent capability surface. Nothing here accepts paths or commands."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


REPORT_REFERENCE_LIMIT = 50
DOCUMENT_LIMIT = 120_000
REPORT_REFERENCES = (f"Use up to {REPORT_REFERENCE_LIMIT} strings copied from get_patrol_delta for this snapshot: "
                     "changes[].id or an ID in completed_audits. Use [] if there are no citations.")

ID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$")]
Repo = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=201)]
Text = Annotated[str, Field(min_length=1, max_length=2048, pattern=r"\S")]
Short = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
Markdown = Annotated[str, Field(min_length=1, max_length=DOCUMENT_LIMIT, pattern=r"\bREQ-[0-9]+\b")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SHA = Annotated[str, Field(pattern=r"^([0-9a-f]{40}|[0-9a-f]{64})$")]
Texts = Annotated[list[Text], Field(max_length=128)]
TaskKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")]
RequirementID = Annotated[str, Field(pattern=r"^REQ-[0-9]+$", max_length=160)]
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
    key: TaskKey
    title: Short
    description: Text
    role: Role
    estimated_hours: Annotated[float, Field(ge=1, le=4, allow_inf_nan=False)]
    priority: int = 0
    dependencies: Annotated[list[TaskKey], Field(max_length=128)]
    requirement_ids: Annotated[list[RequirementID], Field(min_length=1, max_length=128)]
    deliverables: Annotated[Texts, Field(min_length=1)]
    acceptance: Annotated[Texts, Field(min_length=1)]


# The patrol domain rejects blank or NUL-containing prose.
PATROL_TEXT_PATTERN = r"^[^\x00]*[^\s\x00][^\x00]*$"
PatrolShort = Annotated[Short, Field(pattern=PATROL_TEXT_PATTERN)]


class Finding(Record):
    evidence_id: ID
    category: Literal["correctness", "compatibility", "test_coverage", "ci_failure", "feasibility"]
    severity: Literal["low", "medium", "high"]
    summary: PatrolShort
    impact: PatrolShort
    acceptance: PatrolShort


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


class AcceptedFindings(Record):
    forced: Literal[True]
    reason: str
    blocking_questions: list[str]


class WorkflowContext(Record):
    meeting: str
    state: str
    source: str | None = None
    digest: str | None = None
    markdown: str | None = None
    minutes: dict[str, JsonValue] | None = None
    approval: AcceptedFindings | None = None
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
    "submit_specification": (inp("SubmitSpecification", Lease, markdown=Markdown,
                                 evidence=Annotated[Texts, Field(min_length=1)]), WorkflowResult),
    "submit_validation": (inp("SubmitValidation", Lease, digest=Digest, checks=Checks, blocking_questions=Texts,
                              minor_revision=(Markdown | None, None), audit_job_id=(ID | None, None)), WorkflowResult),
    "submit_task_plan": (inp("SubmitTaskPlan", Lease, digest=Digest,
                             tasks=Annotated[list[TaskProposal], Field(min_length=1, max_length=200)]), WorkflowResult),
    "claim_patrol": (inp("ClaimPatrol", Project, slot_id=(ID, "current")), LeaseOut),
    "get_patrol_delta": (inp("PatrolDelta", Lease, cursor=(Cursor, 0), limit=(Limit, 50)),
                         out("Delta", changes=list[JsonValue], overdue=list[JsonValue], blocked=list[JsonValue],
                             remaining_hours=JsonValue, pending_jobs=list[JsonValue], completed_audits=list[str],
                             next_cursor=int | None, snapshot_cursor=str)),
    "get_tasks": (inp("ListTasks", Project, area=(Role | None, None), status=(Status | None, None),
                      cursor=(Cursor, 0), limit=(Limit, 50)), out("Tasks", tasks=list[dict[str, JsonValue]],
                                                                 next_cursor=int | None)),
    "get_schedule": (Project, out("Schedule", tasks=list[dict[str, JsonValue]], duration_hours=float | None,
                                 finish=str | None, critical_paths=list[list[str]], risks=list[str])),
    "get_progress": (Project, out("Progress", counts=dict[str, int], remaining_hours=float, unknown_estimates=int,
                                 blockers=list[str])),
    "request_audit": (inp("RequestAudit", Lease, kind=Literal["pr", "ci", "spec"], repo=Repo, target=ID,
                          sha=SHA, question=PatrolShort), out("AuditRequest", job_id=str, deduplicated=bool)),
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
    "publish_patrol_report": (inp("Report", Lease, summary=Annotated[Text, Field(pattern=PATROL_TEXT_PATTERN)],
                                  references=Annotated[list[ID], Field(max_length=REPORT_REFERENCE_LIMIT,
                                                                       description=REPORT_REFERENCES)]),
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

# Local CLI jobs are already claimed. Discovery/claim operations for Muse are
# deliberately absent. Patrol retains its finding-backed follow-up actions.
PROBE_TOOLS = ("get_project_context", "heartbeat_job")
JOB_TOOLS = {
    "minutes": ("get_transcript", "submit_minutes"),
    "specification": ("get_specification", "submit_specification"),
    "validation": ("get_specification", "request_audit", "submit_validation"),
    "task_plan": ("get_specification", "submit_task_plan"),
    "patrol": ("get_patrol_delta", "get_tasks", "get_progress", "get_schedule", "request_audit",
               "get_audit_result", "propose_or_create_issue", "post_review_comment",
               "update_task_priority", "publish_patrol_report", "complete_patrol"),
    "audit": ("get_audit_context", "submit_audit_result"),
}


def codex_tools(role, kind, *, spec_audit=False):
    if spec_audit and kind != "audit":
        raise ValueError("Only specification audits can read the full audited revision")
    if role not in ALLOWED:
        raise ValueError("Unknown job role")
    if kind == "probe":
        return PROBE_TOOLS
    if kind not in JOB_TOOLS or (role == "audit") != (kind == "audit"):
        raise ValueError("Unexpected job kind for role")
    selected = (*PROBE_TOOLS, "report_job_failure", *JOB_TOOLS[kind])
    if spec_audit:
        selected += ("get_specification",)
    if not ALLOWED[role].issuperset(selected):
        raise ValueError("Unexpected tool for role")
    return selected


TOOL_DESCRIPTIONS = {
    "get_project_context": "Read project constraints and the assigned job kind, job ID and lease.",
    "heartbeat_job": "Renew the active lease while processing the assigned job.",
    "report_job_failure": "Report a classified failure for the assigned job and stop.",
    "get_transcript": "Read the consented transcript of the assigned meeting.",
    "submit_minutes": "Submit extracted minutes with exact transcript quotes to finish this job.",
    "get_specification": "Read the assigned meeting's minutes or current revision and digest.",
    "submit_specification": "Submit a draft with stable requirement IDs and evidence for human review.",
    "submit_validation": "Submit checks for the reviewed digest and flag questions for human resolution.",
    "submit_task_plan": "Submit requirement-linked tasks for the current human-approved digest.",
    "get_patrol_delta": "Read the next page of the assigned patrol. Keep its snapshot_cursor for completion.",
    "get_tasks": "Read paginated tasks and their current versions within the authorized scope.",
    "get_progress": "Read current task counts, remaining effort and blockers.",
    "get_schedule": "Read scheduled tasks, critical paths and risks.",
    "request_audit": ("Queue an audit. For pr/ci use the PR or run number (1–16 digits) and pinned 40-hex SHA. "
                      "For spec use the assigned 20-hex meeting ID and 64-hex revision digest."),
    "get_audit_result": "Read a completed audit and its finding IDs for patrol follow-up.",
    "propose_or_create_issue": "Record a repair issue for an eligible finding and current target.",
    "post_review_comment": "Post a finding-backed comment on the audited PR revision.",
    "update_task_priority": "Adjust a linked task's priority using its current version and finding.",
    "publish_patrol_report": "Persist the patrol summary before completing the checkpoint. " + REPORT_REFERENCES,
    "complete_patrol": "Finish the assigned patrol with its processed snapshot_cursor after reporting.",
    "get_audit_context": "Read the assigned pinned evidence and IDs to cite in audit findings.",
    "submit_audit_result": "Finish the assigned audit with findings tied to its evidence IDs.",
    "list_workflow_jobs": "List eligible workflow jobs for the current project.",
    "claim_job": "Claim an eligible workflow job and obtain its lease.",
    "claim_patrol": "Claim the current patrol slot and obtain its lease.",
    "get_audit_status": "Read the state of an authorized audit job.",
}

# Error details cross the MCP boundary. Only a declared top-level parameter and
# a closed category are useful here. Pydantic locations can contain user keys.
INPUT_REASONS = {"REQUIRED", "UNKNOWN_FIELD", "TYPE", "LIMIT", "FORMAT", "VALUE",
                 "UNKNOWN_REFERENCE", "READ_CONTEXT_FIRST", "DUPLICATE"}


class InputProblem(ValueError):
    def __init__(self, field, reason):
        super().__init__(reason)
        self.field, self.reason = field, reason

    @classmethod
    def from_validation(cls, model, exc):
        error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        location = error["loc"]
        field = location[0] if location and location[0] in model.model_fields else "arguments"
        kind = error["type"]
        if kind in {"missing", "extra_forbidden"}:
            reason = "REQUIRED" if kind == "missing" else "UNKNOWN_FIELD"
        elif kind.endswith("_type"):
            reason = "TYPE"
        elif kind.endswith(("too_long", "too_short")) or kind.startswith(("greater_than", "less_than")):
            reason = "LIMIT"
        elif kind in {"string_pattern_mismatch", "literal_error"}:
            reason = "FORMAT"
        else:
            reason = "VALUE"
        return cls(field, reason)


def input_model(name, *, codex_job):
    if codex_job:
        if name == "get_project_context":
            return Project
        if name == "heartbeat_job":
            return Lease
    return T[name][0]
