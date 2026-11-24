import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from relay.bot import Service, create_client
from relay.config import load_config
from relay.core import Task


CONFIG = Path(__file__).resolve().parents[1] / "config.example.toml"


class BotTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def set_worker(self, inline_worker):
        self.worker = inline_worker

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = load_config(CONFIG)
        self.github = Mock()
        self.workflow = Mock()
        self.tasks = [Task("A", "A", "A", "backend", 2, 2, assignee="10104")]
        self.github.tasks.side_effect = lambda: self.tasks.copy()
        def publish_changes(new, changes):
            for expected, task in changes:
                self.tasks[self.tasks.index(expected)] = task
        self.github.publish_changes.side_effect = publish_changes
        self.service = Service(self.config, self.github, self.workflow, None, Path(self.dir.name) / "cache.db")

    def test_calendar_update_persists_and_stable_schedule_skips_writes(self):
        initial = self.service.recalculate()
        self.assertEqual(self.github.publish_changes.call_count, 1)
        self.service.recalculate()
        self.assertEqual(self.github.publish_changes.call_count, 1)
        changed = self.service.update_calendar({"end": "2030-01-21T22:00:00+09:00"})
        self.assertEqual(changed.tasks[0].baseline_end, initial.tasks[0].baseline_end)
        with self.assertRaises(ValueError):
            self.service.update_calendar({"end": "2020-01-01"})
        with self.assertRaisesRegex(ValueError, "unknown team member"):
            self.service.update_calendar({"availability": {"99999": []}})
        self.assertEqual(self.service.calendar()["end"], "2030-01-21T22:00:00+09:00")
        self.assertEqual(json.loads(self.service.calendar_path.read_text()), {"end": "2030-01-21T22:00:00+09:00"})

    def test_reappearing_risk_can_notify_after_clearing(self):
        plan = NS(tasks=[], risks=["no capacity"])
        self.service._alerts(plan)
        self.service._alerts(plan)
        self.assertEqual(self.github.notify.call_count, 1)
        self.service._alerts(NS(tasks=[], risks=[]))
        self.service._alerts(plan)
        self.assertEqual(self.github.notify.call_count, 2)
        first, second = self.github.notify.call_args_list
        self.assertNotEqual(first[0][0], second[0][0])

    def test_registered_commands_use_actor_and_private_import_gate(self):
        async def run():
            client = create_client(self.service, worker=self.worker)
            guild = discord.Object(id=int(self.config.guild_id))
            tree = client.command_tree
            self.assertEqual({c.name for c in tree.get_commands(guild=guild)},
                             {"meeting", "Import transcript", "spec", "task", "schedule", "progress", "scope"})
            ctx = NS(user=NS(id=10104), guild_id=guild.id, channel_id=11112, id=77,
                     guild=NS(default_role=1), response=NS(defer=AsyncMock()),
                     followup=NS(send=AsyncMock()))
            self.github.action.return_value = self.tasks[0]
            cmd = tree.get_command("task", guild=guild).get_command("action")
            await cmd.callback(ctx, "A", "start")
            self.github.action.assert_called_once_with("A", "start", "10104", None)
            self.assertTrue(ctx.followup.send.call_args.kwargs["ephemeral"])
            cmd = tree.get_command("meeting", guild=guild).get_command("import")
            await cmd.callback(ctx, text="fictional")
            self.workflow.import_meeting.assert_not_called()
            ctx.channel_id = 11111
            ctx.channel = NS(permissions_for=lambda role: NS(view_channel=False))
            await cmd.callback(ctx, text="fictional")
            self.workflow.import_meeting.assert_called_once_with("discord:10000/11111/77", "fictional", "10104", False)
            await client.close()
        asyncio.run(run())

    def test_attachment_summary_opt_in_is_approver_only(self):
        async def run():
            self.service.provider = Mock()
            self.service.provider.summary.return_value = "aggregate"
            client = create_client(self.service, worker=self.worker)
            guild = discord.Object(id=int(self.config.guild_id))
            command = client.command_tree.get_command("progress", guild=guild)
            ctx = NS(user=NS(id=10104), guild_id=guild.id, channel_id=11112,
                     response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
            await command.callback(ctx, ai_consent=True)
            self.service.provider.summary.assert_not_called()
            ctx.user.id = 10101
            await command.callback(ctx, ai_consent=True)
            self.assertEqual(json.loads(self.service.provider.summary.call_args.args[0]), {"status_counts": {"Todo": 1}})
            await client.close()
        asyncio.run(run())
