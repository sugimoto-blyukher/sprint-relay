import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from relay import worker


def request(**options):
    data = {"project": "demo", "job_id": "job123", "role": "audit", "token": "x" * 43,
            "mcp_url": "http://127.0.0.1:8765/mcp", "timeout_seconds": 2}
    return worker.Launch(**({"kind": "audit"} | data | options))


def test_a13_mounts_and_environment_exclude_repository_and_parent_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("SPRINT_RELAY_PRIVATE_CANARY", "must-stay-in-core")
    monkeypatch.setattr(worker, "ACCOUNTS", tmp_path)
    monkeypatch.setattr(worker, "INSTALL_DIR", tmp_path / "install")
    image = tmp_path / "install/codex"
    image.mkdir(parents=True)
    (image / "agent").touch()
    account = tmp_path / "sprintrelay-codex"
    account.mkdir(mode=0o700)
    profile = account / ".codex"
    profile.mkdir(mode=0o700)
    secret = profile / "auth.json"
    secret.touch(mode=0o600)
    monkeypatch.setattr(worker, "_trusted", lambda path: None)
    argv = worker._isolate("codex")
    assert argv[:3] == ["/usr/bin/bwrap", "--unshare-all", "--share-net"]
    assert "--disable-userns" in argv and "--bind" not in argv
    assert str(secret) in argv and str(account) not in argv
    assert all(path not in argv for path in ("/usr", "/home", "/var/run/docker.sock", "/workspace"))
    assert argv[-6:] == ["--remount-ro", "/proc", "--remount-ro", "/", "--", "/bin/agent"]
    assert "must-stay-in-core" not in worker._agent_env().values()
    assert set(worker._agent_env()) == {"HOME", "PATH", "LANG", "TMPDIR", "HTTPS_PROXY", "NO_PROXY"}
    secret.chmod(0o644)
    with pytest.raises(PermissionError):
        worker._isolate("codex")


def test_a13_cli_argv_and_google_security_profile(tmp_path):
    req = request()
    for provider, disabled in [("codex", "shell_tool"), ("claude_code", "--restricted"),
                               ("antigravity", "--sandbox")]:
        argv = worker._arguments(provider, req)
        assert disabled in argv and req.token not in str(argv)
    codex = worker._arguments("codex", req)
    assert "unified_exec" in codex and "read-only" in codex
    assert "forced_login_method=\"chatgpt\"" in codex
    google = worker._google_config(tmp_path, req)
    policy = json.loads(google[0][0].read_bytes())["permissions"]
    assert "command(*)" in policy["deny"] and policy["allow"] == ["mcp(sprintrelay/*)"]
    assert google[1][0].stat().st_mode & 0o077 == 0
    for bad in [{"mcp_url": "https://other.test/mcp"}, {"model": "--exec"}, {"role": "admin"},
                {"job_id": "job\nrun something"}, {"shell": "sh"}]:
        with pytest.raises(ValidationError):
            request(**bad)


def test_a08_cli_clean_exit_does_not_complete_a_job(tmp_path, monkeypatch):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()

        async def delayed_result():
            started.set()
            await finish.wait()
            return b"exited\n"

        monkeypatch.setattr(worker, "_trusted_socket", lambda path: None)
        reader, writer = Mock(), Mock()
        reader.readline = delayed_result
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(return_value=(reader, writer)))
        dispatch = asyncio.create_task(worker.run_cli("codex", **request().model_dump()))
        await asyncio.wait_for(started.wait(), 2)
        assert worker.Launch.model_validate_json(writer.write.call_args.args[0]) == request()
        assert not dispatch.done()  # Other management coroutines may continue immediately.
        finish.set()
        assert await dispatch is None  # Caller still needs the server-side submission.
        assert await worker.run_cli("unknown", **request().model_dump()) == "CAPABILITY"

    asyncio.run(scenario())


