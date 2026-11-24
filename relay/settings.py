"""Validated deployment policy for MCP clients, scheduling and data release."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROVIDERS = ("muse", "claude_code", "codex", "antigravity")
EXECUTABLES = {"codex": "codex", "claude_code": "claude", "antigravity": "agy"}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class AgentSettings(Settings):
    enabled: bool = False
    verified: bool = False
    terms_confirmed: bool = False
    schedule_confirmed: bool = False
    executable: str | None = None
    transport: Literal["remote_mcp"] | None = None
    model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")


class PatrolSettings(Settings):
    interval_minutes: int = Field(default=45, ge=1, le=1440)
    grace_minutes: int = Field(default=10, ge=0, le=1440)


class WatchdogSettings(Settings):
    interval_seconds: int = Field(default=60, ge=1, le=3600)
    recovery_interval_minutes: int = Field(default=45, ge=1, le=1440)
    max_backoff_minutes: int = Field(default=360, ge=1, le=10080)


class JobSettings(Settings):
    probe_diagnostics: bool = False
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    max_retries: int = Field(default=1, ge=0, le=5)
    max_audit_concurrency: Literal[1] = 1
    max_management_concurrency: Literal[1] = 1


class SyncSettings(Settings):
    interval_seconds: int = Field(default=60, ge=5, le=3600)


class PrivacySettings(Settings):
    raw_retention_days: int = Field(default=30, ge=1, le=365)
    max_input_bytes: int = Field(default=1048576, ge=1024, le=4194304)
    project_providers: tuple[Literal["muse", "claude_code", "codex", "antigravity"], ...] = ()


class MCPSettings(Settings):
    local_bind: Literal["127.0.0.1"] = "127.0.0.1"
    local_port: int = Field(default=8371, ge=1024, le=65535)
    remote_enabled: bool = False
    remote_port: int = Field(default=8372, ge=1024, le=65535)
    issuer: str = ""
    jwks_url: str = ""
    resource: str = ""
    client_id: str = ""
    subject: str = ""

    @model_validator(mode="after")
    def check_oauth(self):
        if self.remote_enabled:
            if self.remote_port == self.local_port or not self.client_id or not self.subject:
                raise ValueError("Remote MCP needs a separate port and an authorized client and subject")
            for value in (self.issuer, self.jwks_url, self.resource):
                url = urlsplit(value)
                if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.query:
                    raise ValueError("Use explicit HTTPS issuer, JWKS and resource URLs")
            if urlsplit(self.resource).path != "/mcp":
                raise ValueError("Remote resource URL must end in /mcp")
        return self


def agent_settings(data):
    configured = dict(data)
    management_order = tuple(configured.pop("management", {}).get("order", ("muse", "claude_code", "codex")))
    audit_order = tuple(configured.pop("technical_audit", {}).get("order", ("codex", "antigravity")))
    if management_order != ("muse", "claude_code", "codex") or audit_order != ("codex", "antigravity"):
        raise ValueError("Use the v2 management/audit order and disable individual candidates as needed")
    if set(configured) - set(PROVIDERS):
        raise ValueError("Unknown agent configuration")
    result = {}
    for name in PROVIDERS:
        options = AgentSettings.model_validate(configured.get(name, {}))
        if options.executable is not None and options.executable != EXECUTABLES.get(name):
            raise ValueError("Only official, fixed provider executables are supported")
        result[name] = options
    return result
