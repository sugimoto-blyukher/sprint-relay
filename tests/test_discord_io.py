import asyncio
import unittest
from types import SimpleNamespace as NS

from relay.discord_io import authorize, notice_batches, read_text


class DiscordIOTests(unittest.TestCase):
    def test_private_command_checks(self):
        config = NS(members={"7": "design"}, approvers={"8"}, guild_id="1", channels={"meetings": "2"})
        interaction = NS(user=NS(id=7), guild_id=1, channel_id=2, guild=NS(default_role=1))
        interaction.channel = NS(permissions_for=lambda role: NS(view_channel=False))
        self.assertEqual(authorize(config, interaction, "meetings"), "7")
        with self.assertRaises(PermissionError):
            authorize(config, interaction, "meetings", approver=True)
        interaction.channel.permissions_for = lambda role: NS(view_channel=True)
        with self.assertRaises(PermissionError):
            authorize(config, interaction, "meetings")
        interaction.guild_id = 9
        with self.assertRaises(PermissionError):
            authorize(config, interaction, "meetings")

    def test_oversize_attachment_fails_before_download(self):
        attachment = NS(filename="sample.txt", size=2 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "1048576-byte"):
            asyncio.run(read_text(attachment))

    def test_batches_preserve_ids_under_limit(self):
        batches = list(notice_batches([(12, "r" * 2500), (13, "z" * 55), (14, "hello")]))
        self.assertEqual([ids for ids, _ in batches], [[12], [13, 14]])
        self.assertTrue(all(len(text) <= 1900 for _, text in batches))
