"""Opt-in compatibility check with a native Codex CLI and a fake loopback model."""
import asyncio
import json
from pathlib import Path

import pytest
from codex_fixture import FINAL, binary, call_item, loopback, offered_tools, run_cli

from relay.config import Config
from relay.gateway import Gateway
from relay.jobs import JobStore
from relay.mcp_server import create_app
from relay.security import Principal, TokenVault
from relay.worker import Launch


EXPECTED_TOOLS = {"get_project_context", "heartbeat_job"}


@pytest.mark.parametrize("role,direct,approved", [
    ("management", False, False),
    ("management", True, False),
    ("management", True, True),
    ("audit", True, True),
])
def test_codex_0172_probe_tool_exposure(tmp_path, caplog, role, direct, approved):
    path = binary()

    async def run():
        config = Config.load(Path(__file__).resolve().parents[1] / "config.example.toml")
        config.agents["codex"] = config.agents["codex"].model_copy(update={"terms_confirmed": True})
        jobs = JobStore(tmp_path / "fixture.db", config)
        project = config.management_repo
        jobs.maintenance(project)
        job = jobs.list_jobs(project, provider="codex", role=role, kinds=["probe"])[0]
        lease = jobs.claim(job["id"], "codex")["lease_token"]
        vault = TokenVault()
        token = vault.issue(Principal("fixture", "codex", role, project, job["id"],
                                      synthetic_probe=True, diagnose=True), lease_token=lease)
        app = create_app(Gateway(project, config, jobs, None, None, None, None), local_tokens=vault)
        turns = []
        context_forwarded = False
        supplied_arguments = {"project_id": project, "job_id": job["id"], "lease_token": lease,
                              "probe_answer": job["payload"]["challenge"][::-1]}

        def reply(body, index):
            nonlocal context_forwarded
            offered = set(offered_tools(body))
            turns.append(offered)
            if direct and index <= 2 and turns[0] == EXPECTED_TOOLS:
                if index == 2 and approved:
                    outputs = [item for item in body.get("input", []) if item.get("type") == "function_call_output"]
                    # The real CLI forwarded the validated result to the mock model.
                    context_forwarded = job["payload"]["challenge"] in json.dumps(outputs)
                name = "get_project_context" if index == 1 else "heartbeat_job"
                args = {"project_id": project} if index == 1 else supplied_arguments
                return call_item(name, args, index)
            return FINAL

        async with loopback(app, reply) as port:
            req = Launch(job_id=job["id"], project=project, role=role, token=token,
                         mcp_url=f"http://127.0.0.1:{port}/mcp", timeout_seconds=30,
                         model="gpt-6.2-sol", kind="probe", diagnose=True)
            observer = await run_cli(path, tmp_path, port, req, direct=direct, approved=approved)
            assert turns and turns[0] == (EXPECTED_TOOLS if direct else set())
            if approved:
                assert context_forwarded
                assert jobs.get(job["id"])["state"] == "COMPLETED"
                assert [p["role"] for p in jobs.status(project)["cli_proofs"]] == [role]
                assert {name for code, name in observer.seen if code == "CLI_MCP_CALL_COMPLETED"} == EXPECTED_TOOLS
            else:
                assert jobs.get(job["id"])["state"] == "RUNNING"
                assert not jobs.status(project)["cli_proofs"]
                if direct:
                    assert {name for code, name in observer.seen if code == "CLI_MCP_APPROVAL_REQUIRED"} == EXPECTED_TOOLS
                    assert "code=MCP_GET_PROJECT_CONTEXT" not in caplog.text
                else:
                    assert not any(code.startswith("CLI_MCP_CALL") for code, _ in observer.seen)
            assert ("CLI_FINAL_RESPONSE_PRESENT", None) in observer.seen
            assert "http=200" in caplog.text and "tools=2" in caplog.text
            assert all(secret not in caplog.text for secret in (token, lease, "PRIVATE_FIXTURE_ANSWER"))

    asyncio.run(run())
