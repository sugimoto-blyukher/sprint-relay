"""SQLite job leases, provider eligibility and patrol checkpoints."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL,
        idem TEXT NOT NULL, payload TEXT NOT NULL, providers TEXT NOT NULL, target_sha TEXT,
        state TEXT NOT NULL, provider TEXT, priority INTEGER NOT NULL, attempts INTEGER DEFAULT 0,
        next_attempt_at REAL NOT NULL, lease_hash TEXT, lease_until REAL, fence_generation INTEGER DEFAULT 0,
        created REAL NOT NULL, started REAL, finished REAL, error_class TEXT, result_ref TEXT,
        UNIQUE(project,kind,idem))""",
    "CREATE INDEX IF NOT EXISTS job_ready ON jobs(project,state,next_attempt_at,priority)",
    """CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY, created REAL NOT NULL, project TEXT NOT NULL, actor TEXT NOT NULL,
        provider TEXT, job_id TEXT, operation TEXT NOT NULL, outcome TEXT NOT NULL, target TEXT)""",
    """CREATE TABLE IF NOT EXISTS job_attempts(
        job_id TEXT REFERENCES jobs(id), generation INTEGER, provider TEXT NOT NULL, started REAL NOT NULL,
        finished REAL, outcome TEXT, PRIMARY KEY(job_id,generation))""",
    """CREATE TABLE IF NOT EXISTS artifacts(
        id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL,
        created REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS agent_health(
        provider TEXT, role TEXT, failures INTEGER NOT NULL, reason TEXT NOT NULL, retry_at REAL NOT NULL,
        PRIMARY KEY(provider,role))""",
    """CREATE TABLE IF NOT EXISTS patrol_slots(
        project TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), sequence INTEGER NOT NULL,
        interval_minutes INTEGER NOT NULL, next_at REAL NOT NULL, last_success REAL, checkpoint TEXT)""",
    """CREATE TABLE IF NOT EXISTS muse_challenge(
        project TEXT PRIMARY KEY, challenge TEXT NOT NULL, expires REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS muse_proof(
        receipt TEXT PRIMARY KEY, project TEXT NOT NULL, verified REAL NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS muse_contact(project TEXT PRIMARY KEY,seen REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS cli_proof(provider TEXT,role TEXT,verified REAL,PRIMARY KEY(provider,role))",
    "CREATE TABLE IF NOT EXISTS job_control(project TEXT PRIMARY KEY, paused INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS job_outbox(
        id INTEGER PRIMARY KEY, project TEXT NOT NULL, message TEXT NOT NULL, sent INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS job_route(
        project TEXT, role TEXT, provider TEXT, PRIMARY KEY(project,role))""",
)

TERMINAL_ERRORS = {"PERMISSION", "INPUT", "SECURITY", "CONTRADICTION"}
SOFT_ERRORS = {"TIMEOUT", "FORMAT", "TRANSIENT"}
SHARED_ERRORS = {"AUTH", "QUOTA", "OUTAGE", "UNAVAILABLE"}
ERRORS = TERMINAL_ERRORS | SOFT_ERRORS | SHARED_ERRORS | {"DEPENDENCY", "CAPABILITY"}
AUDIT_OPERATIONS = frozenset("""
    get_project_context list_workflow_jobs claim_job heartbeat_job get_transcript submit_minutes
    get_specification submit_specification submit_validation submit_task_plan claim_patrol get_patrol_delta
    get_tasks get_schedule get_progress request_audit get_audit_status get_audit_context submit_audit_result
    get_audit_result propose_or_create_issue post_review_comment update_task_priority publish_patrol_report
    complete_patrol report_job_failure unknown_tool pause resume patrol_requested claim finish
    provider_unavailable provider_recovered probe_retry
""".split())
AUDIT_OUTCOMES = {"allowed", "denied", "invalid", "retry", "conflict", "failed", "stale",
                  "success", "FAILED", "RETRY_WAIT", "CANCELLED", "COMPLETED"} | ERRORS
