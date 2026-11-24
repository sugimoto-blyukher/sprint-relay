"""Reconcile a newly approved plan with ongoing work, retaining historical issues."""

import hashlib
import json
import threading
from dataclasses import replace

from .core import Task, apply_action, validate_task, validate_tasks
from .github import PlanConflict
from .workflow import private_db

FIELDS = ("title", "description", "role", "repo", "deliverables", "acceptance",
          "estimated_hours", "priority", "deadline", "dependencies", "requirement_ids")


def _id(*parts):
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:20]


class ChangeManager:
    def __init__(self, path, config, github):
        self.path = path
        self.config = config
        self.github = github
        self.lock = threading.RLock()
        with private_db(path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS change_batch (
                    digest TEXT PRIMARY KEY, meeting TEXT NOT NULL, plan TEXT NOT NULL,
                    complete INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS change_decision (
                    id TEXT PRIMARY KEY, meeting TEXT NOT NULL, digest TEXT NOT NULL,
                    body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
            """)

    def pending(self, meeting):
        with private_db(self.path) as db:
            rows = db.execute("""SELECT body FROM change_decision WHERE meeting=?
                                 AND state='pending' ORDER BY id""", (meeting,))
            return [json.loads(row["body"]) for row in rows]

    def _build(self, db, meeting, digest, previous, proposed):
        old = {t.key: t for t in previous}
        new = {t.key: t for t in proposed}
        live = {t.key: t for t in self.github.list_tasks()}
        prior = db.execute("""SELECT plan FROM change_batch WHERE meeting=? AND complete=1
                              ORDER BY rowid DESC LIMIT 1""", (meeting,)).fetchone()
        prior_map = json.loads(prior["plan"])["mapping"] if prior else {}
        carry = {c["key"]: c for c in self.pending(meeting)}
        mapping = {}
        changed = {}
        for key, proposal in new.items():
            physical = prior_map.get(key, key)
            mapping[key] = physical
            if key not in old:
                if physical in live:
                    raise ValueError(f"{key}: choose an unused stable key for new work")
                continue
            if physical not in live:
                raise ValueError(f"{key}: approved issue missing; reconcile GitHub state")
            fields = {f for f in FIELDS if getattr(proposal, f) != getattr(old[key], f)}
            fields.update(carry.get(physical, {}).get("patch", {}))
            changed[key] = fields
            if fields and live[physical].status in ("Done", "Cancelled"):
                # Fixed key per revision makes retries safe and preserves the original issue.
                mapping[key] = key[:48] + "-follow-" + _id(digest, key)

        added, updates, decisions = [], [], []

        def decision(task, kind, reason, patch):
            record = {"id": _id(digest, task.key, kind), "key": task.key, "kind": kind,
                      "reason": reason, "patch": patch, "status": task.status,
                      "assignee": task.assignee,
                      "before": {field: getattr(task, field) for field in patch}}
            if patch:
                validate_task(replace(task, **patch))
            decisions.append(record)

        for key, proposal in new.items():
            physical = mapping[key]
            deps = [mapping.get(d, d) for d in proposal.dependencies]
            if key not in old or physical != prior_map.get(key, key):
                if key in old and live[prior_map.get(key, key)].status == "Done":
                    deps.append(prior_map.get(key, key))
                added.append(replace(proposal, key=physical, dependencies=list(dict.fromkeys(deps))))
                continue
            task = live[physical]
            fields = changed[key]
            prior_deps = [prior_map.get(d, d) for d in old[key].dependencies]
            if deps != prior_deps:
                fields.add("dependencies")
            patch = {f: (deps if f == "dependencies" else getattr(proposal, f)) for f in sorted(fields)}
            patch = {k: v for k, v in patch.items() if getattr(task, k) != v}
            if not patch:
                continue
            if "repo" in patch or "role" in patch:
                raise ValueError(f"{key}: create a new task key for a role or repository transfer")
            if task.status in ("In Progress", "Blocked"):
                decision(task, "owner", "Owner decision required for ongoing work", patch)
            elif task.status == "In Review":
                decision(task, "review", "Review in progress; owner may accept the revised scope", patch)
            elif task.status in ("Done", "Cancelled"):
                # Historical records never acquire dependencies on newer follow-ups.
                continue
            elif "deadline" in patch:
                decision(task, "deadline", "Explicit confirmation required for a contractual date", patch)
            else:
                if "estimated_hours" in patch:
                    patch["remaining_hours"] = patch["estimated_hours"]
                updates.append((task, replace(task, **patch)))

        removed = {prior_map.get(key, key) for key in old.keys() - new.keys()}
        removed.update(key for key, item in carry.items()
                       if item["kind"] == "cancel" and key not in mapping.values())
        for physical in sorted(removed):
            task = live.get(physical)
            if task is not None and task.status not in ("Done", "Cancelled"):
                decision(task, "cancel", "Removed from approved scope; consider cancellation", {})
        resulting = {**live, **{t.key: t for t in added}, **{t.key: t for _, t in updates}}
        validate_tasks(list(resulting.values()))
        return {"mapping": mapping, "new": [t.to_dict() for t in added],
                "updates": [[a.to_dict(), b.to_dict()] for a, b in updates],
                "decisions": decisions}

    def preview(self, meeting, digest, previous, proposed):
        """Reject an infeasible change before the revision becomes immutable."""
        with self.lock, private_db(self.path) as db:
            self._build(db, meeting, digest, previous, proposed)

    def published_keys(self, digest, default):
        with private_db(self.path) as db:
            row = db.execute("SELECT plan FROM change_batch WHERE digest=?", (digest,)).fetchone()
        return set(json.loads(row["plan"])["mapping"].values()) if row else default

    def reconcile(self, meeting, digest, previous, proposed):
        with self.lock:
            with private_db(self.path) as db:
                row = db.execute("SELECT * FROM change_batch WHERE digest=?", (digest,)).fetchone()
                if row is None:
                    plan = self._build(db, meeting, digest, previous, proposed)
                    db.execute("INSERT INTO change_batch(digest,meeting,plan) VALUES(?,?,?)",
                               (digest, meeting, json.dumps(plan)))
                else:
                    plan = json.loads(row["plan"])
            if row is None or not row["complete"]:
                try:
                    self.github.publish_changes(
                        [Task.from_dict(t) for t in plan["new"]],
                        [(Task.from_dict(a), Task.from_dict(b)) for a, b in plan["updates"]],
                        operation_id=digest,
                    )
                except PlanConflict:
                    # Pre-write failure: reclassify the approved plan on retry,
                    # since a task might have started and now require consent.
                    with private_db(self.path) as db:
                        db.execute("DELETE FROM change_batch WHERE digest=? AND complete=0", (digest,))
                    raise
                for item in plan["decisions"]:
                    self.github.notify("change:" + item["id"], f"{item['key']}: {item['reason']} ({item['id']})")
                with private_db(self.path) as db:
                    db.execute("""UPDATE change_decision SET state='superseded' WHERE meeting=?
                                  AND digest!=? AND state='pending'""", (meeting, digest))
                    for item in plan["decisions"]:
                        db.execute("INSERT OR IGNORE INTO change_decision(id,meeting,digest,body) VALUES(?,?,?,?)",
                                   (item["id"], meeting, digest, json.dumps(item)))
                    db.execute("UPDATE change_batch SET complete=1 WHERE digest=?", (digest,))
            keys = set(plan["mapping"].values())
            return [t for t in self.github.list_tasks() if t.key in keys]

    def resolve(self, change_id, actor, approve=True):
        actor = str(actor)
        with self.lock, private_db(self.path) as db:
            row = db.execute("SELECT * FROM change_decision WHERE id=?", (change_id,)).fetchone()
            if row is None or row["state"] != "pending":
                raise ValueError("Change already decided, superseded, or unknown")
            item = json.loads(row["body"])
            task = next((t for t in self.github.list_tasks() if t.key == item["key"]), None)
            if task is None:
                raise ValueError("Task no longer exists")
            if item["kind"] in ("owner", "review"):
                if task.assignee is None or actor != task.assignee or actor != item["assignee"]:
                    raise PermissionError("Current owner decision required")
            elif actor not in self.config.approvers:
                raise PermissionError("An authorized approver must decide")
            if approve:
                if item["kind"] == "cancel":
                    if task.status != "Cancelled":
                        if task.status != item["status"]:
                            raise ValueError("Task status changed; review the current task")
                        self.github.update(apply_action(task, "cancel", actor, self.config,
                                                        item["reason"]), expected=task)
                else:
                    patch = item["patch"]
                    if not all(getattr(task, k) == v for k, v in patch.items()):
                        if task.status != item["status"] or any(
                            getattr(task, k) != v for k, v in item["before"].items()
                        ):
                            raise ValueError("Task changed; submit a revised plan")
                        if task.status == "Todo" and "estimated_hours" in patch:
                            patch["remaining_hours"] = patch["estimated_hours"]
                        self.github.update(replace(task, **patch), expected=task)
            db.execute("UPDATE change_decision SET state=? WHERE id=?",
                       ("accepted" if approve else "declined", change_id))
