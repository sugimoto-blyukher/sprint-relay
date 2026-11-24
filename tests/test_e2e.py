"""Acceptance walkthrough using Discord command callbacks and a simulated GitHub API."""
import asyncio
import socket
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from relay.jobs import JobStore
from relay.gateway import Gateway
from relay.security import Principal
import pytest

from relay.bot import Service, create_client
from relay.config import Config
from relay.discord_io import notice_batches
from relay.github import GitHub
from relay.workflow import CHECKS, Workflow
from test_github import FakeAPI


class EventClock(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime.fromisoformat("2030-01-20T09:00:00+09:00")
        return instant.astimezone(tz)


def attachment(name, text):
    raw = text.encode()
    return SimpleNamespace(filename=name, size=len(raw), read=AsyncMock(return_value=raw))


def test_transcript_to_cross_channel_delivery(tmp_path, monkeypatch, inline_worker):
    monkeypatch.setattr("relay.core.datetime", EventClock)
    monkeypatch.setattr("relay.schedule.datetime", EventClock)
    config = Config.load(Path(__file__).resolve().parents[1] / "config.example.toml")
    config.management_repo = "owner/manage"
    config.project_id = "project-fixture"
    # Synthetic, previously verified MCP account. No provider contacts occur.
    monkeypatch.setenv("OPENAI_API_KEY", "unused-fixture")
    monkeypatch.setenv("MODEL_API_KEY", "unused-fixture")
    def no_network(*args, **kwargs):
        pytest.fail("This acceptance flow must run entirely without external network access")
    monkeypatch.setattr(socket, "create_connection", no_network)
    config.agents["muse"] = config.agents["muse"].model_copy(update={
        "enabled": True, "verified": True, "terms_confirmed": True, "schedule_confirmed": True,
    })
    config.privacy = config.privacy.model_copy(update={"project_providers": ("muse",)})
    api = FakeAPI()
    db = tmp_path / "workflow.sqlite"
    github = GitHub(config, "", db, request=api)
    document = "## Requirement\nREQ-001 Publish an accessible prototype."
    transcript = "We agreed to publish a prototype. Someone proposed a ranking table."
    minutes = {
        "summary": "Prototype decision",
        "decisions": [{"text": "Publish a prototype", "quote": "We agreed to publish a prototype.",
                       "rationale": "Test the experience", "impact": "Design and frontend"}],
        "proposals": ["A ranking table"], "open_questions": [],
    }
    deadline = "2030-01-20T12:00:00+09:00"
    build = dict(key="DESIGN", title="Prototype", description="Design the prototype",
                 estimated_hours=1, role="design", dependencies=[], requirement_ids=["REQ-001"],
                 deliverables=["Prototype"], acceptance=["Demo opens from a shared link"])
    build_test = dict(build, key="VERIFY", title="Verify prototype", role="frontend",
                      description="Verify keyboard focus", dependencies=["DESIGN"],
                      deliverables=["Test report"], acceptance=["Focus reaches each control"])
    clock = [1000.0]
    jobs = JobStore(db, config, clock=lambda: clock[0])
    flow = Workflow(db, config, github, jobs=jobs, project=config.management_repo)
    service = Service(config, github, flow, Mock(), db)
    gateway = Gateway(config.management_repo, config, jobs, flow, None, None, github)
    principal = Principal("synthetic-muse", "muse", "management", config.management_repo)

    def tool(name, **inputs):
        return gateway.dispatch(name, principal, {"project_id": config.management_repo, **inputs})

    def claim(kind):
        listed = tool("list_workflow_jobs", kinds=[kind])["jobs"]
        assert len(listed) == 1
        receipt = tool("claim_job", job_id=listed[0]["job_id"])
        return {"job_id": receipt["job_id"], "lease_token": receipt["lease_token"]}

    client = create_client(service, worker=inline_worker)
    group = client.command_tree
    guild = discord.Object(id=int(config.guild_id))

    def command(area, name, channel, actor, **kwargs):
        interaction = Mock(id=77, guild_id=int(config.guild_id), channel_id=int(config.channels[channel]),
                           response=AsyncMock(), followup=AsyncMock())
        interaction.user.id = int(actor)
        interaction.channel.permissions_for.return_value.view_channel = False
        callback = group.get_command(area, guild=guild).get_command(name).callback
        asyncio.run(callback(interaction, **kwargs))
        response = interaction.followup.send.await_args
        return response.args[0] if response.args else response.kwargs["file"]

    designer, approver = "10102", "10101"
    # A01/A02. A posted message and ordinary poll cannot enqueue a meeting.
    service.poll()
    assert not tool("list_workflow_jobs")["jobs"]
    result = command("meeting", "import", "meetings", designer, text=transcript,
                     consent=True, providers="muse")
    meeting = result.split()[1]
    extract = claim("minutes")
    assert tool("get_transcript", **extract)["transcript"] == transcript
    tool("submit_minutes", **extract, minutes=minutes)
    drafting = claim("specification")
    assert tool("get_specification", **drafting)["minutes"]["proposals"] == ["A ranking table"]
    tool("submit_specification", **drafting, markdown=document, evidence=["We agreed to publish a prototype."])
    draft = flow.show(meeting, designer)["digest"]
    with pytest.raises(ValueError):
        flow.approve(meeting, approver, draft)
    command("spec", "revise", "meetings", designer, meeting=meeting, digest=draft,
            markdown=attachment("spec.md", document + "\nSupport keyboard navigation."),
            reason="Record the team's accessibility decision")
    revision = flow.show(meeting, designer)["digest"]
    with pytest.raises(ValueError):
        flow.approve(meeting, approver, revision)
    validation = claim("validation")
    tool("submit_validation", **validation, digest=revision, blocking_questions=[],
         checks={category: "Checked against fixture decision" for category in CHECKS})
    with pytest.raises(ValueError):
        flow.approve(meeting, approver, draft)
    assert not api.issues

    args = dict(meeting=meeting, digest=revision)
    for _ in range(2):
        result = command("spec", "approve", "meetings", approver, **args)
        assert "Approved" in result
    planning = claim("task_plan")
    tool("submit_task_plan", **planning, digest=revision, tasks=[build, build_test])
    # The durable publication worker may run more than once.
    flow.recover()
    flow.recover()
    assert flow.show(meeting, designer)["state"] == "PUBLISHED"
    assert len(api.issues) == 2
    assert any("/specs/" in path for path in api.files)
    assert any(path == "/graphql" and "addProjectV2ItemById" in body["query"]
               for _, path, body in api.calls)

    def design_action(action, value=None):
        return command("task", "action", "tasks", designer, key="DESIGN", action=action, value=value)

    # The human project owner records the hard deadline separately from AI proposals.
    original = next(t for t in github.tasks() if t.key == "VERIFY")
    github.update(replace(original, deadline=deadline), expected=original)
    # A15. A second imported meeting waits when the only eligible provider loses quota.
    pending_meeting = flow.import_meeting("fixture:second", transcript, designer, True, providers=["muse"])
    delayed_job = claim("minutes")
    tool("report_job_failure", **delayed_job, reason="quota")
    assert all(p is None for p in jobs.status(config.management_repo)["active_providers"].values())
    assert flow.show(pending_meeting, designer)["state"] == "IMPORTED"
    chart = command("schedule", "show", "tasks", designer, view="gantt")
    assert chart.filename == "gantt.png" and chart.fp.read(8) == b"\x89PNG\r\n\x1a\n"
    chart.close()
    # A05/A06. Discord, Issues and the scheduler remain available throughout the outage.
    assert "Todo" in design_action("claim")
    assert "In Progress" in design_action("start")
    before = {t.key: t for t in service.recalculate().tasks}
    assert before["DESIGN"].assignee == designer
    assert api.issues[before["DESIGN"].issue]["assignees"] == []
    assert before["VERIFY"].baseline_end is not None
    assert "In Progress" in design_action("estimate", "3")
    # Owner starts at noon. The longer design task now pushes verification into the afternoon.
    delayed = service.update_calendar({"end": "2030-01-20T15:00:00+09:00",
                                       "feature_freeze": "2030-01-20T14:45:00+09:00"})
    verify = next(t for t in delayed.tasks if t.key == "VERIFY")
    assert verify.forecast_end > before["VERIFY"].forecast_end
    assert verify.baseline_end == before["VERIFY"].baseline_end
    assert verify.deadline == deadline
    assert any("Project end at risk" in risk for risk in delayed.risks)
    assert "In Review" in design_action("review", "https://example.org/fixture-design")
    # A mapped GitHub reviewer and a Discord-only designer use the same task.
    config.github_users[approver] = "fixture-reviewer"
    number = before["DESIGN"].issue
    api.comments[number] = [{"id": 100, "body": "!relay done Keyboard navigation verified",
                             "user": {"login": "fixture-reviewer"}}]
    service.poll()
    after = {t.key: t for t in github.tasks()}
    assert api.issues[number]["state"] == "closed"
    assert after["DESIGN"].status == "Done" and after["DESIGN"].actual_end
    assert after["DESIGN"].actual_start == before["DESIGN"].actual_start
    assert after["VERIFY"].baseline_end == before["VERIFY"].baseline_end
    assert after["VERIFY"].deadline == deadline
    notifications = github.notifications()
    assert any("dependencies are complete" in msg for _, msg in notifications)
    assert any("In Review" in msg for _, msg in notifications)
    for event_ids, _ in notice_batches(notifications):
        for event_id in event_ids:
            github.ack(event_id)
    service.poll()
    assert not github.notifications()
    # An authenticated read after the cooldown restores Muse at a safe job boundary.
    clock[0] += 8 * 3600
    tool("get_project_context")
    resumed = claim("minutes")
    assert resumed["job_id"] == delayed_job["job_id"]
    tool("submit_minutes", **resumed, minutes=minutes)
    assert flow.show(pending_meeting, designer)["state"] == "MINUTES_READY"
