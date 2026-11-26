"""Stage explicitly public repository documentation for MkDocs.

Keep source Markdown in the repository; never copy state, environment files,
credentials, or arbitrary directories into the Pages artifact.
"""
from pathlib import Path
import os
import posixpath
import re
import shutil
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / ".docs-src"
REPOSITORY = "https://github.com/sugimoto-blyukher/sprint-relay"

PAGES = {
    "README.md": "overview.md",
    "PHILOSOPHY.md": "PHILOSOPHY.md",
    "PHILOSOPHY.en.md": "PHILOSOPHY.en.md",
    "CONTRIBUTING.md": "CONTRIBUTING.md",
    "CONTRIBUTING.en.md": "CONTRIBUTING.en.md",
    "AGENTS.md": "AGENTS.md",
    "sprint-relay-v2-specification.md": "sprint-relay-v2-specification.md",
    "sprint-relay-implementation.md": "sprint-relay-implementation.md",
    "docs/workflow.md": "docs/workflow.md",
    "docs/discord.md": "docs/discord.md",
    "docs/deployment.md": "docs/deployment.md",
    "docs/integrations.md": "docs/integrations.md",
    "docs/acceptance.md": "docs/acceptance.md",
    "docs/codex-probe.md": "docs/codex-probe.md",
    "docs/agents/README.md": "docs/agents/index.md",
    "docs/agents/management.md": "docs/agents/management.md",
    "docs/agents/audit.md": "docs/agents/audit.md",
    "website/index.md": "index.md",
    "website/quickstart.md": "quickstart.md",
    "website/commands.md": "commands.md",
    "website/cli.md": "cli.md",
    "website/mcp.md": "mcp.md",
    "website/troubleshooting.md": "troubleshooting.md",
    "website/maintainers.md": "maintainers.md",
}

LINK = re.compile(r"(!?\[[^\]\n]*\]\()([^()\s]+)(\))")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def rewrite_target(target, source, root, revision):
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
        return target
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), parts.path))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError(f"{source}: link escapes the repository: {target}")
    # GitHub directory links normally point to their README.
    mapped = PAGES.get(resolved) or PAGES.get(resolved + "/README.md")
    if mapped is not None:
        relative = posixpath.relpath(mapped, posixpath.dirname(PAGES[source]) or ".")
        return urlunsplit(("", "", relative, parts.query, parts.fragment))
    candidate = root / resolved
    if not candidate.exists():
        raise ValueError(f"{source}: missing link target: {target}")
    # Source files and deployment assets remain on GitHub, not in the site.
    kind = "tree" if candidate.is_dir() else "blob"
    url = f"{REPOSITORY}/{kind}/{quote(revision, safe='')}/{quote(resolved, safe='/@')}"
    return urlunsplit(("", "", url, parts.query, parts.fragment))


def rewrite_markdown(content, source, root, revision):
    result = []
    fence = None
    for line in content.splitlines(keepends=True):
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            result.append(line)
        elif fence is not None:
            result.append(line)
        else:
            result.append(LINK.sub(
                lambda m: m[1] + rewrite_target(m[2], source, root, revision) + m[3],
                line,
            ))
    return "".join(result)


def main():
    revision = os.environ.get("GITHUB_SHA", "main")
    # Validate and render everything before replacing generated sources.
    rendered = {}
    for source, dest in PAGES.items():
        path = ROOT / source
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or symlinked documentation: {source}")
        rendered[dest] = rewrite_markdown(path.read_text(encoding="utf-8"), source, ROOT, revision)
    if DEST.is_symlink():
        raise ValueError("Refusing to replace a symlinked staging directory")
    if DEST.exists():
        shutil.rmtree(DEST)
    for dest, content in rendered.items():
        path = DEST / dest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"Staged {len(rendered)} documentation pages in {DEST.name}")


if __name__ == "__main__":
    main()
