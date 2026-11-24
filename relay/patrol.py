"""Read-only audit snapshots, restartable patrol deltas and narrow GitHub actions."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import threading
from urllib.parse import quote, urlencode

from .github import Conflict, GitHubError, digest

SHA = re.compile(r"[0-9a-f]{40}")
REVISION = re.compile(r"[0-9a-f]{64}")
NUMBER = re.compile(r"[1-9][0-9]{0,15}")
CATEGORIES = {
    "correctness": ("Unexpected behavior", "Verify the affected behavior against its acceptance criteria."),
    "compatibility": ("Interface compatibility", "Check the changed interface against its consumers."),
    "test_coverage": ("Missing regression coverage", "Add a test covering the identified behavior."),
    "ci_failure": ("Failed automated check", "Resolve the failed check and confirm a passing run."),
    "feasibility": ("Feasibility concern", "Verify feasibility against the approved requirements."),
}
TEXT_FILES = {".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".rs", ".go", ".java",
              ".c", ".cpp", ".h", ".hpp", ".rb", ".kt", ".swift", ".sh", ".md", ".sql"}
SENSITIVE_NAME = re.compile(r"(?:secret|credential|password|token|private[_-]?key|^\.env)", re.I)
SENSITIVE_LINE = re.compile(
    r"(?:password|passwd|credential|secret|api.?key|bearer|token|-----BEGIN|github_pat_|"
    r"gh[pousr]_|sk-[A-Za-z0-9]|[A-Za-z0-9+/=_-]{48,})", re.I,
)


def bounded(text, name, limit):
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= limit or "\0" in text:
        raise ValueError(f"Invalid {name}")
    return text.strip()


def safe_diff(text):
    return "\n".join("[redacted]" if SENSITIVE_LINE.search(line) else line[:400]
                     for line in text[:4096].splitlines()[:60])


def json_data(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def latest_report(db, project, retention_days, *, now=None):
    """Read a current private operator report without opening a writer connection."""
    if type(retention_days) is not int or not 1 <= retention_days <= 365:
        raise ValueError("Invalid report retention")
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    row = db.execute("""SELECT id, created, data FROM artifacts WHERE project=? AND kind='patrol_report'
                        AND created>=? AND data!='null' ORDER BY created DESC, rowid DESC LIMIT 1""",
                     (project, cutoff)).fetchone()
    if row is None:
        return None
    return {"receipt": row[0], "created": row[1], **json.loads(row[2])}


class Patrol:
    def __init__(self, config, github, jobs, path, *, workflow=None, now=None):
        self.config = config
        self.github = github
        self.jobs = jobs
        self.project = config.management_repo
        self.workflow = workflow
        self.path = Path(path)
        self._outbound_lock = threading.Lock()
        self.now = now or (lambda: datetime.now(timezone.utc))
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS patrol_identity (project TEXT PRIMARY KEY, nonce TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS patrol_scan (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, cursor TEXT NOT NULL,
                    items TEXT NOT NULL, changes TEXT NOT NULL, completed_audits TEXT NOT NULL,
                    seen INTEGER NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS patrol_audit (
                    job TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL,
                    target TEXT NOT NULL, input TEXT NOT NULL, evidence TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS patrol_action (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, receipt TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO patrol_identity VALUES(?,?)", (self.project, secrets.token_hex(32)))
            self._nonce = db.execute("SELECT nonce FROM patrol_identity WHERE project=?", (self.project,)).fetchone()[0]

    @contextmanager
    def _db(self, db=None):
        if db is not None:
            yield db
            return
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _fence(conn, check):
        if check:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            check(conn)

    def _collect(self):
        records = {}
        for repo in self.config.product_repos:
            root = "/repos/" + repo
            for item in self.github._pages(root + "/issues", {"state": "all"}):
                if "pull_request" not in item:
                    key = f"issue:{repo}:{item['number']}"
                    records[key] = dict(kind="issue", repo=repo, target=str(item["number"]),
                                        state=item["state"], updated=item.get("updated_at"))
            for item in self.github._pages(root + "/pulls", {"state": "all"}):
                key = f"pr:{repo}:{item['number']}"
                records[key] = dict(kind="pr", repo=repo, target=str(item["number"]),
                                    state=item["state"], sha=item["head"]["sha"], updated=item.get("updated_at"))
            page = 1
            while True:
                query = urlencode({"per_page": 100, "page": page})
                try:
                    runs = self.github.request("GET", root + "/actions/runs?" + query)["workflow_runs"]
                except GitHubError as exc:
                    if page != 1 or exc.status not in {403, 404}:
                        raise
                    records["ci-access:" + repo] = dict(kind="availability", repo=repo,
                                                        target="actions", state="unavailable")
                    break
                for run in runs:
                    if run.get("conclusion") == "failure":
                        key = f"ci:{repo}:{run['id']}"
                        records[key] = dict(kind="ci", repo=repo, target=str(run["id"]),
                                            sha=run["head_sha"], attempt=run["run_attempt"])
                if len(runs) < 100:
                    break
                page += 1
        return records

    def delta(self, patrol_id, cursor=0, limit=50, *, provider, db=None, check=None):
        if provider not in self.config.privacy.project_providers:
            raise PermissionError("Provider lacks project consent")
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid patrol page")
        with self._db(db) as conn:
            row = conn.execute("SELECT * FROM patrol_scan WHERE project=? AND id=?",
                               (self.project, patrol_id)).fetchone()
            current = self._collect() if row is None else None
            tasks = self.github.list_tasks()
            self._fence(conn, check)
            # Another reader may have materialized this slot during network I/O.
            row = conn.execute("SELECT * FROM patrol_scan WHERE project=? AND id=?",
                               (self.project, patrol_id)).fetchone()
            if row is None:
                prior = conn.execute("""SELECT items FROM patrol_scan WHERE project=? AND finished=1
                                        ORDER BY rowid DESC LIMIT 1""", (self.project,)).fetchone()
                previous = json.loads(prior[0]) if prior else {}
                for k, old in previous.items():
                    if old["kind"] == "ci" and "ci-access:" + old["repo"] in current:
                        current.setdefault(k, old)
                changes = [dict(v, id=digest([k, v])[:24]) for k, v in sorted(current.items()) if previous.get(k) != v]
                changes.extend(dict(previous[k], id=digest(["removed", k, previous[k]])[:24], removed=True)
                               for k in sorted(previous.keys() - current.keys()))
                # A result is acknowledged by finishing a patrol that actually returned it.
                acknowledged = set()
                for past in conn.execute("SELECT completed_audits FROM patrol_scan WHERE project=? AND finished=1",
                                         (self.project,)):
                    acknowledged.update(json.loads(past[0]))
                audits = []
                for job, payload in conn.execute(
                    "SELECT job,input FROM patrol_audit WHERE project=? AND result IS NOT NULL ORDER BY job",
                    (self.project,),
                ):
                    if job not in acknowledged:
                        try:
                            self._consent(json.loads(payload), provider)
                        except (PermissionError, ValueError):
                            continue
                        audits.append(job)
                checkpoint = digest([patrol_id, current, audits])
                conn.execute("INSERT INTO patrol_scan VALUES(?,?,?,?,?,?,0,0)",
                             (patrol_id, self.project, checkpoint, json_data(current), json_data(changes),
                              json_data(audits)))
                row = conn.execute("SELECT * FROM patrol_scan WHERE id=?", (patrol_id,)).fetchone()
            # A replacement manager can have a narrower set of meeting consents.
            visible = []
            for audit_id in json.loads(row["completed_audits"]):
                try:
                    self._consent(json.loads(self._audit(conn, audit_id)["input"]), provider)
                except (PermissionError, ValueError):
                    continue
                visible.append(audit_id)
            conn.execute("UPDATE patrol_scan SET completed_audits=? WHERE id=?", (json_data(visible), patrol_id))
            items = json.loads(row["changes"])
            if cursor > row["seen"] or cursor > len(items):
                raise ValueError("Patrol pages must be read in order")
            stop = min(cursor + limit, len(items))
            conn.execute("UPDATE patrol_scan SET seen=MAX(seen,?) WHERE id=?", (stop, patrol_id))
            open_tasks = [t for t in tasks if t.status not in {"Done", "Cancelled"}]
            now = self.now()
            pending = [{"job_id": j["id"], "kind": j["kind"], "state": j["state"]}
                       for j in self.jobs.list_jobs(self.project, provider=provider, include_busy=True, limit=100, db=conn)
                       if j["state"] in {"PENDING", "RETRY_WAIT"} and provider in j["providers"]]
            if self.workflow:
                pending.extend(self.workflow.pending_for(provider))
            return dict(changes=items[cursor:stop], next_cursor=stop if stop < len(items) else None,
                        snapshot_cursor=row["cursor"], pending_jobs=pending, completed_audits=visible,
                        overdue=sorted(t.key for t in open_tasks if t.deadline and datetime.fromisoformat(t.deadline) < now),
                        blocked=sorted(t.key for t in open_tasks if t.status == "Blocked"),
                        remaining_hours={t.key: t.remaining_hours for t in open_tasks})

    def complete(self, patrol_id, snapshot_cursor, *, db=None):
        with self._db(db) as conn:
            row = conn.execute("SELECT * FROM patrol_scan WHERE project=? AND id=? AND cursor=?",
                               (self.project, patrol_id, snapshot_cursor)).fetchone()
            if row is None or row["seen"] != len(json.loads(row["changes"])):
                raise Conflict("Complete all pages of the current snapshot first")
            conn.execute("UPDATE patrol_scan SET finished=1 WHERE id=?", (patrol_id,))
        return {"checkpoint": snapshot_cursor}

    def _target(self, kind, repo, target, sha):
        if kind == "spec":
            if repo != self.project or not re.fullmatch(r"[0-9a-f]{20}", target) or not REVISION.fullmatch(sha):
                raise PermissionError("Unexpected specification target")
            providers = self.workflow.providers_for(target, sha)
            return dict(kind=kind, repo=repo, target=target, meeting=target, sha=sha, providers=providers)
        if not (kind in {"pr", "ci"} and repo in self.config.product_repos and
                NUMBER.fullmatch(target) and SHA.fullmatch(sha)):
            raise PermissionError("Unexpected audit target")
        route = "pulls" if kind == "pr" else "actions/runs"
        item = self.github.request("GET", f"/repos/{repo}/{route}/{target}")
        current = item["head"]["sha"] if kind == "pr" else item["head_sha"]
        if current != sha or (kind == "pr" and item["state"] != "open"):
            raise Conflict("Stale audit target")
        extra = {}
        if kind == "pr":
            extra["base"] = item["base"]["sha"]
            if not SHA.fullmatch(extra["base"]):
                raise ValueError("Invalid PR base")
        else:
            if item.get("conclusion") != "failure":
                raise Conflict("CI is no longer failed")
            extra["attempt"] = item["run_attempt"]
        return dict(kind=kind, repo=repo, target=target, sha=sha,
                    providers=list(self.config.privacy.project_providers), **extra)

    def _audit(self, db, job):
        row = db.execute("SELECT * FROM patrol_audit WHERE job=? AND project=?", (job, self.project)).fetchone()
        if row is None:
            raise ValueError("Unrecognized audit job")
        return row

    def request_audit(self, patrol_id, kind, repo, target, sha, question, *, provider, db=None, check=None):
        bounded(question, "audit question", 600)
        assigned = self._target(kind, repo, target, sha)
        assigned["providers"] = sorted(set(assigned["providers"]))
        if (provider not in assigned["providers"] or
                not set(assigned["providers"]).intersection(self.config.audit_order)):
            raise PermissionError("Audit processing lacks consent")
        # Free-form management text can contain material from an unrelated meeting.
        # Forward a scoped question resolved by Core, including the actual target.
        assigned["question"] = {
            "pr": "Review the pinned changes for correctness, compatibility and test coverage.",
            "ci": "Assess the failed run attempt and the related pinned changes.",
            "spec": "Check the technical feasibility and consistency of the assigned specification.",
        }[kind]
        key = digest([self.project, kind, repo, target, sha, assigned.get("base"),
                      assigned.get("attempt"), assigned["providers"], "review-v1"])
        with self._db(db) as conn:
            self._fence(conn, check)
            self._consent(assigned, provider)
            job = self.jobs.enqueue(self.project, "audit", "audit", key, assigned,
                                    assigned["providers"], target_sha=sha, db=conn)
            cur = conn.execute("INSERT OR IGNORE INTO patrol_audit VALUES(?,?,?,?,?,NULL,NULL)",
                               (job, self.project, kind, target, json_data(assigned)))
            return {"job_id": job, "deduplicated": cur.rowcount == 0}

    def _consent(self, assigned, provider):
        if assigned["kind"] == "spec":
            current = self.workflow.providers_for(assigned["target"], assigned["sha"])
        else:
            current = self.config.privacy.project_providers
        if provider not in assigned["providers"] or provider not in current:
            raise PermissionError("Provider lacks consent for this audit")

    def audit_context(self, job_id, *, provider, db=None, check=None):
        with self._db(db) as conn:
            assigned = json.loads(self._audit(conn, job_id)["input"])
            self._consent(assigned, provider)
            repo, sha, kind = assigned["repo"], assigned["sha"], assigned["kind"]
            evidence = []

            def entry(source, location, content, url=None):
                evidence.append(dict(id=digest([sha, source, location, content])[:24],
                                     kind=source, path=location, text=content, url=url))

            if kind == "spec":
                spec = self.workflow.spec_for_audit(assigned["target"], sha, provider)
                markdown = spec.get("markdown", spec.get("markup"))
                entry("spec", "approved-private-workspace", markdown[:24_000])
                truncated = len(markdown) > 24_000
            else:
                if kind == "ci":
                    entry("run", f"actions/runs/{assigned['target']}",
                          f"Failed workflow attempt {assigned['attempt']} at commit {sha}.",
                          f"https://github.com/{repo}/actions/runs/{assigned['target']}/attempts/{assigned['attempt']}")
                    route = f"/repos/{repo}/commits/{sha}"
                else:
                    route = f"/repos/{repo}/compare/{assigned['base']}...{sha}"
                files = self.github.request("GET", route).get("files", [])
                truncated = not files
                for file in files:
                    path = file.get("filename", "")
                    if (not isinstance(path, str) or len(path) > 220 or not path.isascii() or
                            not re.fullmatch(r"[\w./+@ -]+", path) or path.startswith("/") or
                            any(part in ("", ".", "..") or SENSITIVE_NAME.search(part) for part in path.split("/")) or
                            Path(path).suffix.lower() not in TEXT_FILES or not file.get("patch") or len(evidence) >= 8):
                        truncated = True
                        continue
                    patch = file["patch"]
                    hunk = re.search(r"^@@ -[0-9]+(?:,[0-9]+)? \+([1-9][0-9]*)(?:,[0-9]+)? @@", patch, re.M)
                    url = f"https://github.com/{repo}/blob/{sha}/{quote(path)}"
                    if hunk:
                        url += "#L" + hunk.group(1)
                    entry("diff", path, safe_diff(patch), url)
                    truncated |= len(patch) > 4096 or len(patch.splitlines()) > 60
            # Retain evidence anchors, not copies of potentially private code.
            anchors = [{k: v for k, v in item.items() if k != "text"} for item in evidence]
            self._fence(conn, check)
            self._consent(assigned, provider)
            conn.execute("UPDATE patrol_audit SET evidence=? WHERE job=?", (json_data(anchors), job_id))
            return {**{k: assigned[k] for k in ("kind", "repo", "target", "sha", "question")},
                    "job_id": job_id, "evidence": evidence, "truncated": truncated}

    def cli_snapshot(self, job_id, *, provider, lease_token):
        """Get bounded evidence for the worker's disposable, read-only JSON mount."""
        def check(conn):
            job = self.jobs.require_lease(job_id, provider, lease_token, db=conn)
            if (job["project"] != self.project or job["kind"] != "audit" or job["role"] != "audit" or
                    job["payload"]["kind"] not in {"pr", "ci"}):
                raise PermissionError("Only the assigned product audit can mount evidence")
            return job

        # Check before any network access as well as at the fenced snapshot write.
        check(None)
        context = self.audit_context(job_id, provider=provider, check=check)
        return {"job_id": job_id, "sha": context["sha"], "truncated": context["truncated"],
                "evidence": [{key: entry[key] for key in ("id", "path", "text")}
                             for entry in context["evidence"] if entry["kind"] == "diff"]}

    def submit_result(self, job_id, findings, *, provider, db=None):
        if not isinstance(findings, list) or len(findings) > 15:
            raise ValueError("Expected up to 15 audit findings")
        with self._db(db) as conn:
            row = self._audit(conn, job_id)
            if row["evidence"] is None:
                raise ValueError("Read the assigned audit context first")
            assigned = json.loads(row["input"])
            self._consent(assigned, provider)
            anchors = {item["id"]: item for item in json.loads(row["evidence"])}
            clean, ids = [], set()
            for finding in findings:
                if not isinstance(finding, dict) or set(finding) != {
                    "evidence_id", "category", "severity", "summary", "impact", "acceptance"
                }:
                    raise ValueError("Unexpected audit finding fields")
                anchor = anchors.get(finding["evidence_id"])
                if (not anchor or finding["category"] not in CATEGORIES or
                        finding["severity"] not in ("low", "medium", "high")):
                    raise ValueError("Unknown audit evidence, category or severity")
                for key in ("summary", "impact", "acceptance"):
                    bounded(finding[key], key, 600)
                fingerprint = digest([assigned["repo"], assigned["kind"], assigned["target"],
                                      anchor["path"], finding["category"]])
                if fingerprint in ids:
                    raise ValueError("Duplicate finding at this evidence location")
                ids.add(fingerprint)
                clean.append({**finding, "finding_id": fingerprint, "path": anchor["path"],
                              "evidence_url": anchor["url"]})
            result = {"audit_id": job_id, "sha": assigned["sha"], "findings": clean}
            result["result_id"] = digest(result)
            if row["result"] and json.loads(row["result"]) != result:
                raise Conflict("The audit result is immutable")
            conn.execute("UPDATE patrol_audit SET result=? WHERE job=?", (json_data(result), job_id))
            return result

    def audit_result(self, job_id, *, provider, db=None):
        with self._db(db) as conn:
            row = self._audit(conn, job_id)
            assigned = json.loads(row["input"])
            self._consent(assigned, provider)
            if not row["result"]:
                raise Conflict("Audit result unavailable")
            return json.loads(row["result"])

    def _finding(self, db, job, finding_id, provider):
        row = self._audit(db, job)
        assigned = json.loads(row["input"])
        result = self.audit_result(job, provider=provider, db=db)
        finding = next((f for f in result["findings"] if f["finding_id"] == finding_id), None)
        if not finding or finding["severity"] == "low" or assigned["kind"] == "spec":
            raise PermissionError("Only evidenced product findings of medium or high severity may act")
        current = self._target(assigned["kind"], assigned["repo"], assigned["target"], assigned["sha"])
        if (current.get("base"), current.get("attempt")) != (assigned.get("base"), assigned.get("attempt")):
            raise Conflict("Stale audit result")
        return assigned, finding

    def _post(self, patrol_id, job, finding_id, mode, *, provider, check, db=None):
        # Serialize marker lookups with the subsequent request in this single Core instance.
        # Other workers can heartbeat while GitHub returns the read-only pages.
        with self._outbound_lock, self._db(db) as conn:
            assigned, finding = self._finding(conn, job, finding_id, provider)
            repo, target, sha = assigned["repo"], assigned["target"], assigned["sha"]
            if mode == "comment" and assigned["kind"] != "pr":
                raise ValueError("An assigned pull request is required")
            key = digest([self._nonce, mode, finding_id, sha if mode == "comment" else None])
            old = conn.execute("SELECT receipt FROM patrol_action WHERE project=? AND id=?",
                               (self.project, key)).fetchone()
            if old:
                self._fence(conn, check)
                return json.loads(old[0])
            route = f"/repos/{repo}/issues"
            if mode == "comment":
                route = f"/repos/{repo}/pulls/{target}/reviews"
            marker = f"<!-- sprint-relay-audit:{key} -->"
            matches = [item for item in self.github._pages(route, {"state": "all"} if mode == "issue" else {})
                       if not item.get("pull_request") and (item.get("body") or "").startswith(marker + "\n")]
            if len(matches) > 1:
                raise Conflict("Ambiguous audit action marker")
            label, condition = CATEGORIES[finding["category"]]
            if matches:
                self._fence(conn, check)
                item = matches[0]
            else:
                # Copy no free-form AI text, transcripts, source excerpts or CI logs to GitHub.
                # The source anchor is from the target repository's own pinned revision.
                body = (f"{marker}\nAI-generated technical review · {finding['severity']} severity\n\n"
                        f"{assigned['kind'].upper()} #{target} at `{sha}` · `{finding['path']}`\n\n"
                        f"Evidence: {finding['evidence_url']}\n\nPotential impact: {label}.\n\n"
                        f"Acceptance: {condition}\n\nPrivate review reference: `{finding_id}`")
                request = {"body": body}
                if mode == "comment":
                    request.update(event="COMMENT", commit_id=sha)
                if mode == "issue":
                    request["title"] = f"[SprintRelay] {label} in {assigned['kind'].upper()} #{target}"
                self._finding(conn, job, finding_id, provider)  # Recheck the remote head before posting.
                self._fence(conn, check)
                item = self.github.request("POST", route, request)
            number = item["number"] if mode == "issue" else item["id"]
            receipt = dict(receipt=key, status="posted", repo=repo, number=number)
            conn.execute("INSERT INTO patrol_action VALUES(?,?,?)", (key, self.project, json_data(receipt)))
            return receipt

    def repair(self, patrol_id, job_id, finding_id, *, provider, check, db=None):
        return self._post(patrol_id, job_id, finding_id, "issue", provider=provider, check=check, db=db)

    def comment(self, patrol_id, job_id, finding_id, *, provider, check, db=None):
        return self._post(patrol_id, job_id, finding_id, "comment", provider=provider, check=check, db=db)

    def priority(self, patrol_id, job_id, finding_id, task_id, expected_version, priority, reason=None,
                 *, provider, check, db=None):
        if type(priority) is not int or not -100 <= priority <= 100:
            raise ValueError("Priority must be -100 through 100")
        with self._db(db) as conn:
            assigned, _ = self._finding(conn, job_id, finding_id, provider)
            records, sha = self.github._load()
            if task_id not in records:
                raise ValueError("Unknown task")
            from .core import Task
            record = records[task_id]
            task = Task.from_dict(record["task"])
            path = "pull" if assigned["kind"] == "pr" else "actions/runs"
            link = f"https://github.com/{assigned['repo']}/{path}/{assigned['target']}"
            if task.repo != assigned["repo"] or link not in task.links or task.status in {"Done", "Cancelled"}:
                raise PermissionError("Finding must relate to an open task's linked artifact")
            key = digest(["priority", task_id, expected_version, priority, finding_id])
            if key not in record.get("applied", []):
                if digest(task.to_dict()) != expected_version:
                    raise Conflict("Stale task version")
                record["task"] = replace(task, priority=priority).to_dict()
                record.setdefault("applied", []).append(key)
                self._fence(conn, check)
                self.github._store(records, sha, "Adjust SprintRelay priority using verified audit evidence")
            else:
                self._fence(conn, check)
            return {"receipt": key, "status": "applied", "repo": task.repo, "number": task.issue}

    def report(self, patrol_id, summary, references, *, provider, check, db=None):
        bounded(summary, "patrol summary", 2048)
        if not isinstance(references, list) or len(references) > 50:
            raise ValueError("Invalid patrol references")
        with self._db(db) as conn:
            row = conn.execute("SELECT changes, completed_audits FROM patrol_scan WHERE project=? AND id=?",
                               (self.project, patrol_id)).fetchone()
            if not row:
                raise ValueError("Read the patrol snapshot first")
            audits = json.loads(row["completed_audits"])
            changes = json.loads(row["changes"])
            items = {item["id"] for item in changes}
            for ref in references:
                if ref in audits:
                    self.audit_result(ref, provider=provider, db=conn)
                elif ref not in items:
                    raise ValueError("Report references must identify items in the patrol snapshot")
            key = "patrol:" + patrol_id
            # Discord task channels can have a wider audience than a meeting.
            text = (f"SprintRelay patrol: {len(changes)} changed items, {len(audits)} completed audits, "
                    f"{len(references)} referenced items.")
            check()
            data = {"patrol_id": patrol_id, "provider": provider, "summary": summary, "references": references}
            signature = digest(data)
            old = conn.execute("SELECT digest FROM artifacts WHERE project=? AND id=?", (self.project, key)).fetchone()
            if old and old[0] != signature:
                raise Conflict("An immutable report already exists for this patrol")
            conn.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?)",
                         (key, self.project, "patrol_report", signature, json_data(data), self.now().timestamp()))
            conn.execute("INSERT OR IGNORE INTO relay_notice(id,body) VALUES(?,?)", (key, text))
            return {"receipt": key, "status": "queued"}
