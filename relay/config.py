"""Load a deployment's role and repository allowlists from TOML."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib

from .settings import (AgentSettings, JobSettings, MCPSettings, PatrolSettings, PrivacySettings,
                       SyncSettings, WatchdogSettings, agent_settings)


ROLES = ("design", "frontend", "backend", "hardware")
TEAM = {"design": 2, "frontend": 1, "backend": 1, "hardware": 1}
REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
DISCORD_ID = re.compile(r"[1-9][0-9]{0,19}")


@dataclass
class Config:
    members: dict[str, str]
    approvers: set[str]
    github_users: dict[str, str]
    default_repo: str
    management_repo: str
    channels: dict[str, str]
    project_id: str | None = None
    product_repos: tuple[str, ...] = ()
    calendar: dict = field(default_factory=dict)
    guild_id: str = ""

    agents: dict[str, AgentSettings] = field(default_factory=lambda: agent_settings({}))
    management_order = ("muse", "claude_code", "codex")
    audit_order = ("codex", "antigravity")
    patrol: PatrolSettings = field(default_factory=PatrolSettings)
    watchdog: WatchdogSettings = field(default_factory=WatchdogSettings)
    jobs: JobSettings = field(default_factory=JobSettings)
    github_sync: SyncSettings = field(default_factory=SyncSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    mcp: MCPSettings = field(default_factory=MCPSettings)

    def __post_init__(self):
        if not self.product_repos:
            self.product_repos = (self.default_repo,)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        return load_config(path)


def _id(value) -> str:
    value = str(value)
    if not DISCORD_ID.fullmatch(value):
        raise ValueError(f"invalid Discord ID: {value}")
    return value


def load_config(path: str | Path) -> Config:
    with Path(path).open("rb") as f:
        data = tomllib.load(f)
    try:
        github = data["github"]
        discord = data["discord"]
        member_rows = data["members"]
        members = {_id(m["discord_id"]): m["role"] for m in member_rows}
        github_users = {_id(m["discord_id"]): m["github"] for m in member_rows if m.get("github")}
        channels = {key: _id(value) for key, value in discord["channels"].items()}
        approvers = {_id(user) for user in discord["approvers"]}
        default_repo = github["default_repo"]
        product_repos = tuple(github.get("product_repos", [default_repo]))
        management_repo = github["management_repo"]
        guild_id = _id(discord["guild_id"])
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid configuration: {exc}") from exc
    if len(members) != len(member_rows) or Counter(members.values()) != TEAM:
        raise ValueError("configure distinct Discord IDs for two designers and one of each engineering role")
    if not approvers or not {"meetings", "tasks", "notifications"} <= channels.keys():
        raise ValueError("configure approvers and meetings, tasks and notifications channels")
    if channels["meetings"] in (channels["tasks"], channels["notifications"]):
        raise ValueError("use a separate private meetings channel")
    if default_repo not in product_repos or management_repo.casefold() in {r.casefold() for r in product_repos}:
        raise ValueError("configure the default product repository separately from management")
    if not all(REPO_NAME.fullmatch(repo) and repo.split("/")[1] not in (".", "..")
               for repo in (*product_repos, management_repo)):
        raise ValueError("repositories must use owner/name format")
    usernames = [name.casefold() for name in github_users.values()]
    if len(usernames) != len(set(usernames)):
        raise ValueError("duplicate GitHub login in member mapping")
    mcp = MCPSettings(**data.get("mcp", {}))
    agents = agent_settings(data.get("agents", {}))
    if not mcp.remote_enabled:
        # A disabled connector cannot claim jobs, even if a previous smoke test succeeded.
        agents["muse"] = agents["muse"].model_copy(update={"schedule_confirmed": False})
    return Config(members, approvers, github_users, default_repo, management_repo,
                  channels, github.get("project_id") or None, product_repos,
                  data.get("calendar", {}), guild_id, agents,
                  PatrolSettings(**data.get("patrol", {})), WatchdogSettings(**data.get("watchdog", {})),
                  JobSettings(**data.get("jobs", {})), SyncSettings(**data.get("github_sync", {})),
                  PrivacySettings(**data.get("privacy", {})), mcp)
