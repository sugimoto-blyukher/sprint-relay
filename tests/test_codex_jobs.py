"""Scripted project jobs with real workflow, patrol, leases, and optional native Codex."""
import asyncio
import json

import pytest

from codex_fixture import FINAL, binary, call_item, loopback, offered_tools, run_cli
from relay.config import Config
from relay.gateway import Gateway
from relay.github import GitHub
from relay.jobs import JobStore
from relay.mcp_contracts import ALLOWED, T, TOOL_DESCRIPTIONS, codex_tools
from relay.mcp_server import BoundaryError, create_app
from relay.patrol import Patrol
from relay.security import Principal, TokenVault
from relay.worker import Launch
from relay.workflow import CHECKS, Workflow
from test_patrol import API, HEAD, INJECT

TRANSCRIPT = "PRIVATE_TRANSCRIPT agreed to a prototype."
SPEC = "# Prototype\nREQ-1 Deliver the prototype."
TASK = dict(key="DESIGN", title="Prototype", description="Prepare prototype", role="design",
            estimated_hours=1, dependencies=[], requirement_ids=["REQ-1"], deliverables=["Prototype"],
            acceptance=["Prototype opens"])


OPEN_QUESTION = "PRIVATE_QUESTION about a future layout"
RISK_REASON = "PRIVATE_REASON accepted for the prototype"


def project_calls(kind, project, forced):
    """The next call uses only input or results actually returned to the model."""
    ctx = yield "get_project_context", {"project_id": project}
    assert ctx["job"]["kind"] == kind and "probe" not in ctx
    args = {"project_id": project, "job_id": ctx["job"]["job_id"], "lease_token": ctx["job"]["lease_token"]}
    if kind == "patrol":
        page = yield "get_patrol_delta", args | {"limit": 1}
        refs = [page["changes"][0]["id"]]
        while page["next_cursor"] is not None:
            page = yield "get_patrol_delta", args | {"cursor": page["next_cursor"]}
        audit = yield "request_audit", args | dict(kind="pr", repo="owner/product", target="8",
                                                    sha=HEAD, question="Check coverage")
        assert audit["job_id"]
        correction = yield "publish_patrol_report", args | dict(summary="PRIVATE_REPORT", references="PRIVATE_WRONG")
        assert correction["code"] == "INVALID_INPUT" and correction["remaining_submissions"] == 5
        assert (correction["field"], correction["reason"]) == ("references", "TYPE")
        report = yield "publish_patrol_report", args | dict(summary="PRIVATE_REPORT", references=refs)
        assert report["receipt"] == "patrol:" + args["job_id"]
        checkpoint = yield "complete_patrol", args | {"snapshot_cursor": page["snapshot_cursor"]}
        assert checkpoint["checkpoint"] == page["snapshot_cursor"]
    elif kind == "audit":
        context = yield "get_audit_context", args
        assert context["sha"] == HEAD
        finding = dict(evidence_id=context["evidence"][0]["id"], category="test_coverage", severity="low",
                       summary="PRIVATE_FINDING", impact="Missing coverage", acceptance="Add a fixture")
        result = yield "submit_audit_result", args | {"findings": [finding]}
        assert result["findings"][0]["finding_id"]
    elif kind == "minutes":
        record = yield "get_transcript", args
        assert record["transcript"] == TRANSCRIPT
        minutes = dict(summary="PRIVATE_MINUTES", decisions=[dict(text="Build prototype", quote=TRANSCRIPT,
                       rationale="Trial", impact="Design")], proposals=[], open_questions=[])
        yield "submit_minutes", args | {"minutes": minutes}
    else:
        record = yield "get_specification", args
        assert record["minutes"]["summary"] == "PRIVATE_MINUTES"
        if kind == "specification":
            yield "submit_specification", args | {"markdown": SPEC, "evidence": [TRANSCRIPT]}
        elif kind == "validation":
            yield "submit_validation", args | dict(digest=record["digest"], blocking_questions=[OPEN_QUESTION] if forced else [],
                                                   checks={name: "Checked prototype" for name in CHECKS})
        elif kind == "task_plan":
            if forced:
                assert record["approval"] == {"forced": True, "reason": RISK_REASON, "blocking_questions": [OPEN_QUESTION]}
            yield "submit_task_plan", args | {"digest": record["digest"], "tasks": [TASK]}


