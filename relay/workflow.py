"""Private meeting workspace, revision-bound review, and recoverable publication."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path

from .ai import Minutes, Review, TaskPlan
from .core import Task, new_task, validate_tasks

CHECKS = ("technical", "logical", "cross_team", "completeness", "feasibility", "traceability")
MINUTES = ("proposals", "decisions", "open_questions")
REQUIREMENT = re.compile(r"\bREQ-[0-9]+\b")
MAX_TEXT = 120_000


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _digest(*values):
    return hashlib.sha256(_json(values).encode()).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise ValueError(f"Invalid or oversized {name}")
    return value


def _strings(values, name):
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list")
    return [_text(value, name) for value in values]


def _minutes(data):
    if not isinstance(data, dict) or any(k not in data for k in MINUTES):
        raise ValueError("Minutes need proposals, decisions and open_questions")
    return {k: _strings(data[k], k) for k in MINUTES}


def _markdown(value):
    _text(value, "specification")
    if not REQUIREMENT.search(value):
        raise ValueError("Include stable requirement identifiers, e.g. REQ-001")
    return value


def _report(data, digest, providers):
    if not isinstance(data, dict) or data.get("revision") != digest:
        raise ValueError("Review must identify this exact revision digest")
    _text(data.get("provider"), "AI reviewer")
    if data.get("processing") == "local" and data["provider"] in PROVIDERS:
        raise PermissionError("Cloud account providers require explicit participant consent")
    if data.get("processing") != "local" and data.get("provider") not in providers:
        raise PermissionError("External review requires participant consent")
    checks = data.get("checks")
    if not isinstance(checks, dict) or any(k not in checks for k in CHECKS):
        raise ValueError("Review must cover " + ", ".join(CHECKS))
    for key in CHECKS:
        _text(checks[key], key)
    _strings(data.get("blocking_questions"), "blocking_questions")
    return data


@contextmanager
def private_db(path):
    """Caller supplies a private service data directory; no shared temp copies."""
    file = Path(path)
    file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(file, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    if file.stat().st_mode & 0o077:
        raise PermissionError("Workflow state must have mode 0600 or stricter")
    conn = sqlite3.connect(file, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


PROVIDERS = frozenset({"muse", "claude_code", "codex", "antigravity"})
LEGACY_DATE = "1970-01-01T00:00:00+00:00"
STATES_FOR_JOB = {"minutes": "IMPORTED", "specification": "MINUTES_READY",
                  "validation": "VALIDATING", "task_plan": "PLANNING"}


@dataclass(frozen=True)
class PreparedPlan:
    meeting: str
    digest: str
    input_hash: str
    tasks: tuple[Task, ...]


def migrate_workflow(db, project=None):
    """Additive schema. Called inside the migration transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS meeting (
        id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, transcript TEXT NOT NULL,
        consent INTEGER NOT NULL, head TEXT, published TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS revision (
        digest TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, parent TEXT, markdown TEXT NOT NULL,
        minutes TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, report TEXT,
        reviewer TEXT, approved_by TEXT, plan TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_v2 (
        meeting TEXT PRIMARY KEY, project TEXT NOT NULL, state TEXT NOT NULL, providers TEXT NOT NULL,
        minutes TEXT, job_id TEXT, attachment_hash TEXT, input_hash TEXT, imported_at TEXT,
        audit_job TEXT, audit_result TEXT, generation INTEGER NOT NULL DEFAULT 0,
        agent_plan INTEGER NOT NULL DEFAULT 0, UNIQUE(project,attachment_hash))""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_approval (
        digest TEXT PRIMARY KEY, project TEXT NOT NULL, meeting TEXT NOT NULL,
        report_hash TEXT NOT NULL, actor TEXT NOT NULL, approved_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_consent (
        meeting TEXT NOT NULL, project TEXT NOT NULL, actor TEXT NOT NULL,
        recorded_at TEXT NOT NULL, providers TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_key (
        project TEXT NOT NULL, key TEXT NOT NULL, meeting TEXT NOT NULL, PRIMARY KEY(project,key))""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_notice (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, body TEXT NOT NULL)""")

    if project is None:
        return
    previous_factory = db.row_factory
    db.row_factory = sqlite3.Row
    try:
        # Preserve old revisions, digests and actors. A v1 blanket opt-in has
        # no provider allowlist; existing data stays local after migration.
        for row in db.execute("""SELECT m.*, r.report, r.plan, r.parent, r.approved_by
            FROM meeting m LEFT JOIN revision r ON m.head=r.digest
            WHERE NOT EXISTS(SELECT 1 FROM workflow_v2 WHERE meeting=m.id)"""):
            if row["published"] and row["published"] == row["head"]:
                state = "PUBLISHED"
            elif row["approved_by"] and row["plan"]:
                state = "PUBLISHING"
            elif row["report"]:
                state = "NEEDS_REVISION" if json.loads(row["report"])["blocking_questions"] else "VALIDATED"
            elif row["parent"]:
                state = "HUMAN_REVIEWED"
            else:
                state = "DRAFT_READY" if row["head"] else "IMPORTED"
            db.execute("""INSERT INTO workflow_v2(meeting,project,state,providers,input_hash,imported_at)
                          VALUES(?,?,?,'[]',?,?)""",
                       (row["id"], project, state, _digest(row["transcript"]), LEGACY_DATE))
        db.execute("""INSERT OR IGNORE INTO workflow_approval
            SELECT r.digest,v.project,r.meeting_id,'',r.approved_by,NULL FROM revision r
            JOIN workflow_v2 v ON r.meeting_id=v.meeting WHERE r.approved_by IS NOT NULL""")
        for row in db.execute("""SELECT r.digest,r.report FROM revision r JOIN workflow_approval a
                ON a.digest=r.digest WHERE a.report_hash='' AND r.report IS NOT NULL"""):
            db.execute("UPDATE workflow_approval SET report_hash=? WHERE digest=?",
                       (_digest(json.loads(row["report"])), row["digest"]))
    finally:
        db.row_factory = previous_factory


class Workflow:
    def __init__(self, path, config, github, *, changes=None, jobs=None, project=None):
        self.path, self.config, self.github = path, config, github
        self.changes, self.jobs = changes, jobs
        self.project = project or getattr(config, "management_repo", config.default_repo)
        self.lock = threading.RLock()
        with private_db(path) as db:
            db.execute("BEGIN IMMEDIATE")
            migrate_workflow(db, self.project)

    def _member(self, actor):
        if str(actor) not in self.config.members and str(actor) not in self.config.approvers:
            raise PermissionError("Team access required")

    def _approver(self, actor):
        if str(actor) not in self.config.approvers:
            raise PermissionError("An authorized human approver is required")

    def _meeting(self, db, key):
        row = db.execute("""SELECT m.*,v.project,v.state,v.providers,v.job_id,v.minutes AS extraction,
                            v.audit_job,v.audit_result,v.input_hash,v.imported_at,v.generation,v.agent_plan FROM meeting m JOIN workflow_v2 v
                            ON v.meeting=m.id WHERE m.id=? AND v.project=?""", (key, self.project)).fetchone()
        if row is None:
            raise ValueError("Unknown meeting in this project")
        return row

    @staticmethod
    def _revision(db, digest):
        row = db.execute("SELECT * FROM revision WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ValueError("Unknown revision")
        return row

    def _head(self, db, meeting, digest):
        if not digest or digest != meeting["head"]:
            raise ValueError("Operation requires the current revision")
        return self._revision(db, digest)

    def _state(self, db, key, state, job_id=None):
        db.execute("UPDATE workflow_v2 SET state=?,job_id=? WHERE meeting=? AND project=?",
                   (state, job_id, key, self.project))
        head = self._meeting(db, key)["head"]
        notice = "workflow:" + _digest(self.project, key, head, state, job_id)
        db.execute("INSERT OR IGNORE INTO workflow_notice VALUES(?,?,?)",
                   (notice, self.project, f"Meeting {key} · {state}" + (f" · job {job_id}" if job_id else "")))

    def _supersede(self, db, row):
        if self.jobs and row["job_id"]:
            self.jobs.cancel(row["job_id"], "Human workflow decision", db=db)
        db.execute("UPDATE workflow_v2 SET generation=generation+1,job_id=NULL WHERE meeting=?", (row["id"],))

    def _record_consent(self, db, key, actor, allowed):
        db.execute("INSERT INTO workflow_consent VALUES(?,?,?,?,?)",
                   (key, self.project, str(actor), _now(), _json(allowed)))

    def _allowed(self, row):
        policy = getattr(getattr(self.config, "privacy", None), "project_providers", ())
        return sorted(set(json.loads(row["providers"])).intersection(policy))

    def _queue(self, db, meeting, kind):
        key = meeting["id"]
        providers = self._allowed(meeting)
        if self.jobs is None or not providers:
            self._state(db, key, STATES_FOR_JOB[kind])
            return None
        payload = {"meeting": key, "digest": meeting["head"], "providers": providers, "generation": meeting["generation"]}
        if kind == "validation" and meeting["audit_result"]:
            payload["audit_id"] = meeting["audit_job"]
        job_id = self.jobs.enqueue(self.project, kind, "management", _digest(self.project, kind, payload),
                                   payload, providers, db=db)
        self._state(db, key, STATES_FOR_JOB[kind], job_id)
        return job_id

    def import_meeting(self, source, text, actor, consent=False, *, providers=(), attachment_hash=None):
        self._member(actor)
        _text(source, "source reference")
        maximum = getattr(getattr(self.config, "privacy", None), "max_input_bytes", 1024 * 1024)
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > maximum:
            raise ValueError("Invalid or oversized transcript")
        if isinstance(providers, str) or not isinstance(providers, (list, tuple, set, frozenset)):
            raise ValueError("Supply an explicit list of permitted providers")
        allowed = sorted(set(providers))
        if not isinstance(consent, bool) or bool(allowed) != consent or set(allowed) - PROVIDERS:
            raise ValueError("Confirm participant consent and specify each permitted external provider")
        policy = getattr(getattr(self.config, "privacy", None), "project_providers", ())
        if set(allowed) - set(policy):
            raise PermissionError("A selected provider is outside the deployment recipient allowlist")
        if attachment_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", attachment_hash):
            raise ValueError("Invalid attachment SHA-256")
        key = _digest(source)[:20]
        with self.lock, private_db(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("""SELECT m.id,v.providers,v.input_hash,v.imported_at,v.generation,v.agent_plan FROM meeting m
                JOIN workflow_v2 v ON m.id=v.meeting WHERE v.project=? AND
                (m.source=? OR v.attachment_hash=?)""", (self.project, source, attachment_hash)).fetchone()
            if existing:
                if existing["imported_at"] == LEGACY_DATE and existing["input_hash"] == _digest(text):
                    # An explicit re-import with the same source and bytes establishes
                    # a new retention date and named recipients on a legacy record.
                    key = existing["id"]
                    db.execute("UPDATE meeting SET transcript=?,consent=? WHERE id=?", (text, consent, key))
                    db.execute("UPDATE workflow_v2 SET providers=?, imported_at=? WHERE meeting=?",
                               (_json(allowed), _now(), key))
                    self._record_consent(db, key, actor, allowed)
                    legacy = self._meeting(db, key)
                    for kind, state in STATES_FOR_JOB.items():
                        if legacy["state"] == state or (kind == "validation" and legacy["state"] == "HUMAN_REVIEWED"):
                            self._queue(db, legacy, kind)
                    return key
                if existing["input_hash"] != _digest(text) or json.loads(existing["providers"]) != allowed:
                    raise ValueError("Existing import has different content or provider consent")
                return existing["id"]
            db.execute("INSERT INTO meeting VALUES (?,?,?,?,NULL,NULL)", (key, source, text, consent))
            db.execute("""INSERT INTO workflow_v2(meeting,project,state,providers,attachment_hash,input_hash,imported_at)
                          VALUES(?,?,'IMPORTED',?,?,?,?)""",
                       (key, self.project, _json(allowed), attachment_hash, _digest(text), _now()))
            self._record_consent(db, key, actor, allowed)
            self._queue(db, self._meeting(db, key), "minutes")
        return key

    def set_providers(self, meeting, actor, providers):
        """A trusted human may confirm or revoke participant consent, including on v1 imports."""
        self._approver(actor)
        if not isinstance(providers, (list, tuple)) or any(p not in PROVIDERS for p in providers):
            raise ValueError("Choose named recipients, or an empty list for local processing")
        allowed = sorted(set(providers))
        policy = getattr(getattr(self.config, "privacy", None), "project_providers", ())
        if set(allowed) - set(policy):
            raise PermissionError("Provider is outside the deployment recipient allowlist")
        with self.lock, private_db(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._meeting(db, meeting)
            if json.loads(row["providers"]) == allowed and row["job_id"]:
                return row["job_id"]
            self._record_consent(db, meeting, actor, allowed)
            self._supersede(db, row)
            db.execute("UPDATE workflow_v2 SET providers=? WHERE meeting=?", (_json(allowed), meeting))
            db.execute("UPDATE meeting SET consent=? WHERE id=?", (bool(allowed), meeting))
            state = row["state"]
            if state == "HUMAN_REVIEWED":
                state = "VALIDATING"
            for kind, pending in STATES_FOR_JOB.items():
                if pending == state:
                    return self._queue(db, self._meeting(db, meeting), kind)
            return None

    def _save(self, db, meeting, markdown, minutes, actor, reason):
        _markdown(markdown)
        _text(reason, "revision reason")
        digest = _digest(meeting["id"], meeting["head"], markdown, minutes, str(actor), reason)
        db.execute("""INSERT INTO revision
                    (digest,meeting_id,parent,markdown,minutes,actor,reason)
                    VALUES (?,?,?,?,?,?,?)""",
                   (digest, meeting["id"], meeting["head"], markdown, _json(minutes), str(actor), reason))
        db.execute("UPDATE meeting SET head=? WHERE id=?", (digest, meeting["id"]))
        return digest

    @staticmethod
    def _minutes_input(data, row):
        # Trusted human imports retain compatibility with the v1 minutes JSON.
        if isinstance(data, dict) and "summary" in data:
            # /spec show includes server-generated source references; preserve
            # them when humans download, edit, and re-upload the minutes.
            source = {"meeting_id": row["id"], "source": row["source"]}
            fields = {k: v for k, v in data.items() if k not in source}
            return {**Minutes.model_validate(fields).model_dump(), **source}
        return _minutes(data)

    def draft(self, meeting, actor, *, markdown=None, minutes=None):
        self._member(actor)
        with self.lock, private_db(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._meeting(db, meeting)
            if row["head"]:
                raise ValueError("Draft exists; revise the current revision")
            if markdown is None:
                if row["job_id"]:
                    return row["job_id"]
                raise PermissionError("Supply Markdown and minutes for manual drafting")
            self._supersede(db, row)
            records = self._minutes_input(minutes, row)
            digest = self._save(db, row, markdown, records, actor, "Initial draft")
            self._state(db, meeting, "DRAFT_READY")
            return digest

    def revise(self, meeting, actor, markdown, reason, *, based_on=None, minutes=None):
        self._member(actor)
        with self.lock, private_db(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._meeting(db, meeting)
            if based_on is not None and based_on != row["head"]:
                raise ValueError("Stale revision; review the current diff")
            previous = self._revision(db, row["head"])
            if previous["plan"] and row["published"] != row["head"]:
                raise ValueError("Retry publication of the approved plan first")
            self._supersede(db, row)
            records = json.loads(previous["minutes"]) if minutes is None else self._minutes_input(minutes, row)
            digest = self._save(db, row, markdown, records, actor, reason)
            db.execute("UPDATE workflow_v2 SET audit_job=NULL,audit_result=NULL WHERE meeting=?", (meeting,))
            self._state(db, meeting, "HUMAN_REVIEWED")
            self._queue(db, self._meeting(db, meeting), "validation")
            return digest

    def show(self, meeting, actor, *, digest=None):
        self._member(actor)
        with private_db(self.path) as db:
            row = self._meeting(db, meeting)
            output = {"meeting": meeting, "source": row["source"], "state": row["state"],
                      "job_id": row["job_id"], "providers": self._allowed(row), "digest": None, "minutes": None}
            if not digest and not row["head"]:
                output["minutes"] = json.loads(row["extraction"]) if row["extraction"] else None
                return output
            rev = self._revision(db, digest or row["head"])
            if rev["meeting_id"] != meeting:
                raise ValueError("Revision belongs to another meeting")
            previous = self._revision(db, rev["parent"])["markdown"] if rev["parent"] else ""
            approval = db.execute("SELECT * FROM workflow_approval WHERE digest=?", (rev["digest"],)).fetchone()
            return {**output, "digest": rev["digest"], "parent": rev["parent"],
                    "markdown": rev["markdown"], "minutes": json.loads(rev["minutes"]),
                    "actor": rev["actor"], "reason": rev["reason"], "approved_by": rev["approved_by"],
                    "approval": dict(approval) if approval else None,
                    "report": json.loads(rev["report"]) if rev["report"] else None,
                    "diff": "".join(difflib.unified_diff(previous.splitlines(True),
                                                        rev["markdown"].splitlines(True)))}

    def _record_review(self, db, row, digest, report, reviewer):
        rev = self._head(db, row, digest)
        if (row["state"] not in ("HUMAN_REVIEWED", "VALIDATING") or
                not rev["parent"] or rev["approved_by"]):
            raise ValueError("Validate the current, human-revised specification")
        report = dict(_report(report, digest, self._allowed(row)))
        if row["audit_result"]:
            audit = json.loads(row["audit_result"])
            blockers = [f["summary"] for f in audit.get("findings", []) if f.get("severity") == "high"]
            report["blocking_questions"] = list(dict.fromkeys(report["blocking_questions"] + blockers))
            report["audit_job_id"] = row["audit_job"]
        db.execute("UPDATE revision SET report=?, reviewer=? WHERE digest=?",
                   (_json(report), reviewer, digest))
        self._state(db, row["id"], "NEEDS_REVISION" if report["blocking_questions"] else "VALIDATED")
        return report

    def validate(self, meeting, actor, digest, *, report=None):
        self._member(actor)
        with self.lock, private_db(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._meeting(db, meeting)
            self._head(db, row, digest)
            if report is None:
                if row["state"] not in ("HUMAN_REVIEWED", "VALIDATING"):
                    raise ValueError("Validate the current human-revised specification")
                if row["job_id"]:
                    return row["job_id"]
                raise ValueError("Supply an attested report for manual validation")
            self._approver(actor)
            self._supersede(db, row)
            return self._record_review(db, row, digest, report, str(actor))

    def _plan(self, markdown, data, *, initial):
        if not isinstance(data, list) or not data:
            raise ValueError("Provide a nonempty list of task definitions")
        tasks = [new_task(item, self.config) for item in data]
        keys = {t.key for t in tasks}
        existing = self.github.list_tasks()
        if initial and any(t.key in keys for t in existing):
            raise ValueError("A task key already belongs to another approved plan")
        validate_tasks([t for t in existing if t.key not in keys] + tasks)
        defined = set(REQUIREMENT.findall(markdown))
        linked = {key for task in tasks for key in task.requirement_ids}
        if linked != defined:
            raise ValueError("Task requirement links must cover exactly the specification IDs")
        return tasks

    def _prepare_plan(self, db, row, rev, tasks, *, agent):
        if row["state"] not in ("VALIDATED", "PLANNING", "APPROVED"):
            raise ValueError("Task plan requires the current validated revision")
        data = tasks
        if agent:
            data = TaskPlan.model_validate({"tasks": tasks}).model_dump(exclude_none=True)["tasks"]
        plan = self._plan(rev["markdown"], data, initial=not row["published"])
        if agent and any(not self.github.is_private(repo) for repo in {t.repo for t in plan}):
            raise PermissionError("A public product repository requires a human-checked public task plan")
        if row["published"]:
            if self.changes is None:
                raise ValueError("Change manager required to reconcile an approved specification")
            old = self._revision(db, row["published"])
            self.changes.preview(row["id"], rev["digest"],
                                 [Task.from_dict(t) for t in json.loads(old["plan"])], plan)
        return PreparedPlan(row["id"], rev["digest"], _digest(tasks), tuple(plan))

    def prepare_job(self, job, provider, data):
        """Read the GitHub graph/visibility before the lease completion write transaction."""
        with private_db(self.path) as db:
            row = self._job(db, job, provider)
            rev = self._head(db, row, data["digest"])
            if job["kind"] != "task_plan" or not rev["approved_by"]:
                raise PermissionError("Planning requires an approved revision")
            return self._prepare_plan(db, row, rev, data["tasks"], agent=True)

    def _stage_plan(self, db, row, rev, tasks, prepared, *, agent):
        if (row["state"] not in ("VALIDATED", "PLANNING", "APPROVED") or not prepared or
                (row["id"], rev["digest"], _digest(tasks)) !=
                (prepared.meeting, prepared.digest, prepared.input_hash)):
            raise ValueError("Preflight must match the current revision and task plan")
        plan = prepared.tasks
        # Reserve keys before remote publication, including against other meetings
        # that finished preflight concurrently while the first publish is pending.
        for task in plan:
            db.execute("INSERT OR IGNORE INTO workflow_key VALUES(?,?,?)", (self.project, task.key, row["id"]))
            owner = db.execute("SELECT meeting FROM workflow_key WHERE project=? AND key=?",
                               (self.project, task.key)).fetchone()[0]
            if owner != row["id"]:
                raise ValueError(f"{task.key}: key reserved by another approved meeting")
        db.execute("UPDATE revision SET plan=? WHERE digest=?",
                   (_json([t.to_dict() for t in plan]), rev["digest"]))
        db.execute("UPDATE workflow_v2 SET agent_plan=? WHERE meeting=?", (bool(agent), row["id"]))
        self._state(db, row["id"], "PUBLISHING")

    def approve(self, meeting, actor, digest, *, tasks=None):
        self._approver(actor)
        with self.lock:
            with private_db(self.path) as db:
                row = self._meeting(db, meeting)
                rev = self._head(db, row, digest)
                if row["published"] == digest:
                    return self._published(rev)
                prepared = None
                if tasks is not None and not rev["plan"]:
                    prepared = self._prepare_plan(db, row, rev, tasks, agent=False)
                db.execute("BEGIN IMMEDIATE")
                row = self._meeting(db, meeting)
                rev = self._head(db, row, digest)
                if row["state"] not in ("VALIDATED", "APPROVED", "PLANNING", "PUBLISHING", "PUBLISHED"):
                    raise ValueError("Approval requires validation of the current revision")
                if not rev["report"] or json.loads(rev["report"])["blocking_questions"]:
                    raise ValueError("Resolve blocking questions and validate before approval")
                if tasks is not None and not rev["plan"]:
                    self._supersede(db, row)
                    self._stage_plan(db, row, rev, tasks, prepared, agent=False)
                if not rev["approved_by"]:
                    db.execute("UPDATE revision SET approved_by=? WHERE digest=?", (str(actor), digest))
                    db.execute("""INSERT INTO workflow_approval VALUES(?,?,?,?,?,?)""",
                               (digest, self.project, meeting, _digest(json.loads(rev["report"])), str(actor), _now()))
                if tasks is None and not rev["plan"]:
                    if not rev["approved_by"]:
                        self._state(db, meeting, "APPROVED")
                    job = self._queue(db, row, "task_plan")
                    return job or f"manual:{meeting}:{digest}"
            return self._publish(meeting, digest)

    def _published(self, rev):
        keys = {t["key"] for t in json.loads(rev["plan"])}
        if self.changes:
            keys = self.changes.published_keys(rev["digest"], keys)
        return [t for t in self.github.list_tasks() if t.key in keys]

    def _publish(self, meeting, digest):
        with private_db(self.path) as db:
            row = self._meeting(db, meeting)
            rev = self._head(db, row, digest)
            if row["state"] == "PUBLISHED":
                return self._published(rev)
            approval = db.execute("SELECT * FROM workflow_approval WHERE digest=? AND meeting=? AND project=?",
                                  (digest, meeting, self.project)).fetchone()
            if (row["state"] != "PUBLISHING" or not rev["plan"] or not approval or
                    approval["report_hash"] != _digest(json.loads(rev["report"]))):
                raise ValueError("Publication requires a plan bound to the approved validation")
            old = self._revision(db, row["published"]) if row["published"] else None
        plan = [Task.from_dict(item) for item in json.loads(rev["plan"])]
        if row["agent_plan"] and any(not self.github.is_private(repo) for repo in {t.repo for t in plan}):
            raise PermissionError("Product repository visibility changed; publication is paused")
        self.github.archive_spec(meeting, digest, rev["markdown"])
        if old:
            published = self.changes.reconcile(meeting, digest, [Task.from_dict(t) for t in json.loads(old["plan"])], plan)
        else:
            published = self.github.publish(plan)
        with private_db(self.path) as db:
            db.execute("UPDATE meeting SET published=? WHERE id=? AND head=?", (digest, meeting, digest))
            self._state(db, meeting, "PUBLISHED")
        return published

    def pending_for(self, provider):
        """Bounded patrol metadata restricted to this provider's consented meetings."""
        with private_db(self.path) as db:
            rows = db.execute("""SELECT v.meeting,m.head AS digest,v.state,v.job_id,v.providers
                FROM workflow_v2 v JOIN meeting m ON m.id=v.meeting WHERE v.project=?
                AND v.state IN ('IMPORTED','MINUTES_READY','VALIDATING','WAITING_AUDIT','PLANNING','PUBLISHING')
                ORDER BY v.imported_at,v.meeting""", (self.project,))
            result = []
            for row in rows:
                if provider in self._allowed(row):
                    result.append({k: row[k] for k in ("meeting", "digest", "state", "job_id")})
                    if len(result) == 100:
                        break
            return result

    def providers_for(self, meeting, digest):
        with private_db(self.path) as db:
            row = self._meeting(db, meeting)
            self._head(db, row, digest)
            return self._allowed(row)

    def spec_for_audit(self, meeting, digest, provider):
        with private_db(self.path) as db:
            row = self._meeting(db, meeting)
            rev = self._head(db, row, digest)
            if provider not in self._allowed(row):
                raise PermissionError("Provider lacks consent for this meeting")
            return {"meeting": meeting, "state": row["state"], "digest": digest, "markdown": rev["markdown"]}

    def _job(self, db, job, provider):
        if job["project"] != self.project or job["kind"] not in STATES_FOR_JOB:
            raise PermissionError("Invalid project or workflow job")
        row = self._meeting(db, job["payload"]["meeting"])
        if provider not in self._allowed(row):
            raise PermissionError("Provider lacks consent for this meeting")
        if (job["id"] != row["job_id"] or row["state"] != STATES_FOR_JOB[job["kind"]] or
                job["payload"]["digest"] != row["head"]):
            raise ValueError("Superseded workflow job")
        return row

    def job_context(self, job, provider):
        with private_db(self.path) as db:
            row = self._job(db, job, provider)
            result = {"meeting": row["id"], "source": row["source"], "state": row["state"], "digest": row["head"]}
            if job["kind"] == "minutes":
                if not self._raw_available(row):
                    raise ValueError("Transcript retention period expired")
                return {**result, "transcript": row["transcript"]}
            if job["kind"] == "specification":
                return {**result, "minutes": json.loads(row["extraction"])}
            rev = self._head(db, row, row["head"])
            result.update(markdown=rev["markdown"], minutes=json.loads(rev["minutes"]))
            if row["audit_result"] and job["kind"] == "validation":
                result["audit_result"] = json.loads(row["audit_result"])
            return result

    def submit_job(self, db, job, provider, data, *, prepared=None):
        """Called inside JobStore.complete after lease/scope checks, before commit."""
        row = self._job(db, job, provider)
        kind, meeting = job["kind"], row["id"]
        if kind == "minutes":
            minutes = Minutes.model_validate(data["minutes"]).model_dump()
            if not self._raw_available(row) or any(d["quote"] not in row["transcript"] for d in minutes["decisions"]):
                raise ValueError("Decisions require an exact quote from the available transcript")
            minutes.update(meeting_id=meeting, source=row["source"])
            db.execute("UPDATE workflow_v2 SET minutes=? WHERE meeting=?", (_json(minutes), meeting))
            self._queue(db, row, "specification")
        elif kind == "specification":
            evidence = _strings(data["evidence"], "evidence")
            if not evidence:
                raise ValueError("A specification requires decision or minutes references")
            reason = "Draft evidence: " + "\n".join(evidence)
            self._save(db, row, data["markdown"], json.loads(row["extraction"]), "agent:" + provider, reason)
            self._state(db, meeting, "DRAFT_READY")
        else:
            rev = self._head(db, row, data["digest"])
            if kind == "task_plan":
                if not rev["approved_by"]:
                    raise PermissionError("Only a human-approved specification may generate tasks")
                self._stage_plan(db, row, rev, data["tasks"], prepared, agent=True)
            else:
                self._agent_review(db, row, rev, data, provider)
        updated = self._meeting(db, meeting)
        return {"meeting": meeting, "state": updated["state"], "digest": updated["head"], "job_id": updated["job_id"]}

    def _agent_review(self, db, row, rev, data, provider):
        report = Review.model_validate({k: data[k] for k in ("checks", "blocking_questions")}).model_dump()
        report.update(provider=provider, revision=rev["digest"], processing="external")
        patch = data.get("minor_revision")
        if patch and patch != rev["markdown"]:
            # Even whitespace can carry Markdown semantics. Only line-ending
            # conversion is unambiguously mechanical without human review.
            if patch != rev["markdown"].replace("\r\n", "\n"):
                report["blocking_questions"].append("Human decision required for the suggested revision")
                report["suggested_markdown"] = _markdown(patch)
            elif not report["blocking_questions"]:
                self._save(db, row, patch, json.loads(rev["minutes"]), "agent:" + provider, "Normalize line endings")
                self._queue(db, self._meeting(db, row["id"]), "validation")
                return
        audit_id = data.get("audit_job_id")
        if audit_id and not report["blocking_questions"]:
            audit = self.jobs.get(audit_id, db=db)
            args = audit["payload"]
            if (audit["kind"] != "audit" or audit["project"] != self.project or args.get("kind") != "spec" or
                    args.get("meeting") != row["id"] or args.get("sha") != rev["digest"]):
                raise ValueError("Audit must target this meeting and revision")
            if not set(audit["providers"]).issubset(self._allowed(row)):
                raise PermissionError("Audit recipients exceed the meeting consent")
            if audit_id == row["audit_job"] and row["audit_result"]:
                raise ValueError("This audit is already available; submit the final validation")
            db.execute("UPDATE workflow_v2 SET audit_job=?,audit_result=NULL WHERE meeting=?", (audit_id, row["id"]))
            self._state(db, row["id"], "WAITING_AUDIT")
            return
        self._record_review(db, row, rev["digest"], report, "agent:" + provider)

    def recover(self):
        """Resume persisted publishes and audit continuations after restart."""
        with self.lock:
            with private_db(self.path) as db:
                active = list(db.execute("""SELECT m.id,m.head,v.state,v.audit_job FROM workflow_v2 v
                    JOIN meeting m ON m.id=v.meeting WHERE v.project=? AND v.state IN ('PUBLISHING','WAITING_AUDIT')""",
                                         (self.project,)))
            first_failure = None
            for row in active:
                try:
                    if row["state"] == "PUBLISHING":
                        self._publish(row["id"], row["head"])
                    elif self.jobs:
                        with private_db(self.path) as db:
                            db.execute("BEGIN IMMEDIATE")
                            job = self.jobs.get(row["audit_job"], db=db)
                            if job["result_ref"]:
                                result = self.jobs.artifact(job["result_ref"], db=db)
                                db.execute("UPDATE workflow_v2 SET audit_result=? WHERE meeting=?", (_json(result), row["id"]))
                                self._queue(db, self._meeting(db, row["id"]), "validation")
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    if first_failure is None:
                        first_failure = exc
                    # A blocked publication must not starve other approvals or
                    # prevent the delivery of status notices already in the outbox.
                    event = "workflow:" + _digest(row["id"], row["head"], "retry", type(exc).__name__)
                    with private_db(self.path) as db:
                        db.execute("INSERT OR IGNORE INTO workflow_notice VALUES(?,?,?)",
                                   (event, self.project, f"Meeting {row['id']} · retry pending ({row['state']})"))
            with private_db(self.path) as db:
                notices = list(db.execute("SELECT * FROM workflow_notice WHERE project=? ORDER BY rowid LIMIT 100", (self.project,)))
            for notice in notices:
                self.github.notify(notice["id"], notice["body"])
                with private_db(self.path) as db:
                    db.execute("DELETE FROM workflow_notice WHERE id=?", (notice["id"],))
            if first_failure is not None:
                raise first_failure


    def _raw_available(self, row):
        days = getattr(getattr(self.config, "privacy", None), "raw_retention_days", 30)
        return bool(row["transcript"]) and datetime.fromisoformat(row["imported_at"]) >= (
            datetime.now(timezone.utc) - timedelta(days=days))

    def purge_raw(self, days):
        """Expire private raw input without rewriting signed revisions or minutes."""
        if type(days) is not int or days < 1:
            raise ValueError("Retention must be a positive number of days")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with private_db(self.path) as db:
            return db.execute("""UPDATE meeting SET transcript='' WHERE transcript!='' AND id IN
                (SELECT meeting FROM workflow_v2 WHERE project=? AND imported_at<?)""",
                              (self.project, cutoff)).rowcount