ACTIVE = ("PENDING", "RETRY_WAIT", "RUNNING")
RETRYABLE_LOCAL_PROBE_ERRORS = {"CAPABILITY", "FORMAT", "UNTESTED"}
ROLE_KINDS = {
    "management": {"minutes", "specification", "validation", "task_plan", "patrol", "probe"},
    "audit": {"audit", "probe"},
    "system": {"publish", "notification"},
}


def migrate_jobs(db):
    for statement in SCHEMA:
        db.execute(statement)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class JobStore:
    def __init__(self, path, config, *, clock=time.time):
        self.path = Path(path)
        self.config = config
        self.clock = clock
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if self.path.stat().st_mode & 0o077:
            raise PermissionError("Job state requires file mode 0600")
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            migrate_jobs(db)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, db=None, *, write=True):
        if db is not None:
            yield db
            return
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def audit(self, project, actor, provider, job_id, operation, outcome, target=None, *, db=None):
        """Write only bounded identifiers, independently of the untrusted action payload."""
        known_provider = provider if provider in ("muse", "claude_code", "codex", "antigravity", "core") else None
        known_project = project if project == self.config.management_repo else "other_project"
        actor = str(actor) if actor is not None else "system"
        if actor == "system" or (actor.isdecimal() and len(actor) <= 20 and
                                 (actor in self.config.members or actor in self.config.approvers)):
            safe_actor = actor
        else:
            safe_actor = "principal:" + _hash(actor)[:20]
        job_id = job_id if isinstance(job_id, str) and re.fullmatch(r"[a-f0-9]{32}", job_id) else None
        target = target if isinstance(target, str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", target) else None
        with self.transaction(db) as conn:
            if job_id and not conn.execute("SELECT 1 FROM jobs WHERE project=? AND id=?", (project, job_id)).fetchone():
                job_id = None
            conn.execute("""INSERT INTO audit_log(created,project,actor,provider,job_id,operation,outcome,target)
                            VALUES (?,?,?,?,?,?,?,?)""",
                         (self.clock(), known_project, safe_actor, known_provider, job_id,
                          operation if operation in AUDIT_OPERATIONS else "unknown_tool",
                          outcome if outcome in AUDIT_OUTCOMES else "failed", target))

    def _job(self, row):
        if row is None:
            raise ValueError("Unknown job")
        job = dict(row)
        job.pop("lease_hash")
        for name in ("payload", "providers"):
            job[name] = json.loads(job[name])
        if job["kind"] == "patrol":
            # A slot carries project data, never meeting-specific consent.
            job["providers"] = list(self.config.privacy.project_providers)
        return job

    def get(self, job_id, db=None):
        with self.transaction(db, write=False) as conn:
            return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue(self, project, kind, role, key, payload, providers, *, db=None, priority=0, target_sha=None):
        if kind not in ROLE_KINDS.get(role, ()) or not project or not key or not isinstance(payload, dict):
            raise ValueError("Invalid job envelope")
        allowed = set(providers)
        if role == "system" and allowed != {"core"}:
            raise ValueError("System jobs are restricted to Core")
        raw = _json(payload)
        plist = _json(sorted(allowed))
        now = self.clock()
        with self.transaction(db) as conn:
            old = conn.execute("SELECT * FROM jobs WHERE project=? AND kind=? AND idem=?",
                               (project, kind, key)).fetchone()
            if old:
                if (old["role"], old["payload"], old["providers"], old["target_sha"]) != (role, raw, plist, target_sha):
                    raise ValueError("Idempotency key already used for different input")
                return old["id"]
            job_id = uuid.uuid4().hex
            conn.execute("""INSERT INTO jobs(id,project,kind,role,idem,payload,providers,target_sha,
                priority,state,created,next_attempt_at) VALUES(?,?,?,?,?,?,?,?,?,'PENDING',?,?)""",
                (job_id, project, kind, role, key, raw, plist, target_sha, priority, now, now))
            return job_id

    def _configured(self, provider, role, *, probe=False):
        if role == "system":
            return provider == "core"
        settings = self.config.agents.get(provider)
        order = self.config.management_order if role == "management" else self.config.audit_order
        return bool(provider in order and settings and settings.enabled and settings.terms_confirmed
                    and (probe or (settings.verified and (provider != "muse" or settings.schedule_confirmed))))

    def _ill(self, db, provider, role):
        return db.execute("SELECT * FROM agent_health WHERE provider=? AND role IN (?, '*')",
                          (provider, role)).fetchall()

    def _route(self, db, job):
        role = job["role"]
        if role == "system":
            return "core"
        if self._paused(db, job["project"]):
            return None
        order = self.config.management_order if role == "management" else self.config.audit_order
        for provider in order:
            if not self._configured(provider, role, probe=job["kind"] == "probe"):
                continue
            health = self._ill(db, provider, role)
            if job["kind"] == "probe":
                explicit = job["payload"].get("operator_retry") is True
                if (provider in job["providers"] and (health or explicit)
                        and all(self._probe_ready(h, explicit) for h in health)):
                    return provider
                continue
            if provider not in self.config.privacy.project_providers:
                continue
            if job["kind"] != "patrol" and provider not in job["providers"]:
                continue
            if health:
                continue
            if provider != "muse" and not db.execute(
                    "SELECT 1 FROM cli_proof WHERE provider=? AND role=?", (provider, role)).fetchone():
                continue
            return provider
        return None

    def _available(self, db, job, provider):
        if job["state"] not in ("PENDING", "RETRY_WAIT") or job["next_attempt_at"] > self.clock():
            return False
        if self._route(db, job) != provider:
            return False
        return not db.execute("""SELECT 1 FROM jobs WHERE state='RUNNING'
                AND (role=? OR provider=?) LIMIT 1""", (job["role"], provider)).fetchone()

    def list_jobs(self, project, *, role=None, provider=None, kinds=None, limit=50, cursor=None, db=None, include_busy=False):
        with self.transaction(db, write=False) as db:
            query = "SELECT * FROM jobs WHERE project=? AND id>?"
            args = [project, cursor or ""]
            if role:
                query += " AND role=?"
                args.append(role)
            if provider:
                query += " AND state IN ('PENDING','RETRY_WAIT')"
            result = []
            for row in db.execute(query + " ORDER BY id", args):
                job = self._job(row)
                if kinds and job["kind"] not in kinds:
                    continue
                if provider and include_busy:
                    if job["next_attempt_at"] > self.clock() or self._route(db, job) != provider:
                        continue
                if not provider or include_busy or self._available(db, job, provider):
                    result.append(job)
                    if len(result) >= min(200, max(1, limit)):
                        break
            return result

    def claim(self, job_id, provider):
        with self.transaction() as db:
            return self._claim(db, self.get(job_id, db), provider)

    def _claim(self, db, job, provider):
        if not self._available(db, job, provider):
            raise PermissionError("Job unavailable to this provider")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        deadline = now + self.config.jobs.timeout_seconds
        lease_until = min(deadline, now + max(10, 2 * self.config.watchdog.interval_seconds))
        db.execute("""UPDATE jobs SET state='RUNNING',provider=?,lease_hash=?,lease_until=?,started=?,
            fence_generation=fence_generation+1,attempts=attempts+1 WHERE id=?""",
            (provider, _hash(token), lease_until, now, job["id"]))
        db.execute("INSERT INTO job_attempts(job_id,generation,provider,started) VALUES(?,?,?,?)",
                   (job["id"], job["fence_generation"] + 1, provider, now))
        self.audit(job["project"], provider, provider, job["id"], "claim", "success", job["target_sha"], db=db)
        return {**self.get(job["id"], db), "lease_token": token, "deadline": deadline}

    def require_lease(self, job_id, provider, token, *, db=None):
        with self.transaction(db, write=False) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (not row or row["state"] != "RUNNING" or row["provider"] != provider or
                    row["lease_until"] <= self.clock() or not isinstance(token, str) or
                    not hmac.compare_digest(row["lease_hash"], _hash(token)) or
                    (row["role"] != "system" and self._paused(conn, row["project"]))):
                raise PermissionError("Invalid or expired job lease")
            if row["role"] != "system":
                if not self._configured(provider, row["role"], probe=row["kind"] == "probe"):
                    raise PermissionError("Provider disabled")
                if row["kind"] != "probe" and provider not in self.config.privacy.project_providers:
                    raise PermissionError("Project consent withdrawn")
            return self._job(row)

    def cancel(self, job_id, reason, *, db=None):
        """Trusted human or workflow invalidation immediately fences prior work."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Cancellation reason required")
        with self.transaction(db) as conn:
            job = self.get(job_id, conn)
            if job["state"] in ACTIVE:
                self._finish(conn, job, "CANCELLED", "SUPERSEDED")

    def heartbeat(self, job_id, provider, token):
        with self.transaction() as db:
            job = self.require_lease(job_id, provider, token, db=db)
            end = min(job["started"] + self.config.jobs.timeout_seconds,
                      self.clock() + max(10, self.config.watchdog.interval_seconds * 2))
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (end, job_id))
            return end

    def _notice(self, db, project, message):
        db.execute("INSERT INTO job_outbox(project,message) VALUES(?,?)", (project, message))

    def notifications(self, project):
        with self.transaction(write=False) as db:
            return [(r["id"], r["message"]) for r in db.execute(
                "SELECT id,message FROM job_outbox WHERE project=? AND sent=0 ORDER BY id LIMIT 100", (project,))]

    def acknowledge(self, receipt):
        with self.transaction() as db:
            db.execute("UPDATE job_outbox SET sent=1 WHERE id=?", (receipt,))

    def _finish(self, db, job, state, error=None, artifact=None, delay=0):
        now = self.clock()
        db.execute("""UPDATE job_attempts SET finished=?,outcome=? WHERE job_id=? AND generation=?""",
                   (now, error or state, job["id"], job["fence_generation"]))
        db.execute("""UPDATE jobs SET state=?,error_class=?,result_ref=?,finished=?,next_attempt_at=?,
                lease_hash=NULL,lease_until=NULL WHERE id=?""",
                   (state, error, artifact, None if state in ACTIVE else now, now+delay, job["id"]))

        self.audit(job["project"], job["provider"], job["provider"], job["id"], "finish", state, db=db)

    def _recover(self, db, project, provider, role):
        if self._ill(db, provider, role):
            db.execute("DELETE FROM agent_health WHERE provider=? AND role IN (?, '*')", (provider, role))
            self.audit(project, "system", provider, None, "provider_recovered", "success", db=db)
            self._notice(db, project, f"{provider} recovered for {role}; eligible at the next job boundary.")

    def complete(self, job_id, provider, token, result, *, on_submit=None):
        with self.transaction() as db:
            job = self.require_lease(job_id, provider, token, db=db)
            if not isinstance(result, dict):
                raise ValueError("A structured result is required")
            if job["kind"] == "probe":
                if result != {"challenge": job["payload"]["challenge"]}:
                    raise ValueError("Invalid synthetic probe response")
                db.execute("INSERT OR REPLACE INTO cli_proof VALUES(?,?,?)", (provider, job["role"], self.clock()))
                self._recover(db, job["project"], provider, job["role"])
            if job["kind"] == "patrol":
                if not isinstance(result.get("cursor"), (dict, str)):
                    raise ValueError("Validated patrol cursor required")
                changed = db.execute("UPDATE patrol_slots SET checkpoint=?,last_success=? WHERE project=? AND job_id=?",
                                     (_json(result["cursor"]), self.clock(), job["project"], job_id)).rowcount
                if changed != 1:
                    raise ValueError("Stale patrol slot")
            data = _json(result)
            digest = _hash(data)
            artifact = _hash(_json([job_id, digest]))
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                       (artifact, job["project"], job["kind"], digest, data, self.clock()))
            if on_submit:
                on_submit(db, job, provider, result)
            # A slow domain callback may have crossed the lease deadline. Its
            # database writes belong to this transaction and must roll back.
            self.require_lease(job_id, provider, token, db=db)
            self._finish(db, job, "COMPLETED", artifact=artifact)
            return artifact

    def artifact(self, artifact_id, db=None):
        with self.transaction(db, write=False) as conn:
            row = conn.execute("SELECT data FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if not row:
                raise ValueError("Unknown artifact")
            if row["data"] == "null":
                raise ValueError("Artifact contents expired under the retention policy")
            return json.loads(row["data"])

    def fail(self, job_id, provider, token, error, *, retry_after=None):
        with self.transaction() as db:
            job = self.require_lease(job_id, provider, token, db=db)
            return self._failure(db, job, error, retry_after)

    def _failure(self, db, job, error, retry_after=None):
        if error not in ERRORS:
            raise ValueError("Unknown failure classification")
        provider, role = job["provider"], job["role"]
        if error in TERMINAL_ERRORS or (role == "system" and error != "DEPENDENCY"):
            if job["kind"] == "probe":
                # Retire this invalid request. A later commissioning attempt uses
                # an independent synthetic challenge for this same provider.
                self._quarantine(db, job["project"], provider, role, "CAPABILITY")
            self._finish(db, job, "FAILED", error)
            self._notice(db, job["project"], f"{job['kind']} job {job['id']} stopped ({error}); review required.")
            return "FAILED"
        recent = db.execute("""SELECT outcome FROM job_attempts WHERE job_id=? AND provider=?
            AND finished IS NOT NULL ORDER BY generation DESC LIMIT ?""",
            (job["id"], provider, self.config.jobs.max_retries)).fetchall()
        if error in SOFT_ERRORS and len(recent) < self.config.jobs.max_retries and job["kind"] != "probe":
            self._finish(db, job, "RETRY_WAIT", error, delay=1)
            return "RETRY_WAIT"
        if error == "DEPENDENCY":
            state = "RETRY_WAIT" if job["attempts"] <= self.config.jobs.max_retries else "FAILED"
            self._finish(db, job, state, error, delay=self.config.watchdog.interval_seconds)
            self._notice(db, job["project"], f"{job['kind']} job {job['id']}: shared dependency unavailable.")
            return state
        self._quarantine(db, job["project"], provider, role, error, retry_after)
        candidates = self.config.management_order if role == "management" else self.config.audit_order
        recipient_count = max(1, len(set(candidates).intersection(job["providers"])))
        # Permit another complete round after a successful synthetic recovery.
        # A persistently failing real input then goes to human review.
        budget = 2 * recipient_count * (1 + self.config.jobs.max_retries)
        state = "FAILED" if job["kind"] == "probe" or job["attempts"] >= budget else "RETRY_WAIT"
        if state == "FAILED" and job["kind"] != "probe":
            self._notice(db, job["project"], f"Retry budget exhausted for job {job['id']}; review the input.")
        self._finish(db, job, state, error)
        return state

    def _quarantine(self, db, project, provider, role, error, retry_after=None):
        scope = "*" if error in SHARED_ERRORS else role
        previous = db.execute("SELECT failures FROM agent_health WHERE provider=? AND role=?",
                              (provider, scope)).fetchone()
        count = 1 + (previous[0] if previous else 0)
        base = 60 * self.config.watchdog.recovery_interval_minutes
        # Stable per-provider jitter avoids synchronized probes after restart.
        jitter = 1 + int(_hash(f"{provider}:{count}")[:2], 16) / 2550
        seconds = min(base * 2 ** min(12, count-1) * jitter, 60*self.config.watchdog.max_backoff_minutes)
        retry_at = self.clock() + max(seconds, retry_after or 0)
        db.execute("INSERT OR REPLACE INTO agent_health VALUES(?,?,?,?,?)", (provider, scope, count, error, retry_at))
        self.audit(project, "system", provider, None, "provider_unavailable", error, db=db)
        if not previous:
            self._notice(db, project, f"{provider} unavailable ({error})" +
                         (". Sign in again using the official client." if error == "AUTH" else "."))

    def probe_read(self, project, provider, role, *, answer=None):
        """Authenticated Muse commissioning and recovery with no project content."""
        if provider != "muse" or role != "management" or not self._configured(provider, role, probe=True):
            raise PermissionError("Unsupported remote recovery check")
        now = self.clock()
        with self.transaction() as db:
            if answer is None and self._configured(provider, role) and not self._ill(db, provider, role):
                return {}
            row = db.execute("SELECT * FROM muse_challenge WHERE project=?", (project,)).fetchone()
            if answer is None:
                if not row or row["expires"] <= now:
                    challenge = secrets.token_hex(16)
                    db.execute("INSERT OR REPLACE INTO muse_challenge VALUES(?,?,?)", (project, challenge, now+120))
                else:
                    challenge = row["challenge"]
                return {"challenge": challenge, "instruction": "Call get_project_context with probe_answer equal to challenge reversed."}
            if not row or row["expires"] <= now or not isinstance(answer, str) or not hmac.compare_digest(
                answer, row["challenge"][::-1]
            ):
                raise ValueError("Invalid or expired remote challenge")
            db.execute("DELETE FROM muse_challenge WHERE project=?", (project,))
            receipt = uuid.uuid4().hex
            db.execute("INSERT INTO muse_proof VALUES(?,?,?)", (receipt, project, now))
            db.execute("INSERT OR REPLACE INTO muse_contact VALUES(?,?)", (project, now))
            health = self._ill(db, provider, role)
            if health and all(h["retry_at"] <= now for h in health):
                self._recover(db, project, provider, role)
            return {"receipt": receipt, "verified_at": now}

    @staticmethod
    def _paused(db, project):
        row = db.execute("SELECT paused FROM job_control WHERE project=?", (project,)).fetchone()
        return bool(row and row[0])

    def set_paused(self, project, paused, *, actor=None):
        with self.transaction() as db:
            self.audit(project, actor, None, None, "pause" if paused else "resume", "success", db=db)
            db.execute("INSERT OR REPLACE INTO job_control VALUES(?,?)", (project, int(paused)))

    def patrol(self, project, *, force=False, actor=None):
        interval = self.config.patrol.interval_minutes
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM patrol_slots WHERE project=?", (project,)).fetchone()
            if force:
                self.audit(project, actor, None, row["job_id"] if row else None, "patrol_requested", "success", db=db)
            if row:
                job = self.get(row["job_id"], db)
                if row["interval_minutes"] != interval:
                    db.execute("UPDATE patrol_slots SET interval_minutes=?,next_at=? WHERE project=?",
                               (interval, now+interval*60, project))
                    self._notice(db, project, f"Patrol interval changed to {interval} min; update the Muse schedule.")
                    if not force:
                        return job["id"]
                if job["state"] in ACTIVE or (not force and row["next_at"] > now):
                    return job["id"]
            seq = row["sequence"] + 1 if row else 1
            job_id = self.enqueue(project, "patrol", "management", f"slot:{seq}", {"slot": seq},
                                  self.config.privacy.project_providers, db=db)
            db.execute("""INSERT INTO patrol_slots(project,job_id,sequence,interval_minutes,next_at)
                VALUES(?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET job_id=excluded.job_id,
                sequence=excluded.sequence,interval_minutes=excluded.interval_minutes,next_at=excluded.next_at""",
                       (project, job_id, seq, interval, now+interval*60))
            return job_id

    def checkpoint(self, project, *, db=None):
        with self.transaction(db, write=False) as db:
            row = db.execute("SELECT checkpoint FROM patrol_slots WHERE project=?", (project,)).fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def _probe_ready(self, health, explicit=False):
        return health["retry_at"] <= self.clock() or (
            explicit and health["role"] != "*" and health["reason"] in RETRYABLE_LOCAL_PROBE_ERRORS
        )

    def _active_probe(self, db, project, provider, role):
        for row in db.execute("""SELECT * FROM jobs WHERE project=? AND kind='probe' AND role=?
                AND state IN ('PENDING','RETRY_WAIT','RUNNING')""", (project, role)):
            job = self._job(row)
            if job["providers"] == [provider]:
                return job
        return None

    def retry_probe(self, project, provider, role, *, actor):
        """Request a synthetic check without changing health or replacing a lease."""
        if project != self.config.management_repo or str(actor) not in self.config.approvers:
            raise PermissionError("Configured human administrator required")
        if provider == "muse" or role not in (None, "management", "audit"):
            raise ValueError("Choose a local CLI provider and a supported role")
        roles = [r for r in (("management", "audit") if role is None else (role,))
                 if self._configured(provider, r, probe=True)]
        if not roles:
            raise ValueError("Provider and role must be enabled with terms confirmed")
        receipts = []
        with self.transaction() as db:
            if self._paused(db, project):
                raise ValueError("Resume the watchdog before requesting a probe")
            for current_role in roles:
                job = self._active_probe(db, project, provider, current_role)
                waits = [h["retry_at"] for h in self._ill(db, provider, current_role)
                         if not self._probe_ready(h, explicit=True)]
                if job and job["state"] == "RUNNING":
                    state, job_id = "running", job["id"]
                elif waits:
                    state, job_id = "cooldown", job["id"] if job else None
                elif job:
                    state, job_id = "already_queued", job["id"]
                    payload = job["payload"] | {"operator_retry": True}
                    db.execute("UPDATE jobs SET payload=? WHERE id=?", (_json(payload), job_id))
                else:
                    state = "queued"
                    job_id = self.enqueue(project, "probe", current_role,
                                          "operator-probe:" + secrets.token_hex(16),
                                          {"challenge": secrets.token_hex(16), "operator_retry": True},
                                          [provider], db=db, priority=-1000)
                self.audit(project, actor, provider, job_id, "probe_retry",
                           "retry" if state == "cooldown" else "success", db=db)
                receipt = {"role": current_role, "state": state, "job_id": job_id}
                if state == "cooldown":
                    receipt["retry_at"] = max(waits)
                receipts.append(receipt)
        return receipts

    def maintenance(self, project):
        """Fence expired leases, detect missing Muse and schedule bounded recovery probes."""
        now = self.clock()
        self.patrol(project)
        with self.transaction() as db:
            cutoff = now - self.config.privacy.raw_retention_days * 86400
            db.execute("UPDATE artifacts SET data='null' WHERE project=? AND created<? AND data!='null'", (project, cutoff))
            for row in db.execute("SELECT * FROM jobs WHERE project=? AND state='RUNNING' AND lease_until<=?",
                                  (project, now)).fetchall():
                job = self._job(row)
                if job["provider"] == "muse":
                    self._quarantine(db, project, "muse", job["role"], "TIMEOUT")
                    self._finish(db, job, "RETRY_WAIT", "TIMEOUT")
                else:
                    self._failure(db, job, "TIMEOUT")
            if self._paused(db, project):
                return
            for row in db.execute("""SELECT * FROM jobs WHERE project=? AND role='management'
                    AND state IN ('PENDING','RETRY_WAIT') AND next_attempt_at<=?
                    AND MAX(created, COALESCE((SELECT seen FROM muse_contact WHERE project=jobs.project),created))<=?
                    ORDER BY created""", (project, now, now-self.config.patrol.grace_minutes*60)):
                job = self._job(row)
                if self._available(db, job, "muse"):
                    self._quarantine(db, project, "muse", "management", "TIMEOUT")
                    break
            for role, order in (("management", self.config.management_order), ("audit", self.config.audit_order)):
                for provider in order:
                    if provider == "muse" or not self._configured(provider, role, probe=True):
                        continue
                    if not db.execute("SELECT 1 FROM cli_proof WHERE provider=? AND role=?", (provider, role)).fetchone():
                        db.execute("INSERT OR IGNORE INTO agent_health VALUES(?,?,0,'UNTESTED',0)", (provider, role))
            for row in db.execute("SELECT * FROM agent_health WHERE retry_at<=?", (now,)).fetchall():
                provider = row["provider"]
                if provider == "muse":
                    continue
                roles = ("management", "audit") if row["role"] == "*" else (row["role"],)
                for role in roles:
                    if not self._configured(provider, role, probe=True):
                        continue
                    # No project data in probes, even when a project has not consented.
                    key = f"probe:{provider}:{role}:{row['retry_at']}"
                    existing = db.execute("SELECT id FROM jobs WHERE project=? AND kind='probe' AND idem=?",
                                          (project, key)).fetchone()
                    if not existing and not self._active_probe(db, project, provider, role):
                        self.enqueue(project, "probe", role, key, {"challenge": secrets.token_hex(16)},
                                     [provider], db=db, priority=-1000)
                    break

    def dispatchable(self, project, *, busy_providers=()):
        with self.transaction() as db:
            jobs = db.execute("""SELECT * FROM jobs WHERE project=? AND role!='system'
                AND state IN ('PENDING','RETRY_WAIT') AND next_attempt_at<=?
                ORDER BY priority,created,id""", (project, self.clock())).fetchall()
            claims = []
            for row in jobs:
                job = self._job(row)
                provider = self._route(db, job)
                if provider and provider != "muse" and provider not in busy_providers and self._available(db, job, provider):
                    claims.append(self._claim(db, job, provider))
            return claims

    def status(self, project):
        with self.transaction(write=False) as db:
            routes = {}
            for role in ("management", "audit"):
                job = {"kind": "patrol" if role == "management" else "audit", "role": role,
                       "project": project, "providers": self.config.privacy.project_providers}
                routes[role] = self._route(db, job)
            slot = db.execute("SELECT sequence,next_at,last_success FROM patrol_slots WHERE project=?", (project,)).fetchone()
            pending = db.execute("SELECT COUNT(*) FROM jobs WHERE project=? AND state IN ('PENDING','RETRY_WAIT')",
                                 (project,)).fetchone()[0]
            muse_proofs = [dict(r) for r in db.execute(
                "SELECT receipt,verified FROM muse_proof WHERE project=? ORDER BY verified DESC LIMIT 4", (project,))]
            return {"muse_proofs": muse_proofs,
                    "cli_proofs": [dict(r) for r in db.execute("SELECT * FROM cli_proof ORDER BY provider,role")],
                    "active_providers": routes, "degraded": not all(routes.values()), "paused": self._paused(db, project),
                    "pending": pending, "patrol": dict(slot) if slot else None,
                    "health": [dict(row) for row in db.execute("SELECT * FROM agent_health ORDER BY provider,role")]}

    def route_notices(self, project):
        routes = self.status(project)["active_providers"]
        with self.transaction() as db:
            for role, provider in routes.items():
                old = db.execute("SELECT provider FROM job_route WHERE project=? AND role=?", (project, role)).fetchone()
                if old is None or old[0] != provider:
                    self._notice(db, project, f"{role}: {provider or 'degraded; human operations remain available'}.")
                    db.execute("INSERT OR REPLACE INTO job_route VALUES(?,?,?)", (project, role, provider))