@pytest.mark.parametrize("native", [False, True], ids=["gateway", "codex-0172"])
@pytest.mark.parametrize("forced", [False, True], ids=["ordinary", "forced"])
def test_patrol_audit_and_reviewed_meeting(tmp_path, caplog, native, forced):
    path = binary() if native else None
    cfg = Config.load("config.example.toml")
    cfg.default_repo = "owner/product"
    cfg.product_repos = ("owner/product",)
    cfg.privacy = cfg.privacy.model_copy(update={"project_providers": ("codex",)})
    cfg.agents["codex"] = cfg.agents["codex"].model_copy(update={"terms_confirmed": True})
    project, db = cfg.management_repo, tmp_path / "workflow.db"
    jobs = JobStore(db, cfg)
    api = API()
    github = GitHub(cfg, "", db, request=api)
    flow = Workflow(db, cfg, github, jobs=jobs, project=project)
    patrol = Patrol(cfg, github, jobs, db, workflow=flow)
    gateway = Gateway(project, cfg, jobs, flow, patrol, None, github)
    jobs.maintenance(project)
    for role in ("audit", "management"):
        job, = jobs.list_jobs(project, provider="codex", role=role, kinds=["probe"])
        claim = jobs.claim(job["id"], "codex")
        jobs.complete(job["id"], "codex", claim["lease_token"], {"challenge": job["payload"]["challenge"]})
    cfg.agents["codex"] = cfg.agents["codex"].model_copy(update={"verified": True})

    async def exercise(kind, job_id):
        job = jobs.claim(job_id, "codex")
        role = job["role"]
        vault = TokenVault()
        token = vault.issue(Principal("worker", "codex", role, project, job_id, diagnose=True),
                            lease_token=job["lease_token"])
        p = vault.verify(token)
        script = project_calls(kind, project, forced)
        executed = []
        if native:
            def respond(body, index):
                if index == 1:
                    defs = offered_tools(body)
                    assert set(defs) == set(codex_tools(role, kind)) <= ALLOWED[role]
                    for name, definition in defs.items():
                        assert TOOL_DESCRIPTIONS[name] in definition["description"]
                        wire = definition["parameters"]
                        source = T[name][0].model_json_schema()
                        assert set(wire["properties"]) == set(source["properties"]) - {"probe_answer"}
                        assert set(wire["required"]) == set(source["required"])
                        nested = {"submit_minutes": "quote", "submit_task_plan": "estimated_hours",
                                  "submit_validation": "cross_team", "submit_audit_result": "evidence_id"}
                        if name in nested:
                            assert nested[name] in json.dumps(wire)
                    result = None
                else:
                    output = next(it["output"] for it in reversed(body["input"])
                                  if it.get("type") == "function_call_output")
                    # The CLI prefixes the JSON tool result with timing metadata.
                    result, _ = json.JSONDecoder().raw_decode(output[output.index("{"):])
                try:
                    name, arguments = script.send(result)
                except StopIteration:
                    return FINAL
                executed.append(name)
                return call_item(name, arguments, index)

            app = create_app(gateway, local_tokens=vault)
            async with loopback(app, respond) as port:
                req = Launch(job_id=job_id, kind=kind, project=project, role=role,
                             token=token, mcp_url=f"http://127.0.0.1:{port}/mcp", timeout_seconds=40,
                             model="gpt-6.2-sol", diagnose=True)
                events = await run_cli(path, tmp_path, port, req)
                assert {tool for code, tool in events.seen if code == "CLI_MCP_CALL_COMPLETED"} == set(executed)
                assert ("CLI_FINAL_RESPONSE_PRESENT", None) in events.seen
                assert f"job code=MCP_CATALOG_READY job={job_id}" in caplog.text
        else:
            result = None
            while True:
                try:
                    name, arguments = script.send(result)
                except StopIteration:
                    break
                assert name in gateway.catalog(p)
                try:
                    result = gateway.dispatch(name, p, arguments)
                except BoundaryError as exc:
                    assert exc.code == "INVALID_INPUT"
                    result = dict(code=exc.code, field=exc.field, reason=exc.reason, remaining_submissions=exc.remaining)
        vault.revoke(token)
        assert jobs.get(job_id)["state"] == "COMPLETED"
        assert all(secret not in caplog.text for secret in (
            token, job["lease_token"], TRANSCRIPT, SPEC, INJECT, "PRIVATE_MINUTES", "PRIVATE_REPORT",
            OPEN_QUESTION, RISK_REASON, "PRIVATE_FINDING", "PRIVATE_WRONG", "PRIVATE_FIXTURE_ANSWER"))

    def run(kind, job_id):
        asyncio.run(exercise(kind, job_id))

    slot = jobs.patrol(project)
    run("patrol", slot)
    assert jobs.checkpoint(project) and jobs.artifact("patrol:" + slot)["summary"] == "PRIVATE_REPORT"
    audit, = jobs.list_jobs(project, kinds=["audit"])
    run("audit", audit["id"])
    assert patrol.audit_result(audit["id"], provider="codex")["findings"][0]["summary"] == "PRIVATE_FINDING"

    reviewer, approver = "10102", "10101"
    meeting = flow.import_meeting("fixture:meeting", TRANSCRIPT, reviewer, True, providers=["codex"])
    def state():
        return flow.show(meeting, reviewer)

    run("minutes", state()["job_id"])
    run("specification", state()["job_id"])
    draft = state()["digest"]
    with pytest.raises(ValueError):
        flow.approve(meeting, approver, draft)
    assert not jobs.list_jobs(project, kinds=["task_plan"])
    revised = flow.revise(meeting, reviewer, SPEC + "\nReview recorded.", "Review", based_on=draft)
    run("validation", state()["job_id"])
    assert not jobs.list_jobs(project, kinds=["task_plan"])
    flow.approve(meeting, approver, revised, force=forced, reason=RISK_REASON if forced else None)
    run("task_plan", state()["job_id"])
    flow.recover()
    assert state()["state"] == "PUBLISHED" and len(api.issues) == 1


