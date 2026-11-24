"""Discord application commands and the single-instance polling loop."""

import asyncio
from collections import Counter
from dataclasses import replace
from functools import wraps
from hashlib import sha256
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from typing import Literal

from .discord_io import authorize, notice_batches, read_text
from .schedule import Calendar, gantt, progress, recalculate


log = logging.getLogger(__name__)
CALENDAR_KEYS = {"start", "end", "feature_freeze", "milestones", "availability", "timezone", "buffer_hours"}


class Service:
    def __init__(self, config, github, workflow, provider, db_path):
        self.config = config
        self.github = github
        self.workflow = workflow
        self.provider = provider
        self.db_path = Path(db_path)
        self.calendar_path = self.db_path.with_suffix(".calendar.json")
        self.base_calendar = dict(config.calendar)
        self.config.calendar = self.calendar()
        self._validate_calendar(self.config.calendar)

    def calendar(self):
        overrides = json.loads(self.calendar_path.read_text()) if self.calendar_path.exists() else {}
        return self.base_calendar | overrides

    def _validate_calendar(self, settings):
        parsed = Calendar.from_dict(settings)
        if parsed.availability.keys() - self.config.members.keys():
            raise ValueError("Availability references an unknown team member.")

    def update_calendar(self, patch):
        if not isinstance(patch, dict) or not patch or patch.keys() - CALENDAR_KEYS:
            raise ValueError("Upload a JSON object with recognized calendar fields.")
        current = self.calendar()
        self._validate_calendar(current | patch)
        overrides = json.loads(self.calendar_path.read_text()) if self.calendar_path.exists() else {}
        with NamedTemporaryFile(mode="w", dir=self.calendar_path.parent, delete=False) as stream:
            try:
                json.dump(overrides | patch, stream)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(stream.name, self.calendar_path)
            finally:
                Path(stream.name).unlink(missing_ok=True)
        self.config.calendar = current | patch
        try:
            return self.recalculate()
        except Exception as exc:
            raise ValueError("Calendar saved. Forecast synchronization is pending; use /schedule show to retry.") from exc

    def recalculate(self):
        existing = self.github.tasks()
        originals = {task.key: task for task in existing}
        plan = recalculate(existing, replace(self.config, calendar=self.calendar()))
        changed = [(originals[t.key], t) for t in plan.tasks if t != originals[t.key]]
        if changed:
            self.github.publish_changes([], changed)
        self._alerts(plan)
        return plan

    def _alerts(self, plan):
        by_key = {t.key: t for t in plan.tasks}
        current = {f"risk:{risk}": risk for risk in plan.risks}
        for t in plan.tasks:
            if t.dependencies and t.status == "Todo" and all(by_key[k].status == "Done" for k in t.dependencies):
                current[f"ready:{t.key}"] = f"{t.key}: dependencies are complete."
        # Store the generation so an alert can fire again after it has cleared.
        # Enqueue before updating the snapshot. On retry the same event ID deduplicates.
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS bot_alerts (id INTEGER PRIMARY KEY, generation INTEGER, active TEXT)")
            row = db.execute("SELECT generation,active FROM bot_alerts WHERE id=1").fetchone()
        generation, previous = (row[0], set(json.loads(row[1]))) if row else (0, set())
        if current.keys() == previous:
            return
        for key in sorted(current.keys() - previous):
            digest = sha256(key.encode()).hexdigest()
            self.github.notify(f"schedule:{generation + 1}:{digest}", current[key])
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT OR REPLACE INTO bot_alerts VALUES(1,?,?)",
                       (generation + 1, json.dumps(sorted(current))))

    def poll(self):
        self.github.poll()
        self.recalculate()


def _list_tasks(tasks):
    return "\n".join(f"{t.key} · {t.status} · {t.title}" for t in tasks) or "No matching tasks."


