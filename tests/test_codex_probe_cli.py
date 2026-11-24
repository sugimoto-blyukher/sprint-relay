"""Opt-in compatibility check with a native Codex CLI and a fake loopback model."""
import asyncio
import json
import os
from pathlib import Path
import socket

import pytest
from starlette.responses import StreamingResponse
from starlette.routing import Route
import uvicorn

from relay.config import Config
from relay.gateway import Gateway
from relay.jobs import JobStore
from relay.mcp_server import create_app
from relay.runtime import Listener
from relay.security import Principal, TokenVault
from relay.worker import Launch, _arguments, _CodexProbeEvents, _invoke, _prompt


EXPECTED_TOOLS = {"get_project_context", "heartbeat_job"}
NAMESPACE = "mcp__sprintrelay"


@pytest.mark.parametrize("role,direct,approved", [
    ("management", False, False),
    ("management", True, False),
    ("management", True, True),
    ("audit", True, True),
])
def test_codex_0172_probe_tool_exposure(tmp_path, caplog, role, direct, approved):
    configured = os.environ.get("SPRINTRELAY_TEST_CODEX")
    if not configured:
        pytest.skip("Set SPRINTRELAY_TEST_CODEX to a native Codex CLI 0.172.0 binary")
    binary = Path(configured).resolve(strict=True)

    async def run(sock):
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

        async def fake_model(request):
            nonlocal context_forwarded
            body = await request.json()
            definitions = list(body.get("tools", []))
            for item in body.get("input", []):
                if item.get("type") == "additional_tools":
                    definitions.extend(item.get("tools", []))
            offered = {child["name"] for spec in definitions if spec.get("name") == NAMESPACE
                       for child in spec.get("tools", [])}
            turns.append(offered)
            index = len(turns)
            response_id = f"fixture-response-{index}"
            if direct and index <= 2 and turns[0] == EXPECTED_TOOLS:
                if index == 2 and approved:
                    outputs = [item for item in body.get("input", []) if item.get("type") == "function_call_output"]
                    # The real CLI forwarded the validated result to the mock model.
                    context_forwarded = job["payload"]["challenge"] in json.dumps(outputs)
                name = "get_project_context" if index == 1 else "heartbeat_job"
                args = {"project_id": project} if index == 1 else supplied_arguments
                item = {"type": "function_call", "namespace": NAMESPACE, "name": name,
                        "call_id": f"fixture-call-{index}", "arguments": json.dumps(args)}
            else:
                item = {"type": "message", "role": "assistant", "id": "mock-answer",
                        "content": [{"type": "output_text", "text": "PRIVATE_FIXTURE_ANSWER"}]}
            events = [
                {"type": "response.created", "response": {"id": response_id}},
                {"type": "response.output_item.done", "item": item},
                {"type": "response.completed", "response": {"id": response_id}},
            ]
            return StreamingResponse(
                iter(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events),
                media_type="text/event-stream",
            )

        app.router.routes.append(Route("/fixture/responses", fake_model, methods=["POST"]))
        port = sock.getsockname()[1]
        server = Listener(uvicorn.Config(app, access_log=False, log_config=None))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(30):
                while not server.started:
                    await asyncio.sleep(.01)
                req = Launch(job_id=job["id"], project=project, role=role, token=token,
                             mcp_url=f"http://127.0.0.1:{port}/mcp", timeout_seconds=30,
                             model="gpt-6.2-sol", probe=True, diagnose=True)
                env = {"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin",
                       "LANG": "C.UTF-8", "SPRINTRELAY_JOB_TOKEN": token}
                rc, version, _ = await _invoke([str(binary), "--version"], env, diagnostics=True)
                assert rc == 0 and version.strip() == b"codex-cli 0.172.0"
                args = _arguments("codex", req)[:-1]
                if not direct:
                    index = args.index('mcp_servers.sprintrelay.omit_tools_from=["deferred","code_mode"]')
                    del args[index - 1:index + 1]
                if not approved:
                    for name in EXPECTED_TOOLS:
                        index = args.index(f'mcp_servers.sprintrelay.tools.{name}.approval_mode="approve"')
                        del args[index - 1:index + 1]
                # A synthetic provider speaks only to the loopback fixture.
                # Production account authentication and inference are not exercised.
                args.extend(["-c", 'model_provider="fixture"', "-c",
                             f'model_providers.fixture={{name="Fixture",base_url="http://127.0.0.1:{port}/fixture",'
                             'wire_api="responses",request_max_retries=0,stream_max_retries=0}', "-"])
                observer = _CodexProbeEvents(req)
                rc, _, _ = await _invoke([str(binary), *args], env, _prompt(req).encode(), events=observer)
                assert rc == 0
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
        finally:
            server.should_exit = True
            await serving

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        asyncio.run(run(sock))