@pytest.mark.parametrize("direct", [False, True], ids=["old-launch", "direct-without-approval"])
def test_incomplete_normal_job_with_native_cli(tmp_path, caplog, direct):
    """Isolate discovery from exec approval while the server has a valid patrol lease."""
    path = binary()
    cfg = Config.load("config.example.toml")
    cfg.privacy = cfg.privacy.model_copy(update={"project_providers": ("codex",)})
    cfg.agents["codex"] = cfg.agents["codex"].model_copy(update={"terms_confirmed": True})
    jobs = JobStore(tmp_path / "normal.db", cfg)
    project = cfg.management_repo
    jobs.maintenance(project)
    probe, = jobs.list_jobs(project, provider="codex", role="management", kinds=["probe"])
    lease = jobs.claim(probe["id"], "codex")["lease_token"]
    jobs.complete(probe["id"], "codex", lease, {"challenge": probe["payload"]["challenge"]})
    cfg.agents["codex"] = cfg.agents["codex"].model_copy(update={"verified": True})
    slot = jobs.patrol(project)
    lease = jobs.claim(slot, "codex")["lease_token"]
    vault = TokenVault()
    token = vault.issue(Principal("worker", "codex", "management", project, slot, diagnose=True),
                        lease_token=lease)
    gateway = Gateway(project, cfg, jobs, None, None, None, None)

    def respond(body, index):
        if index == 1:
            expected = set(codex_tools("management", "patrol")) if direct else set()
            assert set(offered_tools(body)) == expected
            if direct:
                return call_item("get_project_context", {"project_id": project}, index)
        return FINAL

    async def run():
        async with loopback(create_app(gateway, local_tokens=vault), respond) as port:
            req = Launch(job_id=slot, kind="patrol", project=project, role="management", token=token,
                         mcp_url=f"http://127.0.0.1:{port}/mcp", timeout_seconds=30,
                         model="gpt-6.2-sol", diagnose=True)
            events = await run_cli(path, tmp_path, port, req, direct=direct, approved=False)
            assert ("CLI_FINAL_RESPONSE_PRESENT", None) in events.seen
            assert (("CLI_MCP_APPROVAL_REQUIRED", "get_project_context") in events.seen) == direct
            assert jobs.get(slot)["state"] == "RUNNING"
            assert "code=MCP_GET_PROJECT_CONTEXT" not in caplog.text

    asyncio.run(run())