async def _reply(interaction, text, *, filename="result.txt", view=None):
    # All commands respond privately. The notifications channel receives only team events.
    kwargs = {"ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    if len(text) <= 1850:
        await interaction.followup.send(text or "OK", **kwargs)
    else:
        import discord
        await interaction.followup.send(file=discord.File(BytesIO(text.encode()), filename=filename), **kwargs)


def _failure(exc):
    if isinstance(exc, (PermissionError, ValueError, KeyError)):
        return str(exc)[:1550] or "Invalid request."
    log.warning("Discord operation failed (%s)", type(exc).__name__)
    return "Operation incomplete. Check service configuration and retry the same operation."


def create_client(service, *, worker=asyncio.to_thread):
    import discord
    from discord import app_commands

    class RelayClient(discord.Client):
        async def setup_hook(self):
            await tree.sync(guild=guild)
            self.worker = asyncio.create_task(periodic())

        async def close(self):
            if getattr(self, "worker", None):
                self.worker.cancel()
            await super().close()

    client = RelayClient(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
    guild = discord.Object(id=int(service.config.guild_id))
    tree = app_commands.CommandTree(client)
    lock = asyncio.Lock()
    workflow, github = service.workflow, service.github
    config = service.config

    def guarded(channel, *, approver=False):
        def decorate(fn):
            @wraps(fn)
            async def perform(interaction, *args, **kwargs):
                await interaction.response.defer(ephemeral=True, thinking=True)
                try:
                    authorize(config, interaction, channel, approver=approver)
                    async with lock:
                        await fn(interaction, *args, **kwargs)
                except Exception as exc:
                    await _reply(interaction, _failure(exc))
            return perform
        return decorate

    async def after_change(interaction, message):
        try:
            await worker(service.recalculate)
        except Exception as exc:
            log.warning("Recalculation deferred (%s)", type(exc).__name__)
            message += "\nForecast update pending. The next poll will retry."
        await _reply(interaction, message)

    async def attachment_json(attachment):
        return json.loads(await read_text(attachment, extensions=(".json",))) if attachment else None

    async def send_notices():
        channel = client.get_channel(int(config.channels["notifications"]))
        if not channel or str(channel.guild.id) != config.guild_id:
            raise RuntimeError("notifications channel unavailable")
        notices = await worker(github.notifications)
        for ids, message in notice_batches(notices):
            await channel.send(message)
            for event_id in ids:
                await worker(github.ack, event_id)

    async def periodic():
        await client.wait_until_ready()
        while not client.is_closed():
            async with lock:
                try:
                    await worker(service.poll)
                except Exception as exc:
                    log.warning("Reconciliation incomplete (%s); retrying next poll", type(exc).__name__)
                try:
                    await send_notices()
                except Exception as exc:
                    log.warning("Notification delivery deferred (%s)", type(exc).__name__)
            await asyncio.sleep(300)

    def group(name, description):
        result = app_commands.Group(name=name, description=description)
        tree.add_command(result, guild=guild)
        return result

    meeting = group("meeting", "Import a transcript in the private meetings channel")
    spec = group("spec", "Draft, revise, check and approve specifications")
    task = group("task", "Manage canonical GitHub issues")
    schedule = group("schedule", "View or recalculate the team schedule")
    scope = group("scope", "Review changes to previously approved work")

    @meeting.command(name="import", description="Import a transcript; enable consent only with every participant's agreement")
    @guarded("meetings")
    async def import_meeting(interaction: discord.Interaction, text: str = "",
                             attachment: discord.Attachment = None, consent: bool = False):
        if bool(text) == bool(attachment):
            raise ValueError("Provide exactly one transcript as text or a .md/.txt attachment.")
        content = await read_text(attachment) if attachment else text
        source = f"discord:{interaction.guild_id}/{interaction.channel_id}/{interaction.id}"
        mid = await worker(workflow.import_meeting, source, content, str(interaction.user.id), consent)
        await _reply(interaction, f"Imported meeting {mid}.")

    @tree.context_menu(name="Import transcript", guild=guild)
    async def import_message(interaction: discord.Interaction, message: discord.Message):
        try:
            authorize(config, interaction, "meetings")
            if message.channel.id != interaction.channel_id:
                raise PermissionError("Select a message in the configured meetings channel.")
        except PermissionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        class Consent(discord.ui.Modal, title="Import this meeting transcript"):
            confirmation = discord.ui.TextInput(
                label="Consent to external AI (yes/no)", default="no",
                placeholder="yes only with consent from all participants", max_length=3,
            )

            async def on_submit(self, interaction):
                await guarded("meetings")(save)(interaction, self.confirmation.value)

        async def save(interaction, answer):
            if answer.lower() not in {"yes", "no"}:
                raise ValueError("Enter yes or no for external AI consent.")
            if len(message.attachments) > 1:
                raise ValueError("Select a message with at most one transcript attachment.")
            body = await read_text(message.attachments[0]) if message.attachments else message.content
            mid = await worker(workflow.import_meeting, message.jump_url, body,
                                         str(interaction.user.id), answer.lower() == "yes")
            await _reply(interaction, f"Imported meeting {mid}.")

        await interaction.response.send_modal(Consent())

    @spec.command(description="Draft with your configured provider, or upload Markdown and minutes JSON")
    @guarded("meetings")
    async def draft(interaction: discord.Interaction, meeting: str, markdown: discord.Attachment = None,
                    minutes: discord.Attachment = None):
        body = await read_text(markdown) if markdown else None
        records = await attachment_json(minutes)
        digest = await worker(workflow.draft, meeting, str(interaction.user.id),
                                         markdown=body, minutes=records)
        await _reply(interaction, f"Draft revision {digest}. Use /spec show to review.")

    @spec.command(description="Read a revision, recorded decisions, diff and consistency report")
    @guarded("meetings")
    async def show(interaction: discord.Interaction, meeting: str, digest: str = None,
                   view: Literal["review", "markdown", "minutes"] = "review"):
        result = await worker(workflow.show, meeting, str(interaction.user.id), digest=digest)
        if view == "review":
            await _reply(interaction, json.dumps(result, indent=2, ensure_ascii=False), filename="revision.json")
            return
        body = result["markdown"] if view == "markdown" else json.dumps(result["minutes"], indent=2, ensure_ascii=False)
        filename = "spec.md" if view == "markdown" else "minutes.json"
        await interaction.followup.send(f"Revision {result['digest']}",
                                       file=discord.File(BytesIO(body.encode()), filename=filename), ephemeral=True)

    @spec.command(description="Submit your reviewed Markdown against the displayed revision")
    @guarded("meetings")
    async def revise(interaction: discord.Interaction, meeting: str, digest: str, reason: str,
                     markdown: discord.Attachment, minutes: discord.Attachment = None):
        body = await read_text(markdown)
        result = await worker(workflow.revise, meeting, str(interaction.user.id),
                                         body, reason, based_on=digest, minutes=await attachment_json(minutes))
        await _reply(interaction, f"Revised to {result}. Validate this revision before approval.")

    @spec.command(name="validate", description="Run a consistency check or import an AI review JSON for this revision")
    @guarded("meetings")
    async def validate_spec(interaction: discord.Interaction, meeting: str, digest: str,
                            report: discord.Attachment = None):
        checked = await worker(workflow.validate, meeting, str(interaction.user.id),
                                          digest, report=await attachment_json(report))
        await _reply(interaction, json.dumps(checked, indent=2, ensure_ascii=False), filename="review.json")

    @spec.command(description="Approve a validated revision and publish its generated or uploaded tasks")
    @guarded("meetings", approver=True)
    async def approve(interaction: discord.Interaction, meeting: str, digest: str,
                      tasks: discord.Attachment = None):
        result = await worker(workflow.approve, meeting, str(interaction.user.id),
                                         digest, tasks=await attachment_json(tasks))
        await after_change(interaction, f"Approved {digest}.\n{_list_tasks(result)}")

    @scope.command(description="List changes that require a human decision")
    @guarded("meetings")
    async def pending(interaction: discord.Interaction, meeting: str):
        result = await worker(workflow.changes.pending, meeting)
        await _reply(interaction, json.dumps(result, indent=2, ensure_ascii=False), filename="changes.json")

    @scope.command(description="Accept or decline one pending scope change as its authorized decision maker")
    @guarded("meetings")
    async def resolve(interaction: discord.Interaction, change_id: str, accept: bool):
        await worker(workflow.changes.resolve, change_id, str(interaction.user.id), approve=accept)
        await after_change(interaction, "Change decision recorded.")

    @task.command(name="list", description="List team tasks or just the ones you own")
    @guarded("tasks")
    async def list_tasks(interaction: discord.Interaction, mine: bool = False):
        tasks = await worker(github.tasks)
        if mine:
            tasks = [t for t in tasks if t.assignee == str(interaction.user.id)]
        await _reply(interaction, _list_tasks(tasks), filename="tasks.txt")

    async def change_task(interaction, key, action, value):
        result = await worker(github.action, key, action, str(interaction.user.id), value)
        await after_change(interaction, _list_tasks([result]))

    @task.command(description="Perform an authorized task action; review/link takes a URL, done takes acceptance notes")
    @guarded("tasks")
    async def action(interaction: discord.Interaction, key: str,
                     action: Literal["claim", "start", "review", "reject", "done", "block", "unblock",
                                     "estimate", "link", "cancel"], value: str = None):
        await change_task(interaction, key, action, value)

    class ValueModal(discord.ui.Modal):
        def __init__(self, key, action, label):
            super().__init__(title=f"Task {action}")
            self.key, self.action = key, action
            self.value = discord.ui.TextInput(label=label, max_length=500)
            self.add_item(self.value)

        async def on_submit(self, interaction):
            await guarded("tasks")(change_task)(interaction, self.key, self.action, self.value.value)

    def controls(key, status):
        view = discord.ui.View(timeout=300)
        options = {
            "Todo": ["claim", "start", "block"], "In Progress": ["review", "block", "estimate"],
            "Blocked": ["unblock"], "In Review": ["done", "reject"],
        }
        prompts = {"review": "Deliverable URL", "done": "Acceptance evidence", "reject": "Changes requested",
                   "block": "Blocker", "estimate": "Hours remaining (or unknown)"}
        for name in options.get(status, []):
            button = discord.ui.Button(label=name.title())

            async def click(interaction, action=name):
                if action in prompts:
                    try:
                        authorize(config, interaction, "tasks")
                    except PermissionError as exc:
                        await interaction.response.send_message(str(exc), ephemeral=True)
                        return
                    await interaction.response.send_modal(ValueModal(key, action, prompts[action]))
                else:
                    await guarded("tasks")(change_task)(interaction, key, action, None)
            button.callback = click
            view.add_item(button)
        return view if view.children else None

    @task.command(name="show", description="View a task with action buttons")
    @guarded("tasks")
    async def show_task(interaction: discord.Interaction, key: str):
        tasks = await worker(github.tasks)
        item = next((t for t in tasks if t.key == key), None)
        if not item:
            raise ValueError("Unknown task key.")
        owner = f"<@{item.assignee}>" if item.assignee else f"Unclaimed ({item.role})"
        remaining = f"{item.remaining_hours:g} h" if item.remaining_hours is not None else "unknown"
        lines = [f"**{item.key} · {item.title}**", f"{item.status} · {owner} · Remaining {remaining}",
                 item.description, "", "**Deliverables**", *item.deliverables, "", "**Acceptance**", *item.acceptance]
        if item.dependencies:
            lines.append("Depends on " + ", ".join(item.dependencies))
        if item.block_reason:
            lines.append("Blocked: " + item.block_reason)
        if item.deadline:
            lines.append("Deadline: " + item.deadline)
        if item.repo and item.issue:
            lines.append(f"https://github.com/{item.repo}/issues/{item.issue}")
        lines.extend(item.links)
        await _reply(interaction, "\n".join(lines), filename="task.txt", view=controls(key, item.status))

    @schedule.command(name="show", description="Recalculate and show the team forecast, Gantt or critical paths")
    @guarded("tasks")
    async def show_schedule(interaction: discord.Interaction,
                            view: Literal["forecast", "gantt", "critical"] = "forecast"):
        plan = await worker(service.recalculate)
        if view == "gantt":
            data = await worker(gantt, plan)
            await interaction.followup.send(file=discord.File(BytesIO(data), filename="gantt.png"), ephemeral=True)
        elif view == "critical":
            paths = "\n".join(" → ".join(path) for path in plan.critical_paths)
            await _reply(interaction, f"Dependency-only duration: {plan.duration_hours} h\n{paths or 'No known critical path.'}")
        else:
            await _reply(interaction, progress(plan), filename="forecast.txt")

    @schedule.command(description="View the effective calendar for edits")
    @guarded("tasks")
    async def settings(interaction: discord.Interaction):
        data = await worker(service.calendar)
        await _reply(interaction, json.dumps(data, default=str, indent=2), filename="calendar.json")

    @schedule.command(description="Apply an explicit calendar JSON update and recalculate")
    @guarded("tasks", approver=True)
    async def update(interaction: discord.Interaction, calendar: discord.Attachment):
        data = await attachment_json(calendar)
        plan = await worker(service.update_calendar, data)
        await _reply(interaction, progress(plan), filename="forecast.txt")

    @tree.command(name="progress", description="Team progress; optionally summarize aggregate counts with AI", guild=guild)
    @guarded("tasks")
    async def progress_report(interaction: discord.Interaction, ai_consent: bool = False):
        plan = await worker(service.recalculate)
        report = progress(plan)
        if ai_consent:
            if str(interaction.user.id) not in config.approvers:
                raise PermissionError("An approver must confirm participant consent for AI summaries.")
            counts = dict(Counter(t.status for t in plan.tasks))
            try:
                summary = await worker(service.provider.summary, json.dumps({"status_counts": counts}))
                report += "\nAI commentary on status counts:\n" + summary
            except Exception:
                report += "\nAI summary unavailable."
        await _reply(interaction, report, filename="progress.txt")

    client.command_tree = tree
    return client


def run(config, token, github, workflow, provider, db_path):
    create_client(Service(config, github, workflow, provider, db_path)).run(token)
