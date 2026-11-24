"""Acceptance walkthrough using Discord command callbacks and a simulated GitHub API."""
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
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
    api = FakeAPI()
    db = tmp_path / "workflow.sqlite"
    github = GitHub(config, "", db, request=api)
    document = "## Requirement\nREQ-001 Publish an accessible prototype."
    minutes = {"proposals": [], "decisions": ["Publish a prototype"], "open_questions": []}
    ai = Mock()
    ai.draft.return_value = {"markdown": document, "minutes": minutes}
    ai.validate.side_effect = lambda markdown, minutes, digest: {
        "revision": digest, "provider": "fixture reviewer", "processing": "local",
        "checks": {category: "Checked against fixture decision" for category in CHECKS},
        "blocking_questions": [],
    }
    deadline = "2030-01-20T12:00:00+09:00"
    build = dict(key="DESIGN", title="Prototype", description="Design the prototype",
                 estimated_hours=1, role="design", requirement_ids=["REQ-001"],
                 deliverables=["Prototype"], acceptance=["Demo opens from a shared link"])
    build_test = dict(build, key="VERIFY", title="Verify prototype", role="frontend",
                      description="Verify keyboard focus", dependencies=["DESIGN"], deadline=deadline,
                      deliverables=["Test report"], acceptance=["Focus reaches each control"])
    ai.tasks.return_value = [build, build_test]
    flow = Workflow(db, config, github, ai)
    service = Service(config, github, flow, ai, db)
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
        return interaction.followup.send.await_args.args[0]

    designer, approver = "10102", "10101"
    result = command("meeting", "import", "meetings", designer, text="We agreed to publish a prototype.",
                     consent=True)
    meeting = result.split()[-1].rstrip(".")
    command("spec", "draft", "meetings", designer, meeting=meeting)
    draft = flow.show(meeting, designer)["digest"]
    command("spec", "revise", "meetings", designer, meeting=meeting, digest=draft,
            markdown=attachment("spec.md", document + "\nSupport keyboard navigation."),
            reason="Record the team's accessibility decision")
    revision = flow.show(meeting, designer)["digest"]
    with pytest.raises(ValueError):
        flow.approve(meeting, approver, revision)
    command("spec", "validate", "meetings", designer, meeting=meeting, digest=revision)
    with pytest.raises(ValueError):
        flow.approve(meeting, approver, draft)
    assert not api.issues

    args = dict(meeting=meeting, digest=revision)
    for _ in range(2):
        result = command("spec", "approve", "meetings", approver, **args)
        assert "Approved" in result
    ai.tasks.assert_called_once()
    assert len(api.issues) == 2
    assert any("/specs/" in path for path in api.files)
    assert any(path == "/graphql" and "addProjectV2ItemById" in body["query"]
               for _, path, body in api.calls)

    def design_action(action, value=None):
        return command("task", "action", "tasks", designer, key="DESIGN", action=action, value=value)

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
