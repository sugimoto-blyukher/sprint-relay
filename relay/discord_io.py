"""Discord transport validation shared by commands and interactive controls."""

from hashlib import sha256


MAX_INPUT_BYTES = 1024 * 1024


def authorize(config, interaction, channel, *, approver=False):
    actor = str(interaction.user.id)
    if (
        getattr(interaction.user, "bot", False) is True
        or str(interaction.guild_id) != config.guild_id
        or str(interaction.channel_id) != config.channels[channel]
        or actor not in set(config.members) | set(config.approvers)
    ):
        raise PermissionError("Use the configured channel and a registered Discord account.")
    if approver and actor not in config.approvers:
        raise PermissionError("A configured approver is required.")
    if channel == "meetings":
        everyone = interaction.guild.default_role
        if interaction.channel.permissions_for(everyone).view_channel:
            raise PermissionError("Meeting imports require a private channel.")
    return actor


async def read_attachment(attachment, *, extensions=(".md", ".txt"), limit=MAX_INPUT_BYTES):
    if not attachment.filename.lower().endswith(extensions):
        raise ValueError(f"Upload a {' or '.join(extensions)} file.")
    if attachment.size > limit:
        raise ValueError(f"The input exceeds the configured {limit}-byte limit.")
    content = await attachment.read()
    if len(content) > limit:
        raise ValueError(f"The input exceeds the configured {limit}-byte limit.")
    try:
        return content.decode("utf-8-sig"), sha256(content).hexdigest()
    except UnicodeDecodeError as exc:
        raise ValueError("The file must contain UTF-8 text.") from exc


async def read_text(attachment, *, extensions=(".md", ".txt"), limit=MAX_INPUT_BYTES):
    text, _ = await read_attachment(attachment, extensions=extensions, limit=limit)
    return text


def notice_batches(notices):
    """Group notices while keeping acknowledgements attached to their messages."""
    ids, lines, length = [], [], 0
    for event_id, notice in notices:
        line = str(notice)[:1850]
        if ids and length + len(line) + 1 > 1900:
            yield ids, "\n".join(lines)
            ids, lines, length = [], [], 0
        ids.append(event_id)
        lines.append(line)
        length += len(line) + 1
    if ids:
        yield ids, "\n".join(lines)
