"""Check that operator help and argument errors work without host changes."""
from pathlib import Path
import subprocess

import pytest


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


@pytest.mark.parametrize("script", ["redeploy.sh", "setup.sh"])
def test_operator_help_and_argument_validation(script):
    path = DEPLOY / script
    result = subprocess.run(["bash", str(path), "--help"], capture_output=True, text=True, check=True)
    assert f"deploy/{script}" in result.stdout
    result = subprocess.run(["bash", str(path), "--invalid"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_redeploy_requires_supported_worker_selector():
    result = subprocess.run(["bash", str(DEPLOY / "redeploy.sh"), "--start-worker", "muse"],
                            capture_output=True, text=True)
    assert result.returncode == 2


@pytest.mark.parametrize("failure", [False, True])
def test_redeploy_preserves_state_and_rolls_back_units(tmp_path, failure, monkeypatch):
    """Exercise the shell flow with temporary files and a fake systemd and venv."""
    import os
    import shlex
    import shutil

    checkout = tmp_path / "checkout"
    shutil.copytree(DEPLOY, checkout / "deploy")
    (checkout / "relay").mkdir()
    (checkout / "pyproject.toml").touch()
    app = tmp_path / "app"
    conf = tmp_path / "conf"
    state = tmp_path / "state"
    units = tmp_path / "units"
    backups = tmp_path / "backups"
    bin_dir = tmp_path / "bin"
    for path in (app / ".venv", conf, state, units, bin_dir):
        path.mkdir(parents=True)
    (app / ".venv/keep").touch()
    (conf / "config.toml").write_text("fixture settings\n")
    (conf / "core.env").write_text("DISCORD_TOKEN=fixture_secret\n")
    (conf / "core.env").chmod(0o600)
    (state / "sprint-relay.sqlite3").write_bytes(b"same database")
    (units / "sprintrelay.service").write_text("previous unit")
    common = checkout / "deploy/common.sh"
    with common.open("a") as out:
        for name, path in [("APP", app), ("CONFIG_DIR", conf), ("STATE", state), ("UNIT_DIR", units),
                           ("BACKUPS", backups), ("LOCK_FILE", tmp_path / "lock")]:
            out.write(f"\n{name}={shlex.quote(str(path))}\n")
        out.write("require_host() { :; }\nrequire_root_directory() { :; }\nas_core() { :; }\n")
    script = checkout / "deploy/redeploy.sh"
    script.write_text(script.read_text().replace("== 0:600", f"== {os.getuid()}:600"))

    def mock(name, content):
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + content)
        path.chmod(0o755)

    mock("getent", "exit 0\n")
    mock("python3", 'mkdir -p "$3/bin"\nprintf "#!/bin/sh\\nexit 0\\n" >"$3/bin/python"\nchmod +x "$3/bin/python"\n')
    mock("systemctl", r'''
printf '%s\n' "$*" >>"$SR_EVENTS"
if [[ $1 == is-active ]]; then [[ $3 == sprintrelay.service ]]; exit; fi
if [[ $1 == daemon-reload && $SR_FAIL == true && ! -e $SR_MARKER ]]; then
    touch "$SR_MARKER"
    exit 1
fi
''')
    events = tmp_path / "events"
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SR_EVENTS", str(events))
    monkeypatch.setenv("SR_MARKER", str(tmp_path / "once"))
    monkeypatch.setenv("SR_FAIL", str(failure).lower())
    result = subprocess.run(["bash", str(script), "--yes"], text=True, capture_output=True, timeout=15)
    assert result.returncode == int(failure), result.stdout + result.stderr
    assert "fixture_secret" not in result.stdout + result.stderr
    backup, = backups.iterdir()
    assert (backup / "state/sprint-relay.sqlite3").read_bytes() == b"same database"
    assert (backup / "units/sprintrelay.service").read_text() == "previous unit"
    assert (conf / "config.toml").read_text() == "fixture settings\n"
    assert "start sprintrelay.service" in events.read_text()
    if failure:
        assert (app / ".venv/keep").exists() and not (app / ".venv").is_symlink()
        assert (units / "sprintrelay.service").read_text() == "previous unit"
    else:
        assert (app / ".venv").is_symlink()
        assert list((app / ".venvs").glob("previous.*/keep"))
