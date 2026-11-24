# Discord operator guide

Install the application with the `bot` and `applications.commands` scopes in the configured guild. Give it View Channel, Send Messages, Attach Files and Read Message History in its configured channels. The service registers guild commands on startup. Team members use their mapped Discord accounts. The bot reads explicit submissions and selected messages with standard Gateway intents.

Restrict the meetings channel to the authorized participants and the bot. Deny View Channel to `@everyone` and audit other roles with access. Keep the host data directory and backups private. Choose a team-visible notifications channel appropriate for task titles, status changes and CI links. Slash replies and interactive task cards are ephemeral.

## From a meeting to product issues

1. In the private meetings channel, select **Apps → Import transcript** on a posted message, or use `/meeting import` with pasted text or one UTF-8 `.md` or `.txt` attachment. Ordinary messages start no work. The default transcript limit is 1 MiB. Confirm consent and name each permitted destination, e.g. `muse,codex`, only with every participant's agreement. Leave the destination list blank and consent off for manual processing. The reply identifies the meeting, stage and pending job when available.
   Consent is limited to the project’s configured `privacy.project_providers`. An approver can use `/meeting consent` to update the named destinations with participant agreement or enter `none` to revoke further release. This also applies when resuming a migrated meeting.
2. After a consented import, eligible management agents extract minutes and draft the specification through MCP. The queue survives a provider outage. `/spec draft` can show/queue the draft stage or accept a manual Markdown file and minutes JSON. Use `/spec show` for status, the current digest and recorded evidence. Its markdown and minutes views download editable files once a draft exists. Human review should cover purpose, interfaces, ownership, stable requirement IDs, measurable acceptance, constraints and unresolved decisions.
3. Submit the reviewed Markdown with `/spec revise`, the displayed digest and a reason. Upload the same Markdown with an explicit review reason when it needs no changes. Optionally attach corrected minutes. Keep proposed changes distinct from decisions.
4. Consistency validation runs for the revised digest after human review. Use `/spec validate` to queue it or import a structured, independently produced AI review. Address blocking questions in another revision. `/spec show` exposes the final review, diff and reason. A new digest needs a new check.
5. An approver runs `/spec approve` for this exact digest. The stored decision starts task planning. A validated plan is published to GitHub and the schedule recalculates. An approver can supply a JSON plan in manual operation. On a transient publishing failure, leave the approved revision in place for recovery. Repeating approval for the same digest reuses the saved plan.

Minutes separate proposals, unresolved questions and decisions. For example:

```json
{
  "summary": "The team discussed a preview.",
  "proposals": ["Export the results"],
  "decisions": [{
    "text": "Show a preview first",
    "quote": "We decided to show a preview first.",
    "rationale": "Participants can inspect the sample.",
    "impact": "The preview screen precedes export."
  }],
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

In the tasks channel, use `/task list` for the backlog, or `/task mine` for assigned work. The original list command still supports its mine option. Open `/task show` with the stable key for details and actions. Designers claim a design task before starting it. Engineering tasks have a fixed role owner. A design deliverable can be a shared HTTPS document URL.

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
- `/schedule recalculate` refreshes the forecast; `/schedule critical` returns the dependency-only duration and paths.
- `/progress` reports team workload, status, critical work and risks. This calculation works while every provider is offline.
- In the tasks channel, `/relay status` shows provider health, degraded mode, pending work and patrol timing. `/relay jobs` gives job summaries. Approvers can use `/relay pause` and `/relay resume` during maintenance, and `/relay patrol` for an exceptional patrol verification. Normal failover and recovery are automatic.
- For scope changes, revise, validate and approve the updated spec. In the meetings channel, use `/scope pending` to inspect required decisions and `/scope resolve` to accept or decline one. Active work is reviewed by its owner. Completed work can generate follow-up issues. Approvers decide cancellation candidates.

The service recalculates after Discord task and approval changes. Every 60 seconds by default it polls GitHub, recalculates overdue work and delivers grouped notices. Use `/schedule show` to refresh on demand. Delivery retries use the persisted notification queue. A crash between sending a Discord message and recording its acknowledgement can cause repeat delivery.

- 承認者はタスクチャンネルで `/relay retry provider:codex` を実行できます。 `role:management` または `role:audit` で限定でき、省略時はそのプロバイダーに設定された両方の役割が対象です。対象は合成 Probe です。既存の待機中・実行中 Probe には同じ ID が返り、レート制限中は再試行可能時刻が返ります。発行済みデータ、失敗履歴、実行中のリースを保持します。[再デプロイと両役割の合格条件](deployment.md#worker-更新と-codex-の実環境受け入れ)
