"""Real loopback HTTP with the official client and actual leased job credentials."""
import asyncio
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from relay.config import Config
from relay.gateway import Gateway
from relay.jobs import JobStore
from relay.mcp_server import create_app
from relay.runtime import Listener
from relay.security import Principal, TokenVault


@pytest.mark.parametrize("role", ["management", "audit"])
def test_leased_probe_over_loopback(tmp_path, caplog, role):
    config = Config.load(Path(__file__).resolve().parents[1] / "config.example.toml")
    config.agents["codex"] = config.agents["codex"].model_copy(update={"terms_confirmed": True})
    project = config.management_repo
    jobs = JobStore(tmp_path / "probe.db", config)
    gateway = Gateway(project, config, jobs, None, None, None, None)
    now = [100.]
    vault = TokenVault(clock=lambda: now[0])
    jobs.maintenance(project)
    probe = jobs.list_jobs(project, provider="codex", role=role, kinds=["probe"])[0]
    claimed = jobs.claim(probe["id"], "codex")
    lease = claimed["lease_token"]
    principal = Principal("fixture-worker", "codex", role, project, probe["id"],
                          synthetic_probe=True, diagnose=True)
    token = vault.issue(principal, ttl=30, lease_token=lease)
    expired = vault.issue(principal, ttl=1, lease_token=lease)
    now[0] += 2

    async def run(sock):
        app = create_app(gateway, local_tokens=vault)
        port = sock.getsockname()[1]
        server = Listener(uvicorn.Config(app, access_log=False, log_config=None, lifespan="on"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(10):
            try:
                while not server.started:
                    await asyncio.sleep(.01)
                url = f"http://127.0.0.1:{port}/mcp"
                headers = {"Accept": "application/json, text/event-stream"}
                async with httpx.AsyncClient(trust_env=False, headers=headers) as unauth:
                    for invalid in ("PRIVATE_TOKEN_CANARY", expired):
                        response = await unauth.post(url, json={"method": "initialize", "id": "PRIVATE_ID_CANARY"},
                                                     headers={"Authorization": f"Bearer {invalid}"})
                        assert response.status_code == 401
                    denied_project = vault.issue(
                        Principal("fixture-worker", "codex", role, "other/project", probe["id"]),
                        lease_token=lease)
                    response = await unauth.post(url, headers={"Authorization": f"Bearer {denied_project}"})
                    assert response.status_code == 401
                async with httpx.AsyncClient(trust_env=False, headers={"Authorization": f"Bearer {token}"}) as client:
                    async with streamable_http_client(url, http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as conn:
                            await conn.initialize()
                            assert {t.name for t in (await conn.list_tools()).tools} == {
                                "get_project_context", "heartbeat_job"}
                            result = await conn.call_tool("get_project_context", {"project_id": project})
                            assert not result.isError
                            context = result.structuredContent
                            assert context["job"]["job_id"] == probe["id"]
                            assert context["job"]["lease_token"] == lease
                            args = {"project_id": project, "job_id": probe["id"], "lease_token": lease,
                                    "probe_answer": context["probe"]["challenge"][::-1]}
                            result = await conn.call_tool("heartbeat_job", args)
                            assert result.structuredContent["completed"] is True
                            assert jobs.get(probe["id"])["state"] == "COMPLETED"
                            proofs = jobs.status(project)["cli_proofs"]
                            assert [(p["provider"], p["role"]) for p in proofs] == [("codex", role)]
                            # A completed lease cannot be reused, even with the original bearer.
                            again = await conn.call_tool("heartbeat_job", args)
                            assert again.structuredContent["code"] == "LEASE_LOST"
                            wrong_project = await conn.call_tool("get_project_context", {"project_id": "other/project"})
                            assert wrong_project.structuredContent["code"] == "UNAUTHORIZED"

                retry, = jobs.retry_probe(project, "codex", role, actor=next(iter(config.approvers)))
                active = jobs.claim(retry["job_id"], "codex")
                scoped_token = vault.issue(
                    Principal("fixture-worker", "codex", role, project, retry["job_id"],
                              synthetic_probe=True, diagnose=True), lease_token=active["lease_token"])
                async with httpx.AsyncClient(trust_env=False,
                                             headers={"Authorization": f"Bearer {scoped_token}"}) as client:
                    async with streamable_http_client(url, http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as conn:
                            await conn.initialize()
                            foreign = await conn.call_tool("heartbeat_job", args)
                            assert foreign.structuredContent["code"] == "UNAUTHORIZED"
                            assert jobs.get(probe["id"])["state"] == "COMPLETED"
            finally:
                server.should_exit = True
                await serving

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        asyncio.run(run(sock))
    for code in ("MCP_AUTH_ACCEPTED", "MCP_AUTH_REJECTED", "MCP_INITIALIZE", "MCP_TOOLS_LIST",
                 "MCP_GET_PROJECT_CONTEXT", "MCP_HEARTBEAT_JOB", "MCP_TOOL_FAILED"):
        assert f"code={code} " in caplog.text
    assert all(secret not in caplog.text for secret in (lease, token, expired, "PRIVATE_TOKEN_CANARY",
                                                        "PRIVATE_ID_CANARY", principal.subject))
