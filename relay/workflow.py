"""Private meeting workspace, revision-bound review, and recoverable publication."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

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


def _report(data, digest, consent):
    if not isinstance(data, dict) or data.get("revision") != digest:
        raise ValueError("Review must identify this exact revision digest")
    _text(data.get("provider"), "AI reviewer")
    if not consent and data.get("processing") != "local":
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


class Workflow:
    def __init__(self, path, config, github, provider=None, *, changes=None):
        self.path = path
        self.config = config
        self.github = github
        self.provider = provider
        self.changes = changes
        self.lock = threading.RLock()
        with private_db(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meeting (
                    id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, transcript TEXT NOT NULL,
                    consent INTEGER NOT NULL, head TEXT, published TEXT);
                CREATE TABLE IF NOT EXISTS revision (
                    digest TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, parent TEXT,
                    markdown TEXT NOT NULL, minutes TEXT NOT NULL, actor TEXT NOT NULL,
                    reason TEXT NOT NULL, report TEXT, reviewer TEXT, approved_by TEXT, plan TEXT);
            """)

    def _member(self, actor):
        if str(actor) not in self.config.members and str(actor) not in self.config.approvers:
            raise PermissionError("Team access required")

    def _approver(self, actor):
        if str(actor) not in self.config.approvers:
            raise PermissionError("An authorized approver is required")

    def _ai(self, meeting):
        if not meeting["consent"]:
            raise PermissionError("External processing requires participant consent")
        if self.provider is None:
            raise ValueError("Supply manual input or configure an AI provider")
        return self.provider

    @staticmethod
    def _meeting(db, key):
        row = db.execute("SELECT * FROM meeting WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Unknown meeting")
        return row

    @staticmethod
    def _revision(db, digest):
        row = db.execute("SELECT * FROM revision WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ValueError("Unknown revision")
        return row

    def import_meeting(self, source, text, actor, consent=False):
        self._member(actor)
        _text(source, "source reference")
        _text(text, "transcript")
        if not isinstance(consent, bool):
            raise ValueError("Consent must be explicit")
        key = _digest(source)[:20]
        with private_db(self.path) as db:
            db.execute("INSERT OR IGNORE INTO meeting VALUES (?,?,?,?,NULL,NULL)",
                       (key, source, text, consent))
            row = self._meeting(db, key)
            if row["transcript"] != text or bool(row["consent"]) != consent:
                raise ValueError("Source already imported with different text or consent")
        return key

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

    def draft(self, meeting, actor, *, markdown=None, minutes=None):
        self._member(actor)
        with self.lock, private_db(self.path) as db:
            row = self._meeting(db, meeting)
            if row["head"]:
                raise ValueError("Draft exists; revise the current revision")
            if markdown is None:
                result = self._ai(row).draft(row["transcript"])
                markdown, minutes = result["markdown"], result["minutes"]
            db.execute("BEGIN IMMEDIATE")
            row = self._meeting(db, meeting)
            if row["head"]:
                raise ValueError("Another draft was saved; reload")
            return self._save(db, row, markdown, _minutes(minutes), actor, "Initial draft")

    def revise(self, meeting, actor, markdown, reason, *, based_on=None):
        self._member(actor)
        with self.lock, private_db(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._meeting(db, meeting)
            if based_on is not None and based_on != row["head"]:
                raise ValueError("Stale revision; review the current diff")
            previous = self._revision(db, row["head"])
            if previous["approved_by"] and row["published"] != row["head"]:
                raise ValueError("Retry publication of the approved plan first")
            return self._save(db, row, markdown, json.loads(previous["minutes"]), actor, reason)

    def show(self, meeting, actor, *, digest=None):
        self._member(actor)
        with private_db(self.path) as db:
            row = self._meeting(db, meeting)
            rev = self._revision(db, digest or row["head"])
            if rev["meeting_id"] != meeting:
                raise ValueError("Revision belongs to another meeting")
            previous = self._revision(db, rev["parent"])["markdown"] if rev["parent"] else ""
            return {"digest": rev["digest"], "parent": rev["parent"], "source": row["source"],
                    "markdown": rev["markdown"], "minutes": json.loads(rev["minutes"]),
                    "actor": rev["actor"], "reason": rev["reason"], "approved_by": rev["approved_by"],
                    "report": json.loads(rev["report"]) if rev["report"] else None,
                    "diff": "".join(difflib.unified_diff(previous.splitlines(True),
                                                        rev["markdown"].splitlines(True)))}

    def validate(self, meeting, actor, digest, *, report=None):
        self._member(actor)
        with self.lock, private_db(self.path) as db:
            row = self._meeting(db, meeting)
            rev = self._revision(db, digest)
            if row["head"] != digest or not rev["parent"] or rev["approved_by"]:
                raise ValueError("Validate the current, human-revised specification")
            if rev["report"] and json.loads(rev["report"])["blocking_questions"]:
                raise ValueError("Revise the specification to resolve the blocking review")
            if report is None:
                report = self._ai(row).validate(rev["markdown"], json.loads(rev["minutes"]), digest)
            else:
                # Manual import is an attestation by a trusted reviewer.
                self._approver(actor)
            report = _report(report, digest, row["consent"])
            db.execute("BEGIN IMMEDIATE")
            if self._meeting(db, meeting)["head"] != digest or self._revision(db, digest)["approved_by"]:
                raise ValueError("Revision changed during validation")
            db.execute("UPDATE revision SET report=?, reviewer=? WHERE digest=?",
                       (_json(report), str(actor), digest))
            return report

    def _plan(self, markdown, data):
        if not isinstance(data, list) or not data:
            raise ValueError("Provide a nonempty list of task definitions")
        tasks = [new_task(item, self.config) for item in data]
        validate_tasks(tasks)
        defined = set(REQUIREMENT.findall(markdown))
        linked = {key for task in tasks for key in task.requirement_ids}
        if linked != defined:
            raise ValueError("Task requirement links must cover exactly the specification IDs")
        return tasks

    def approve(self, meeting, actor, digest, *, tasks=None):
        self._approver(actor)
        with self.lock:
            with private_db(self.path) as db:
                row = self._meeting(db, meeting)
                rev = self._revision(db, digest)
                if row["head"] != digest or not rev["report"]:
                    raise ValueError("Approval requires validation of the current revision")
                report = _report(json.loads(rev["report"]), digest, row["consent"])
                if report["blocking_questions"]:
                    raise ValueError("Resolve blocking questions and revise the specification")
                if rev["plan"]:
                    plan = [Task.from_dict(item) for item in json.loads(rev["plan"])]
                else:
                    if tasks is None:
                        tasks = self._ai(row).tasks(rev["markdown"], digest)
                    plan = self._plan(rev["markdown"], tasks)
                if row["published"] == digest:
                    keys = {task.key for task in plan}
                    return [task for task in self.github.list_tasks() if task.key in keys]
                if row["published"] and self.changes is None:
                    raise ValueError("Change manager required to reconcile an approved specification")
                db.execute("BEGIN IMMEDIATE")
                current = self._revision(db, digest)
                if self._meeting(db, meeting)["head"] != digest or current["report"] != rev["report"]:
                    raise ValueError("Revision or review changed; reload before approving")
                if current["plan"]:
                    plan = [Task.from_dict(item) for item in json.loads(current["plan"])]
                else:
                    db.execute("UPDATE revision SET approved_by=?, plan=? WHERE digest=?",
                               (str(actor), _json([task.to_dict() for task in plan]), digest))
                old = self._revision(db, row["published"]) if row["published"] else None

            # Persist the exact approved plan before making any network calls. A retry
            # uses these bytes even if a provider would produce different task keys.
            self.github.archive_spec(meeting, digest, rev["markdown"])
            if old:
                previous = [Task.from_dict(item) for item in json.loads(old["plan"])]
                published = self.changes.reconcile(meeting, digest, previous, plan)
            else:
                published = self.github.publish(plan)
            with private_db(self.path) as db:
                db.execute("UPDATE meeting SET published=? WHERE id=? AND head=?", (digest, meeting, digest))
            return published
