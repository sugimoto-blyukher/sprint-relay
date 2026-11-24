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
    return worker.Launch(**(data | options))


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
    assert argv[-4:] == ["--remount-ro", "/", "--", "/bin/agent"]
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


def test_a08_stdout_cannot_satisfy_watchdog():
    from relay.watchdog import Watchdog

    store = Mock()
    store.get.return_value = {"state": "RUNNING"}
    dispatch = AsyncMock(return_value=None)
    async def local_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    watchdog = Watchdog(store, "demo", dispatch=dispatch, worker=local_call)
    asyncio.run(watchdog._execute({"id": 8, "provider": "codex", "lease_token": "test-lease"}))
    args, kwargs = store.fail.call_args
    assert args == (8, "codex", "test-lease", "FORMAT")


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
    if (open("/session/.codex/auth.json", O_WRONLY) >= 0) return 4;
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
    actual = subprocess.run(worker._isolate("codex"), env=worker._agent_env(), capture_output=True, timeout=5)
    assert actual.returncode == 0, actual.stderr.decode()
