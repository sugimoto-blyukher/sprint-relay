"""Render the exact GitHub issue payload used at approval and publication."""

TITLE_LIMIT = 256
BODY_LIMIT = 65_536


def render_issue(task, nonce="0" * 32):
    # The placeholder is the exact width of the UUID.hex assigned during publication.
    title = f"[{task.key}] {task.title}"
    criteria = "\n".join("- " + line for line in task.acceptance)
    artifacts = "\n".join("- " + item for item in task.deliverables)
    deps = ", ".join(task.dependencies) or "—"
    links = "\n".join("- " + item for item in task.links) or "—"
    remaining = "unknown" if task.remaining_hours is None else f"{task.remaining_hours:g} h"
    content = (
        f"**{task.key}** · {task.role} · {task.status}\n\n{task.description}"
        f"\n\n**Acceptance**\n\n{criteria}\n\n**Deliverables**\n\n{artifacts}"
        f"\n\n**Requirements** {', '.join(task.requirement_ids)}"
        f"\n\n**Dependencies** {deps} · **Remaining** {remaining}"
        f"\n\n**Evidence / PRs**\n\n{links}\n\n"
        "Use `!relay ACTION [VALUE]` in a new issue comment to update the task."
    ).replace("<!--", "&lt;!--")
    body = f"<!-- sprint-relay:{nonce} -->\n{content}\n<!-- /sprint-relay:{nonce} -->"
    if len(title) > TITLE_LIMIT or len(body) > BODY_LIMIT:
        raise ValueError(
            f"{task.key}: rendered GitHub issue exceeds {TITLE_LIMIT} title or {BODY_LIMIT} body characters"
        )
    return title, body