def test_api_backed_login_rejected_and_namespace_failure_stops_before_inference(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    root.mkdir()
    (root / "version").write_text("codex-fixture")
    monkeypatch.setattr(worker, "INSTALL_DIR", tmp_path)
    monkeypatch.setattr(worker, "_trusted", lambda path: None)
    monkeypatch.setattr(worker, "_isolate", lambda provider, bindings: ["fake-namespace", "/bin/agent"])
    invoke = AsyncMock(side_effect=[(0, b"codex-fixture", b""), (0, b"", b"Logged in using API key")])
    monkeypatch.setattr(worker, "_invoke", invoke)
    assert asyncio.run(worker._execute("codex", request())) == "AUTH"
    assert invoke.await_count == 2
    def deny_namespace(provider, bindings):
        raise PermissionError()
    monkeypatch.setattr(worker, "_isolate", deny_namespace)
    assert asyncio.run(worker._execute("codex", request())) == "CAPABILITY"
    assert invoke.await_count == 2


def test_timeout_kills_fake_cli_and_discards_its_output(tmp_path):
    pidfile = tmp_path / "pid"
    fixture = ("import os,sys,time; "
               "open(sys.argv[1],'w').write(str(os.getpid())); "
               "print('fake CLI result'); time.sleep(60)")

    async def scenario():
        job = asyncio.create_task(worker._invoke([sys.executable, "-c", fixture, str(pidfile)], {}))
        async with asyncio.timeout(4):
            while not pidfile.exists():
                await asyncio.sleep(.01)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(job, timeout=.05)
        pid = int(pidfile.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(scenario())


@pytest.mark.parametrize("err,code", [(b"rate limit", "QUOTA"),
                                      (b"429 too many requests\nRetry-After: 120", "QUOTA 120"), (b"login required", "AUTH"),
                                      (b"permission denied by policy", "PERMISSION"),
                                      (b"bwrap: cannot create namespace", "CAPABILITY")])
def test_classified_diagnostics_no_raw_output(err, code):
    assert worker._failure(err) == code


def test_a08_stdout_cannot_satisfy_watchdog(caplog):
    from relay.watchdog import Watchdog

    store = Mock()
    store.get.return_value = {"state": "RUNNING"}
    store.config.jobs.probe_diagnostics = True
    dispatch = AsyncMock(return_value=None)
    async def local_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    watchdog = Watchdog(store, "demo", dispatch=dispatch, worker=local_call)
    asyncio.run(watchdog._execute({"id": "a" * 32, "provider": "codex", "role": "management",
                                   "kind": "probe", "lease_token": "test-lease"}))
    args, kwargs = store.fail.call_args
    assert args == ("a" * 32, "codex", "test-lease", "FORMAT")
    assert "CLI_EXITED_WITHOUT_MCP_COMPLETION" in caplog.text
    assert "test-lease" not in caplog.text


def audit_snapshot():
    return {"sha": "b" * 40, "job_id": "job123", "truncated": True, "evidence": [
        {"id": "c" * 24, "path": "src/view.py", "text": "+return 2\n+password = 'synthetic'"}]}


def test_sealed_snapshot_contains_only_scrubbed_bound_evidence(tmp_path):
    data = audit_snapshot()
    req = request(snapshot=data)
    config = worker._google_config(tmp_path, req)
    rules = json.loads(config[0][0].read_text())["permissions"]
    assert "read_file(/evidence/context.json)" in rules["allow"]
    assert {"command(*)", "read_file(/session)", "read_file(/proc)", "write_file(*)"} <= set(rules["deny"])
    with worker._mount_snapshot(["bwrap", "--remount-ro", "/", "--", "/bin/agent"],
                                req.snapshot) as (cmd, fds):
        assert "/evidence/context.json" in cmd and "--ro-bind-data" in cmd
        contents = json.loads(os.pread(fds[0], 16384, 0))
        assert contents["sha"] == data["sha"]
        assert contents["evidence"][0]["text"] == "+return 2\n[redacted]"
        with pytest.raises(PermissionError):
            os.write(fds[0], b"overwrite")
    for bad in [dict(snapshot=data, role="management"), dict(snapshot=data, job_id="other"),
                dict(snapshot=dict(data, sha="untrusted-branch")),
                dict(snapshot=dict(data, evidence=[dict(data["evidence"][0], path="../token.py")]))]:
        with pytest.raises(ValidationError):
            request(**bad)


def test_a13_kernel_mount_boundary_when_namespaces_are_available(tmp_path, monkeypatch):
    """Actual mount/UID check on Linux hosts permitting unprivileged bwrap."""
    import shutil
    import subprocess

    if not shutil.which("cc") or not os.path.exists("/usr/bin/bwrap"):
        pytest.skip("This host lacks bubblewrap or a native C toolchain")
    image = tmp_path / "image/codex"
    image.mkdir(parents=True)
    probe = tmp_path / "probe.c"
    probe.write_text(r"""
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc > 1) return 0;
    const char *hidden[] = {"/workspace", "/bin/sh", "/run/docker.sock", "/etc/sprintrelay",
                            "/var/lib/sprintrelay", "/run/user", 0};
    for (int i=0; hidden[i]; i++) if (access(hidden[i], F_OK) == 0) return 1;
    if (getenv("SPRINT_RELAY_PRIVATE_CANARY") || getuid() == 0) return 2;
    struct statvfs fs;
    if (statvfs("/work", &fs) != 0 || !(fs.f_flag & ST_RDONLY)) return 3;
    if (open("/work/edit", O_CREAT|O_WRONLY, 0600) >= 0) return 3;
    if (statvfs("/proc", &fs) != 0 || !(fs.f_flag & ST_RDONLY)) return 10;
    if (access("/sys", F_OK) == 0) return 11;
    if (open("/session/.codex/auth.json", O_WRONLY) >= 0) return 4;
    const char *file = "/evidence/context.json";
    struct stat info;
    if (stat(file, &info) || (info.st_mode & 0777) != 0400) return 5;
    if (statvfs(file, &fs) || !(fs.f_flag & ST_RDONLY)) return 6;
    if (open(file, O_WRONLY) >= 0) return 7;
    char evidence[2048] = {0};
    int fd = open(file, O_RDONLY);
    if (fd < 0 || read(fd, evidence, sizeof(evidence)-1) < 1) return 8;
    if (!strstr(evidence, "[redacted]") || strstr(evidence, "synthetic")) return 9;
    return 0;
}
""")
    binary = image / "agent"
    build = subprocess.run(["cc", "-static", str(probe), "-o", str(binary)], capture_output=True)
    if build.returncode:
        pytest.skip("This host lacks the static C runtime for the containment fixture")
    kernel = subprocess.run(["/usr/bin/bwrap", "--unshare-all", "--share-net", "--unshare-user", "--disable-userns",
                             "--ro-bind", str(binary), "/test", "--", "/test", "probe"],
                            capture_output=True, timeout=5)
    if kernel.returncode and any(text in kernel.stderr.lower() for text in
                                 (b"not permitted", b"permission denied", b"no permissions",
                                  b"creating new namespace", b"unprivileged user namespaces",
                                  b"setting up uid map: read-only file system")):
        pytest.skip("Host kernel denies unprivileged user namespaces")
    assert kernel.returncode == 0, kernel.stderr.decode()
    if os.getuid() == 0:
        pytest.skip("Run the namespace acceptance test under the unprivileged worker UID")
    monkeypatch.setattr(worker, "_trusted", lambda path: None)
    monkeypatch.setattr(worker, "INSTALL_DIR", tmp_path / "image")
    monkeypatch.setattr(worker, "ACCOUNTS", tmp_path)
    account = tmp_path / "sprintrelay-codex"
    account.mkdir(mode=0o700)
    (account / ".codex").mkdir(mode=0o700)
    (account / ".codex/auth.json").touch(mode=0o600)
    monkeypatch.setenv("SPRINT_RELAY_PRIVATE_CANARY", "host only")
    snapshot = request(snapshot=audit_snapshot()).snapshot
    with worker._mount_snapshot(worker._isolate("codex"), snapshot) as (cmd, fds):
        actual = subprocess.run(cmd, env=worker._agent_env(), capture_output=True, timeout=5, pass_fds=fds)
    assert actual.returncode == 0, actual.stderr.decode()


@pytest.mark.parametrize(("invoke_result", "expected", "code"), [
    ([(2, b"", b"bwrap: fixture SECRET_PAYLOAD")], "CAPABILITY", "BWRAP_START_FAILED"),
    ([(0, b"wrong version SECRET_PAYLOAD", b"")], "CAPABILITY", "VERSION_MISMATCH"),
    ([(0, b"fixture", b""), (1, b"", b"SECRET_PAYLOAD")], "AUTH", "ACCOUNT_AUTH_FAILED"),
    ([(0, b"fixture", b""), (0, b"", b"Logged in using ChatGPT"),
      (2, b"", b"unexpected argument SECRET_PAYLOAD")], "CAPABILITY", "CLI_CONFIG_INCOMPATIBLE"),
    ([(0, b"fixture", b""), (0, b"", b"Logged in using ChatGPT"),
      (1, b"", b"MCP startup sprintrelay: failed to list tools SECRET_PAYLOAD")], "TRANSIENT", "MCP_CLIENT_LIST_FAILED"),
    ([FileNotFoundError("SECRET_PAYLOAD")], "UNAVAILABLE", "BINARY_START_FAILED"),
    ([TimeoutError("SECRET_PAYLOAD")], "TIMEOUT", "TIMEOUT"),
])
def test_probe_diagnostics_are_scoped_and_redacted(tmp_path, monkeypatch, caplog, invoke_result, expected, code):
    root = tmp_path / "codex"
    root.mkdir()
    (root / "version").write_text("fixture")
    monkeypatch.setattr(worker, "INSTALL_DIR", tmp_path)
    monkeypatch.setattr(worker, "_trusted", lambda path: None)
    monkeypatch.setattr(worker, "_isolate", lambda provider, bindings: ["fake-namespace", "/bin/agent"])
    monkeypatch.setattr(worker, "_invoke", AsyncMock(side_effect=invoke_result))
    req = request(job_id="a" * 32, kind="probe", diagnose=True)
    assert asyncio.run(worker._execute("codex", req)) == expected
    assert f"code={code} job={req.job_id} provider=codex role=audit" in caplog.text
    assert "SECRET_PAYLOAD" not in caplog.text and req.token not in caplog.text
    assert "mcp_servers.sprintrelay.required=true" in worker._arguments("codex", req)
    with pytest.raises(ValidationError):
        request(kind="minutes", role="audit", diagnose=True)


def test_codex_probe_events_report_calls_and_final_answer_without_payloads(caplog):
    req = request(kind="probe", diagnose=True)
    observed = worker._CodexEvents(req)
    records = [
        {"type": "thread.started", "thread_id": "never_log_thread"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "mcp_tool_call", "server": "sprintrelay",
                                         "tool": "get_project_context", "arguments": "SECRET_ARGUMENTS"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "sprintrelay",
                                           "tool": "get_project_context", "status": "completed", "result": "SECRET_RESULT"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "sprintrelay",
                                           "tool": "heartbeat_job", "status": "failed", "error": "SECRET_ERROR"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "sprintrelay",
                                           "tool": "SECRET_TOOL", "status": "completed"}},
        {"type": "item.completed", "item": {"type": "error",
                                           "message": "Code Mode is unavailable; install codex-code-mode-host SECRET_PATH"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "SECRET_ANSWER"}},
        {"type": "turn.completed"},
    ]
    stream = b"\n".join(json.dumps(record).encode() for record in records)
    for start in range(0, len(stream), 31):
        observed.feed(stream[start:start + 31])
    observed.finish()

    assert {code for code, _ in observed.seen} == {
        "CLI_MCP_CALL_STARTED", "CLI_MCP_CALL_COMPLETED", "CLI_MCP_CALL_FAILED",
        "CLI_TURN_COMPLETED", "CLI_FINAL_RESPONSE_PRESENT", "CODE_MODE_HOST_UNAVAILABLE"}
    assert "tool=get_project_context" in caplog.text and "tool=heartbeat_job" in caplog.text
    assert "SECRET" not in caplog.text and "never_log_thread" not in caplog.text


def test_codex_probe_events_skip_oversized_lines_and_distinguish_text_only_output(caplog):
    observed = worker._CodexEvents(request(kind="probe", diagnose=True))
    observed.feed(b"x" * (observed.MAX_LINE + 1))
    observed.feed(b'\n{"type":"turn.completed"}\ninvalid event\n')
    observed.finish()
    assert {code for code, _ in observed.seen} == {
        "CLI_JSON_TRUNCATED", "CLI_JSON_UNREADABLE", "CLI_TURN_COMPLETED", "CLI_FINAL_RESPONSE_ABSENT"}
    assert "invalid event" not in caplog.text


def test_codex_probe_and_audit_have_distinct_noninteractive_permissions():
    args = worker._arguments("codex", request(kind="probe", diagnose=True))
    assert "--json" in args
    assert 'mcp_servers.sprintrelay.omit_tools_from=["deferred","code_mode"]' in args
    assert {a for a in args if a.endswith('.approval_mode="approve"')} == {
        'mcp_servers.sprintrelay.tools.get_project_context.approval_mode="approve"',
        'mcp_servers.sprintrelay.tools.heartbeat_job.approval_mode="approve"',
    }
    plain = worker._arguments("codex", request())
    assert "--json" not in plain
    assert {a for a in plain if a.endswith('.approval_mode="approve"')} == {
        f'mcp_servers.sprintrelay.tools.{tool}.approval_mode="approve"' for tool in (
            "get_project_context", "heartbeat_job", "report_job_failure", "get_audit_context", "submit_audit_result")}
    assert "--json" in worker._arguments("codex", request(diagnose=True))
    quiet_probe = worker._arguments("codex", request(kind="probe"))
    assert "--json" not in quiet_probe


def test_full_specification_tool_is_limited_to_assigned_spec_audit():
    flag = 'mcp_servers.sprintrelay.tools.get_specification.approval_mode="approve"'
    assert flag in worker._arguments("codex", request(spec_audit=True))
    assert flag not in worker._arguments("codex", request(snapshot=audit_snapshot()))
    with pytest.raises(ValidationError):
        request(kind="probe", spec_audit=True)


@pytest.mark.parametrize("state,expected", [("RUNNING", "CLI_EXITED_WITHOUT_MCP_COMPLETION"),
                                            ("COMPLETED", "JOB_COMPLETED")])
def test_normal_diagnostics_use_durable_job_status(caplog, state, expected):
    from relay.watchdog import Watchdog

    store = Mock()
    store.get.return_value = {"state": state}
    store.config.jobs.job_diagnostics = True
    store.config.jobs.probe_diagnostics = False
    watchdog = Watchdog(store, "demo", dispatch=AsyncMock(return_value=None))
    asyncio.run(watchdog._execute({"id": "a" * 32, "provider": "codex", "role": "management",
                                   "kind": "minutes", "lease_token": "PRIVATE_LEASE"}))
    assert f"job code={expected}" in caplog.text and "PRIVATE_LEASE" not in caplog.text
    assert store.fail.called == (state == "RUNNING")
