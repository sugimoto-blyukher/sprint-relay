# Meeting to approved issues

Use the configured private meetings channel. Restrict access to the team and the bot before submitting text. The operator of the service can read its SQLite file and backups. Store both under the service account's private data directory. The approved Markdown is archived in the private management repository. Approved task descriptions, acceptance criteria and deliverables appear in the configured product repository. Review them for the intended audience.

1. Use **Apps → Import transcript** on a meeting message, or use `/meeting import` with text or a UTF-8 Markdown/text attachment. Confirm consent only when all participants have agreed to external AI processing. The message context menu preserves the source message link and supports safe retries against the same source.
2. Use `/spec draft`. With a configured provider and consent, the service extracts minutes and drafts Markdown. To work manually, attach a Markdown draft and a JSON minutes object.
3. Use `/spec show` to inspect the Markdown and minutes. Submit the human-reviewed Markdown with `/spec revise`, the displayed digest and the reason for the change. Supply corrected minutes if necessary. Include purpose, scope, functional and nonfunctional requirements, interfaces, owner, acceptance and open questions. Carry stable identifiers such as `REQ-001` across revisions. Record decisions on feature, technology, interface or scope changes. The digest returned by revise identifies the meeting, parent revision, text, minutes, author and reason. Historical revisions are accessible through their parent digests.
4. Run `/spec validate` on that exact digest. The check returns findings and blocking questions. Resolve blockers through a new human revision and run another check. The reviewer has no write access to the specification. For a provider outage, an authorized approver can import a structured review from an actual AI-assisted check.
5. Use `/spec approve` as a configured approver. With an enabled provider, tasks are generated automatically. For manual publication, attach the task definitions as a JSON array. Approval applies to the current validated digest. The complete task graph is validated first. During publication, retry the same command and digest after a network failure. The service reuses the saved plan.
6. A later scope change starts with a new revision of the **same meeting**. Keep the previous stable task keys in the new plan for work that continues. Include the entire revised plan. New keys create issues, and omitted keys become cancellation candidates. Unstarted tasks can be updated. Owners decide ongoing or review-stage changes, and approvers explicitly confirm a changed contractual deadline on unstarted work. Completed work receives a separate follow-up issue. A role or repository transfer requires a new task key. Inspect `/scope pending` and use `/scope resolve` to record decisions.

## Manual attachments

A minutes file separates accepted decisions from suggestions and unresolved questions. Example with fictional data:

```json
{
  "proposals": ["Add an export endpoint."],
  "decisions": ["Export the agreed fields as CSV."],
  "open_questions": []
}
```

To obtain an external review, give the reviewer the revised Markdown, its digest and the approved minutes. Request a finding for each of the six checks below and a list of blocking questions. The service checks the digest, fields, consent and submitting approver. The approver attests that the report came from the recorded reviewer. Use an authorized offline AI model if participants have withheld external consent. Only an actual offline check may use `"processing": "local"`. If no permitted AI reviewer is available, continue drafting and revising and leave publication pending. Fill the placeholders with the actual findings.

```json
{
  "revision": "<full digest from /spec revise>",
  "provider": "<reviewing provider and model>",
  "processing": "local",
  "checks": {
    "technical": "<findings>",
    "logical": "<findings>",
    "cross_team": "<findings>",
    "completeness": "<findings>",
    "feasibility": "<findings>",
    "traceability": "<reference the decisions and requirement IDs>"
  },
  "blocking_questions": []
}
```

A minimal manual task plan for a specification containing `REQ-001`:

```json
[
  {
    "key": "export-csv",
    "title": "Export the agreed report fields",
    "description": "Implement a CSV export of the existing report.",
    "role": "backend",
    "estimated_hours": 2,
    "priority": 1,
    "dependencies": [],
    "requirement_ids": ["REQ-001"],
    "deliverables": ["CSV download endpoint"],
    "acceptance": ["A fixture with two records produces the agreed header and two data rows."]
  }
]
```

Optional task fields include a configured product repository in `repo` and an ISO timestamp with UTC offset in `deadline`. Role determines the engineering owner. Designers claim their own tasks. Keep each task within 1–4 hours and cover all the specification's requirement IDs. Include a single primary role for cross-team work.
