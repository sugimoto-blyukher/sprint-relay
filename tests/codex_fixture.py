"""Official CLI against loopback MCP and scripted Responses, with no account credentials."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket

import pytest
from starlette.responses import StreamingResponse
from starlette.routing import Route
import uvicorn

from relay.runtime import Listener
from relay.worker import _arguments, _CodexEvents, _invoke, _prompt

NAMESPACE = "mcp__sprintrelay"


def binary():
    configured = os.environ.get("SPRINTRELAY_TEST_CODEX")
    if not configured:
        pytest.skip("Set SPRINTRELAY_TEST_CODEX to a native Codex CLI 0.172.0 binary")
    return Path(configured).resolve(strict=True)


def offered_tools(body):
    definitions = list(body.get("tools", []))
    for item in body.get("input", []):
        if item.get("type") == "additional_tools":
            definitions.extend(item["tools"])
    return {tool["name"]: tool for spec in definitions if spec.get("name") == NAMESPACE
            for tool in spec["tools"]}


def call_item(name, args, index):
    return {"type": "function_call", "namespace": NAMESPACE, "name": name,
            "call_id": f"fixture-call-{index}", "arguments": json.dumps(args)}


FINAL = {"type": "message", "role": "assistant", "id": "mock-answer",
         "content": [{"type": "output_text", "text": "PRIVATE_FIXTURE_ANSWER"}]}


@asynccontextmanager
async def loopback(app, respond):
    index = 0
    async def fake_model(request):
        nonlocal index
        index += 1
        item = respond(await request.json(), index)
        response = {"id": f"fixture-response-{index}"}
        events = [{"type": "response.created", "response": response},
                  {"type": "response.output_item.done", "item": item},
                  {"type": "response.completed", "response": response}]
        return StreamingResponse(
            iter(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events),
            media_type="text/event-stream")

    app.router.routes.append(Route("/fixture/responses", fake_model, methods=["POST"]))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = Listener(uvicorn.Config(app, access_log=False, log_config=None))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(45):
                while not server.started:
                    await asyncio.sleep(.01)
                yield port
        finally:
            server.should_exit = True
            await serving


async def run_cli(path, tmp_path, port, req, *, direct=True, approved=True):
    env = {"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin",
           "LANG": "C.UTF-8", "SPRINTRELAY_JOB_TOKEN": req.token}
    rc, version, _ = await _invoke([str(path), "--version"], env, diagnostics=True)
    assert rc == 0 and version.strip() == b"codex-cli 0.172.0"
    args = _arguments("codex", req)[:-1]
    for flag in args.copy():
        if ((not direct and not approved and flag.startswith(("mcp_servers.sprintrelay.required=",
                                                               "mcp_servers.sprintrelay.enabled_tools=")))
                or (not direct and ".omit_tools_from=" in flag)
                or (not approved and '.approval_mode="approve"' in flag)):
            pos = args.index(flag)
            del args[pos-1:pos+1]
    args.extend(["-c", 'model_provider="fixture"', "-c",
                 f'model_providers.fixture={{name="Fixture",base_url="http://127.0.0.1:{port}/fixture",'
                 'wire_api="responses",request_max_retries=0,stream_max_retries=0}', "-"])
    observer = _CodexEvents(req)
    rc, _, _ = await _invoke([str(path), *args], env, _prompt(req).encode(), events=observer)
    assert rc == 0
    return observer
