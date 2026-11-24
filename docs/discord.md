# Discord operator guide

Install the application with the `bot` and `applications.commands` scopes in the configured guild. Give it View Channel, Send Messages, Attach Files and Read Message History in its configured channels. The service registers guild commands on startup. Team members use their mapped Discord accounts. The bot reads explicit submissions and selected messages with standard Gateway intents.

Restrict the meetings channel to the authorized participants and the bot. Deny View Channel to `@everyone` and audit other roles with access. Keep the host data directory and backups private. Choose a team-visible notifications channel appropriate for task titles, status changes and CI links. Slash replies and interactive task cards are ephemeral.

## From a meeting to product issues

1. In the meetings channel, select **Apps → Import transcript** on a posted transcript. The message link identifies this import on retry. For a new submission use `/meeting import` with pasted text or one UTF-8 `.txt` or `.md` attachment. Only enable AI consent when all participants agreed to external processing. The bot returns a meeting ID.
2. Use `/spec draft` with the meeting ID. In manual mode, attach the draft Markdown and a minutes JSON object. Include stable IDs such as `REQ-001` in the spec and record purpose, scope, requirements, interfaces, owners, acceptance criteria and open questions.
3. Use `/spec show` to get the current digest, Markdown, decisions, diff and reason. Select the markdown or minutes view to download an editable file. Review the content and upload edited Markdown with `/spec revise`, the displayed digest and a reason. Attach corrected minutes JSON when proposals, decisions or open questions need edits. Review material changes with the team. An optional digest in `/spec show` selects a historical revision.
4. Use `/spec validate` on the new digest. With a supported configured reviewer this runs the AI check. Alternatively an approver can upload a structured report produced by an AI reviewer. Set `processing` to `local` only for a review performed locally. Address blocking questions with another revision and repeat the check.
5. An approver runs `/spec approve` with the validated digest. Automatic mode generates a task plan. Manual mode accepts a JSON array of task proposals. Once approved, the service archives the exact Markdown in the private management repository and publishes the validated tasks to product repositories and the configured Project. If publication stops midway, correct the API/configuration problem and repeat approval of **the same digest**. The saved approved plan supplies the stable issue keys on retry.

The upload limit is 128 KiB, with an additional 120,000-character limit for workflow text fields. Minutes have this structure:

```json
{
  "proposals": ["Export the results"],
  "decisions": ["Show a preview first"],
  "open_questions": []
}
```

An imported review uses the following structure. Replace every placeholder with the reviewer's findings on the exact revised document. Fill `blocking_questions` whenever a decision is still needed.

```json
{
  "revision": "<digest from /spec revise>",
  "provider": "<AI reviewer and model>",
  "processing": "local",
  "checks": {
    "technical": "<finding>",
    "logical": "<finding>",
    "cross_team": "<finding>",
    "completeness": "<finding>",
    "feasibility": "<finding>",
    "traceability": "<finding>"
  },
  "blocking_questions": []
}
```

A manual task plan covers every requirement ID in the revised spec. For example:

```json
[
  {
    "key": "PREVIEW-01",
    "title": "Create the preview screen",
    "description": "Build the preview screen for the agreed sample.",
    "role": "frontend",
    "estimated_hours": 2,
    "dependencies": [],
    "requirement_ids": ["REQ-001"],
    "deliverables": ["Preview screen"],
    "acceptance": ["The sample input renders all three expected rows."]
  }
]
```

## Working on an issue

In the tasks channel, use `/task list` for the backlog or set `mine` to true. Open `/task show` with the stable key for details and actions. Designers claim a design task before starting it. Engineering tasks have a fixed role owner. A design deliverable can be a shared HTTPS document URL.

`/task action` accepts the same operations as the task card, plus link and cancel:

| Action | Who acts | Input |
| --- | --- | --- |
| claim | Designer | Unclaimed design key |
| start | Owner | Dependencies must be Done |
| link | Owner | Deliverable or pull request HTTPS URL |
| review | Owner | HTTPS evidence, or previously linked evidence |
| done / reject | Approver | Acceptance evidence or requested revision |
| block / unblock | Owner | A reason for blocking |
| estimate | Owner | Remaining hours or `unknown` |
| cancel | Approver | Reason for cancellation |

A mapped GitHub user can enter `!relay ACTION [VALUE]` in a new comment on the product issue. The poller processes these with the same role and transition checks. Discord-only designers can use the full task flow in Discord.

## Schedule, progress and scope

- `/schedule show` recalculates and displays the forecast. Select `critical` for dependency-only CPM or `gantt` to get a PNG with hour labels.
- `/schedule settings` returns the effective calendar. An approver can use `/schedule update` to upload a JSON object containing changed top-level fields, e.g. `{"end":"2030-01-21T18:00:00+09:00"}`. A supplied availability or milestones map replaces that whole map. The service validates the combined calendar and saves the overrides next to the state database as `*.calendar.json`. Back up this file with the workflow state. Scheduler runs preserve task deadlines, actuals and established baselines.
- `/progress` reports the team's workload, progress, critical work and risks. An approver can enable `ai_consent` with participant agreement to send aggregate status counts to the configured summarizer. The regular report works offline from AI.
- For scope changes, revise, validate and approve the updated spec. In the meetings channel, use `/scope pending` to inspect required decisions and `/scope resolve` to accept or decline one. Active work is reviewed by its owner. Completed work can generate follow-up issues. Approvers decide cancellation candidates.

The service recalculates after Discord task and approval changes. Every five minutes it polls GitHub, recalculates overdue work and delivers grouped notices. Use `/schedule show` to refresh on demand. Delivery retries use the persisted notification queue. A crash between sending a Discord message and recording its acknowledgement can cause repeat delivery.
