"""GitHub-backed task ledger, issue/Project projections and inbound comment commands."""

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .core import Task, apply_action, assign_owner, validate_tasks
from .issue_format import BODY_LIMIT, render_issue


class GitHubError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"GitHub ({status}): {message}")


class Conflict(ValueError):
    """The caller must read the current task and reconsider its change."""


class PlanConflict(Conflict):
    """The approved graph change has not been applied and may be reclassified."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(token):
    # Never forward a credential to a new origin or follow an unvalidated route.
    opener = build_opener(NoRedirect())

    def request(method, path, data=None):
        req = Request(
            "https://api.github.com" + path,
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "SprintRelay",
            },
        )
        try:
            with opener.open(req, timeout=20) as response:
                raw = response.read(12_000_001)
                if len(raw) > 12_000_000:
                    raise GitHubError(413, "response exceeds read budget")
        except HTTPError as exc:
            # GitHub error payloads can contain request data. Keep diagnostics minimal.
            raise GitHubError(exc.code, exc.reason) from exc
        return json.loads(raw) if raw else None

    return request


def encode(text):
    return base64.b64encode(text.encode()).decode()


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class GitHub:
    """Deploy one writer process. GitHub blob SHAs protect against stale writes."""

    def __init__(self, config, token, db_path, *, product_token=None, request=None):
        self.config = config
        if request is None and (not token or not product_token or token == product_token):
            raise ValueError("Set distinct GitHub management and product credentials")
        self._management = request or transport(token)
        self._product = request or transport(product_token)
        self._lock = threading.RLock()
        if str(db_path) != ":memory:":
            file = Path(db_path)
            file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.close(os.open(file, os.O_CREAT | os.O_RDWR, 0o600))
            if file.stat().st_mode & 0o077:
                raise PermissionError("GitHub adapter database must have mode 0600 or stricter")
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15)
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS relay_notice "
                "(id TEXT PRIMARY KEY, body TEXT NOT NULL, sent INTEGER DEFAULT 0)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS relay_projection (key TEXT PRIMARY KEY, hash TEXT)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS relay_issue_cache (key TEXT PRIMARY KEY, title TEXT, body_hash TEXT)"
            )
        self._ledger = f"/repos/{config.management_repo}/contents/.sprint-relay/tasks.json"

    def request(self, method, path, data=None):
        """Only the configured repository and known operation select a credential."""
        route = path.partition("?")[0]
        match = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", route)
        if not match:
            raise PermissionError("Unrecognized GitHub resource")
        repo, suffix = match.groups()
        suffix = suffix or ""
        if repo == self.config.management_repo:
            doc = r"/contents/\.sprint-relay/(?:tasks\.json|specs/[0-9a-f]{64}/[0-9a-f]{64}\.md)"
            if ((method == "GET" and not suffix) or
                    (method in ("GET", "PUT") and re.fullmatch(doc, suffix))):
                return self._management(method, path, data)
        elif repo in self.config.product_repos:
            reads = (
                r"", r"/issues(?:/[1-9][0-9]*(?:/comments)?)?", r"/pulls(?:/[1-9][0-9]*)?",
                r"/actions/runs(?:/[1-9][0-9]*)?", r"/commits/[0-9a-f]{40}",
                r"/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}",
            )
            if ((method == "GET" and any(re.fullmatch(rule, suffix) for rule in reads)) or
                    (method == "POST" and re.fullmatch(r"/issues(?:/[1-9][0-9]*/comments)?", suffix)) or
                    (method == "PATCH" and re.fullmatch(r"/issues/[1-9][0-9]*", suffix))):
                return self._product(method, path, data)
        raise PermissionError("GitHub operation outside configured scope")

    def is_private(self, repo):
        value = self.request("GET", f"/repos/{repo}").get("private")
        if type(value) is not bool:
            raise GitHubError(503, "repository visibility unavailable")
        return value

    def _load(self):
        try:
            response = self.request("GET", self._ledger)
        except GitHubError as exc:
            if exc.status == 404:
                return {}, None
            raise
        data = json.loads(base64.b64decode(response["content"]))
        if data["version"] != 1:
            raise ValueError("Unsupported SprintRelay ledger version")
        return data["records"], response["sha"]

    def _store(self, records, sha, reason):
        content = json.dumps({"version": 1, "records": records}, ensure_ascii=False,
                             indent=2, sort_keys=True) + "\n"
        data = {"message": reason, "content": encode(content)}
        if sha:
            data["sha"] = sha
        try:
            result = self.request("PUT", self._ledger, data)
        except GitHubError as exc:
            if exc.status in (409, 422):
                raise Conflict("Task ledger changed; refresh and retry") from exc
            raise
        return result["content"]["sha"]

    def list_tasks(self):
        # The remote blob is an atomic snapshot. Readers do not block the poller's local writes.
        records, _ = self._load()
        return [Task.from_dict(records[key]["task"]) for key in sorted(records)]

    tasks = list_tasks

    def _pages(self, route, params=None):
        page = 1
        while True:
            query = urlencode({**(params or {}), "per_page": 100, "page": page})
            items = self.request("GET", route + "?" + query)
            yield from items
            if len(items) < 100:
                return
            page += 1

    def publish(self, tasks):
        """Reserve the entire validated batch before making any product issues."""
        return self.publish_changes(tasks, [])

    def publish_changes(self, new_tasks, updates, *, operation_id=None):
        """Validate and commit a whole dependency-graph change in one GitHub revision."""
        tasks = list(new_tasks)
        updates = list(updates)
        with self._lock:
            records, sha = self._load()
            added = False
            seen = set()
            for candidate in tasks:
                task = assign_owner(candidate, self.config)
                repo = task.repo or self.config.default_repo
                if repo not in self.config.product_repos:
                    raise ValueError(f"Repository not configured: {repo}")
                task = replace(task, repo=repo)
                if task.key in seen:
                    raise ValueError(f"duplicate task key: {task.key}")
                seen.add(task.key)
                origin = digest(task.to_dict())
                if task.key in records:
                    if records[task.key]["origin"] != origin:
                        raise Conflict(f"Stable key already used: {task.key}")
                    continue
                if task.issue is not None or task.status != "Todo":
                    raise ValueError("New tasks must be unpublished and Todo")
                records[task.key] = {"task": task.to_dict(), "origin": origin,
                                     "nonce": uuid.uuid4().hex, "comment": 0}
                added = True
            keys = [task.key for task in tasks]
            for expected, replacement in updates:
                key = expected.key
                if key in seen or key != replacement.key or key not in records:
                    raise ValueError(f"Invalid change key: {key}")
                seen.add(key)
                if (expected.repo, expected.issue) != (replacement.repo, replacement.issue):
                    raise ValueError("Cannot change task issue binding")
                if operation_id and operation_id in records[key].get("applied", []):
                    keys.append(key)
                    continue
                current = Task.from_dict(records[key]["task"])
                if current != replacement:
                    if current != expected:
                        raise PlanConflict(f"Task changed: {key}")
                    records[key]["task"] = replacement.to_dict()
                    added = True
                if operation_id:
                    records[key].setdefault("applied", []).append(operation_id)
                    added = True
                keys.append(key)
            validate_tasks([Task.from_dict(r["task"]) for r in records.values()])
            if added:
                try:
                    sha = self._store(records, sha, "Apply approved SprintRelay task plan")
                except Conflict as exc:
                    raise PlanConflict(str(exc)) from exc
            result = []
            for key in keys:
                task, sha = self._ensure_issue(records, key, sha)
                result.append(task)
            return result

    def _find_issue(self, route, key, opening, closing):
        # A lost POST may already have created the issue. A pasted or damaged
        # marker must never bind an arbitrary issue or cause a second creation.
        candidates = []
        for issue in self._pages(route, {"state": "all"}):
            if "pull_request" in issue:
                continue
            body = issue["body"] or ""
            if opening in body or issue["title"].startswith(f"[{key}] "):
                candidates.append(issue)
        if not candidates:
            return None
        lines = (candidates[0]["body"] or "").splitlines()
        if len(candidates) != 1 or opening not in lines or closing not in lines:
            raise Conflict(f"{key}: ambiguous or damaged issue marker; inspect matching issues before retrying")
        return candidates[0]

    def _compose_body(self, task, issue, title, block, opening, closing):
        cached = self._db.execute(
            "SELECT title, body_hash FROM relay_issue_cache WHERE key=?", (task.key,)
        ).fetchone()
        body = issue["body"] or ""
        head, start, remainder = body.partition(opening)
        inside, end, tail = remainder.partition(closing)
        if start and end:
            previous = opening + inside + closing
            body = head + block + tail
            if previous != block and (not cached or digest(previous) != cached[1]):
                body += "\n\n**Previous GitHub issue text**\n\n" + inside.strip()
        else:
            # Keep all unmatched content outside the newly rendered block.
            original = body.replace(opening, "").replace(closing, "").strip()
            body = block + ("\n\n" + original if original else "")
        if issue["title"] != title and (not cached or cached[0] != issue["title"]):
            body += "\n\n**Previous GitHub title** " + issue["title"]
        return body

    def _ensure_issue(self, records, key, sha):
        record = records[key]
        task = Task.from_dict(record["task"])
        marker = f"<!-- sprint-relay:{record['nonce']} -->"
        closing = f"<!-- /sprint-relay:{record['nonce']} -->"
        root = f"/repos/{task.repo}/issues"
        title, managed = render_issue(task, record["nonce"])
        state = "closed" if task.status in {"Done", "Cancelled"} else "open"
        login = self.config.github_users.get(task.assignee)
        # A Discord-only member can claim and complete design work.
        desired = {"title": title, "body": managed}
        if task.issue is None:
            issue = self._find_issue(root, key, marker, closing)
            if issue is None:
                issue = self.request("POST", root, {**desired, "assignees": [login] if login else []})
            task = replace(task, issue=issue["number"])
            record["task"] = task.to_dict()
            sha = self._store(records, sha, f"Bind {key} to issue {issue['number']}")
        else:
            issue = self.request("GET", f"{root}/{task.issue}")
        desired["body"] = self._compose_body(task, issue, desired["title"], managed, marker, closing)
        if len(desired["body"]) > BODY_LIMIT:
            raise Conflict(f"{key}: shorten manual issue notes to fit GitHub's {BODY_LIMIT}-character body limit")
        desired["state"] = state
        changes = {k: v for k, v in desired.items() if issue.get(k) != v}
        if {u["login"].casefold() for u in issue.get("assignees", [])} != (
            {login.casefold()} if login else set()
        ):
            changes["assignees"] = [login] if login else []
        if changes:
            self.request("PATCH", f"{root}/{task.issue}", changes)
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO relay_issue_cache VALUES(?,?,?)",
                             (key, desired["title"], digest(managed)))
        if self.config.project_id:
            try:
                self._project(task, issue["node_id"])
            except (GitHubError, OSError, TimeoutError) as exc:
                self.notify("project:" + key + ":" + str(exc), f"{key}: Projects sync delayed ({exc}); use its issue.")
        return task, sha

    def _graphql(self, query, variables):
        res = self._product("POST", "/graphql", {"query": query, "variables": variables})
        if res.get("errors"):
            # Includes unsupported fields and insufficient project scopes.
            raise GitHubError(200, str(res["errors"][0].get("type", "GraphQL error")))
        return res["data"]

    def _project(self, task, node):
        project = self.config.project_id
        # Refresh unchanged projections hourly to repair manually moved or removed items.
        signature = digest([project, node, task.status, task.forecast_start, task.forecast_end,
                            self.config.calendar.get("timezone", "Asia/Tokyo"), int(time.time()) // 3600])
        cached = self._db.execute("SELECT hash FROM relay_projection WHERE key=?", (task.key,)).fetchone()
        if cached and cached[0] == signature:
            return
        result = self._graphql(
            """mutation($p:ID!, $c:ID!) {addProjectV2ItemById(input:{projectId:$p,contentId:$c})
               {item{id}}}""", {"p": project, "c": node},
        )
        item = result["addProjectV2ItemById"]["item"]["id"]
        result = self._graphql(
            """query($p:ID!) {node(id:$p) {... on ProjectV2 {fields(first:100) {nodes {
               ... on ProjectV2SingleSelectField {id name options{id name}}
               ... on ProjectV2Field {id name dataType}
               }}}}}""", {"p": project},
        )
        project_node = result.get("node")
        if not project_node or "fields" not in project_node:
            raise GitHubError(404, "Project is inaccessible")
        fields = {f["name"]: f for f in project_node["fields"]["nodes"] if f and "name" in f}
        status_field = fields.get("Status", {})
        status = next((o["id"] for o in status_field.get("options", [])
                       if o["name"].casefold() == task.status.casefold()), None)
        values = []
        if status:
            values.append((status_field["id"], {"singleSelectOptionId": status}))
        else:
            self.notify("project-status:" + project + task.status,
                        f'Configure a Projects Status option named "{task.status}".')
        for name, date in (("Start date", task.forecast_start), ("Target date", task.forecast_end)):
            field = fields.get(name, {})
            if field.get("dataType") == "DATE":
                local_date = None
                if date:
                    zone = ZoneInfo(self.config.calendar.get("timezone", "Asia/Tokyo"))
                    local_date = datetime.fromisoformat(date).astimezone(zone).date().isoformat()
                values.append((field["id"], {"date": local_date} if local_date else None))
        for field_id, value in values:
            variables = {"p": project, "i": item, "f": field_id}
            if value is None:
                self._graphql(
                    """mutation($p:ID!, $i:ID!, $f:ID!) {clearProjectV2ItemFieldValue(input:{
                      projectId:$p,itemId:$i,fieldId:$f}) {projectV2Item{id}}}""", variables,
                )
            else:
                self._graphql(
                    """mutation($p:ID!, $i:ID!, $f:ID!, $v:ProjectV2FieldValue!) {
                    updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:$v})
                    {projectV2Item{id}}}""", {**variables, "v": value},
                )
        if status:
            with self._db:
                self._db.execute("INSERT OR REPLACE INTO relay_projection VALUES(?,?)",
                                 (task.key, signature))

    def update(self, task, expected):
        with self._lock:
            records, sha = self._load()
            if (expected.key != task.key or expected.key not in records or
                    Task.from_dict(records[expected.key]["task"]) != expected):
                raise Conflict("Task changed; refresh and retry")
            if (task.repo, task.issue) != (expected.repo, expected.issue):
                raise ValueError("Cannot change task issue binding")
            return self._commit_update(records, sha, task)

    def _commit_update(self, records, sha, task, comment=None):
        record = records[task.key]
        before = Task.from_dict(record["task"])
        record["task"] = task.to_dict()
        if comment is not None:
            record["comment"] = comment
        validate_tasks([Task.from_dict(r["task"]) for r in records.values()])
        sha = self._store(records, sha, f"Update SprintRelay task {task.key}")
        if task.status in {"In Review", "Blocked"} and task.status != before.status:
            self.notify(f"transition:{task.key}:{sha}", f"{task.key} · {task.status}: {task.title}")
        task, _ = self._ensure_issue(records, task.key, sha)
        return task

    def _action(self, key, action, actor, value=None, comment=None):
        records, sha = self._load()
        if key not in records:
            raise ValueError(f"Unknown task: {key}")
        record = records[key]
        task = Task.from_dict(record["task"])
        if comment is not None and comment <= record["comment"]:
            return task
        if action == "start" and any(records[dep]["task"]["status"] != "Done" for dep in task.dependencies):
            raise ValueError("Resolve unfinished or cancelled dependencies before starting")
        task = apply_action(task, action, actor, self.config, value=value)
        return self._commit_update(records, sha, task, comment)

    def action(self, key, action, actor, value=None):
        with self._lock:
            return self._action(key, action, actor, value)

    def archive_spec(self, meeting, digest, markdown):
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("Expected a revision SHA-256")
        path = ".sprint-relay/specs/" + hashlib.sha256(meeting.encode()).hexdigest() + "/" + digest + ".md"
        self.save_spec(path, markdown, "Archive approved SprintRelay specification")

    def save_spec(self, path, markdown, reason):
        repo = self.config.management_repo
        if self.request("GET", f"/repos/{repo}").get("private") is not True:
            raise ValueError("Approved specifications require a private management repository")
        if not path.startswith(".sprint-relay/specs/") or ".." in path or not path.endswith(".md"):
            raise ValueError("Invalid spec archive path")
        route = f"/repos/{repo}/contents/{quote(path)}"
        try:
            previous = self.request("GET", route)
        except GitHubError as exc:
            if exc.status != 404:
                raise
        else:
            if base64.b64decode(previous["content"]).decode() != markdown:
                raise Conflict("Archived revision already exists with different content")
            return
        self.request("PUT", route, {"message": reason, "content": encode(markdown)})

    def notify(self, event_id, text):
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO relay_notice(id,body) VALUES(?,?)", (event_id, text))

    def notifications(self):
        with self._lock:
            return self._db.execute(
                "SELECT id,body FROM relay_notice WHERE sent=0 ORDER BY rowid LIMIT 50"
            ).fetchall()

    def ack(self, event_id):
        with self._lock, self._db:
            self._db.execute("UPDATE relay_notice SET sent=1 WHERE id=?", (event_id,))

    def _comments(self, task, cursor, actors):
        route = f"/repos/{task.repo}/issues/{task.issue}/comments"
        changed = False
        for comment in self._pages(route):
            author = comment.get("user") or {}
            if (author.get("type") not in (None, "User") or comment.get("performed_via_github_app") or
                    not author.get("login") or author["login"].lower().endswith("[bot]")):
                continue
            body = (comment["body"] or "").strip()
            parts = body.split(maxsplit=2)
            if (comment["id"] <= cursor or len(parts) < 2 or parts[0] != "!relay" or
                    comment["user"]["login"].casefold() not in actors):
                continue
            actor = actors[comment["user"]["login"].casefold()]
            event = f"comment:{task.repo}:{comment['id']}"
            if self._db.execute("SELECT 1 FROM relay_notice WHERE id=?", (event,)).fetchone():
                continue
            try:
                self._action(task.key, parts[1], actor, parts[2] if len(parts) == 3 else None,
                             comment=comment["id"])
                changed = True
            except Conflict:
                raise
            except (ValueError, PermissionError) as exc:
                self.notify(event, f"{task.key} GitHub command rejected: {exc}")
        return changed

    def poll(self):
        with self._lock:
            records, sha = self._load()
            actors = {login.casefold(): actor for actor, login in self.config.github_users.items() if login}
            for key in sorted(records):
                try:
                    task, sha = self._ensure_issue(records, key, sha)
                    if self._comments(task, records[key]["comment"], actors):
                        records, sha = self._load()
                except (Conflict, GitHubError) as exc:
                    if isinstance(exc, GitHubError) and exc.status not in (404, 410, 422):
                        raise
                    self.notify(f"sync:{key}:{exc}", f"{key}: issue reconciliation needs attention ({exc}).")
                    records, sha = self._load()
            for repo in sorted({r["task"]["repo"] for r in records.values()}):
                try:
                    runs = self.request("GET", f"/repos/{repo}/actions/runs?status=failure&per_page=20")
                except GitHubError as exc:
                    if exc.status not in (403, 404):
                        raise
                    self.notify("ci-access:" + repo, f"CI status unavailable for {repo} ({exc.status}).")
                    continue
                for run in runs["workflow_runs"]:
                    self.notify(f"ci:{repo}:{run['id']}:{run.get('run_attempt', 1)}",
                                f"CI failed in {repo}: {run['html_url']}")
