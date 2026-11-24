from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from relay.__main__ import main, service_directory
from relay.config import Config
from relay.migration import backup, migrate
from relay.runtime import ConfigurationFile, Control

EXAMPLE = Path(__file__).parents[1] / "config.example.toml"


def test_example_can_be_checked_without_tokens(capsys):
    main(["check", "--config", str(EXAMPLE)])
    assert "OK" in capsys.readouterr().out
    main(["doctor", "--config", str(EXAMPLE)])
    assert "muse: unverified" in capsys.readouterr().out


def test_only_one_service_per_private_directory(tmp_path):
    path = tmp_path / "state"
    with service_directory(path):
        with pytest.raises(ValueError, match="Another SprintRelay"):
            with service_directory(path):
                pytest.fail("second writer")
    path.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        with service_directory(path):
            pytest.fail("shared state directory")


def test_run_migrates_and_starts_with_separate_credentials(tmp_path, monkeypatch):
    calls = []
    async def connect(config, config_path, db_path, management, product, discord):
        assert config_path == EXAMPLE
        with closing(sqlite3.connect(db_path)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        calls.append((management, product, discord))

    monkeypatch.setattr("relay.__main__.os.umask", lambda mask: 0o077)
    monkeypatch.setattr("relay.runtime.serve", connect)
    for name, value in (("GITHUB_MANAGEMENT_TOKEN", "fixture-management"),
                        ("GITHUB_PRODUCT_TOKEN", "fixture-product"), ("DISCORD_TOKEN", "fixture-discord")):
        monkeypatch.setenv(name, value)
    args = ["run", "--config", str(EXAMPLE), "--state-dir", str(tmp_path / "state")]
    main(args)
    assert calls == [("fixture-management", "fixture-product", "fixture-discord")]
    monkeypatch.setenv("GITHUB_PRODUCT_TOKEN", "fixture-management")
    with pytest.raises(SystemExit):
        main(args)


def test_a16_dry_run_upgrade_backup_and_restore_legacy(tmp_path):
    project = "example/management"
    path = tmp_path / "state.sqlite3"
    path.touch(mode=0o600)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE meeting(id TEXT PRIMARY KEY,source TEXT UNIQUE,transcript TEXT,consent INTEGER,head TEXT,published TEXT)")
        db.execute("CREATE TABLE revision(digest TEXT PRIMARY KEY,meeting_id TEXT,parent TEXT,markdown TEXT,minutes TEXT,actor TEXT,reason TEXT,report TEXT,reviewer TEXT,approved_by TEXT,plan TEXT)")
        db.execute("INSERT INTO meeting VALUES('m','fixture','old text',1,'r','r')")
        db.execute("""INSERT INTO revision VALUES('r','m',NULL,'unchanged','{}','human','original','{"blocking_questions":[]}', 'reviewer','human','[]')""")
        db.commit()
    assert migrate(path, project=project, dry_run=True)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='jobs'").fetchone() == (0,)
    assert migrate(path, project=project)
    assert not migrate(path, project=project)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT digest,approved_by,markdown FROM revision").fetchone() == ("r", "human", "unchanged")
        assert db.execute("SELECT digest,project,actor FROM workflow_approval").fetchone() == ("r", project, "human")
        assert db.execute("SELECT providers FROM workflow_v2").fetchone() == ("[]",)
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
    old, = tmp_path.glob("pre-v2-*.sqlite3")
    restored = tmp_path / "restored.sqlite3"
    backup(old, restored)
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert db.execute("SELECT head,published FROM meeting").fetchone() == ("r", "r")
    assert old.stat().st_mode & 0o077 == restored.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="already exists"):
        backup(old, restored)


def test_configuration_refresh_and_control(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE.read_text())
    cfg = Config.load(path)
    notices = []
    file = ConfigurationFile(path, cfg, lambda *args: notices.append(args))
    path.write_text(path.read_text().replace("interval_minutes = 45", "interval_minutes = 12", 1))
    file.refresh()
    assert cfg.patrol.interval_minutes == 12 and notices
    with pytest.raises(PermissionError):
        Control(cfg, None).pause("unknown")
    with pytest.raises(ValueError):
        path.write_text(path.read_text().replace('local_bind = "127.0.0.1"', 'local_bind = "0.0.0.0"'))
        file.refresh()


def test_core_uses_same_database_and_separate_mcp_listeners(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from relay import runtime
    from relay.settings import MCPSettings

    cfg = Config.load(EXAMPLE)
    cfg.mcp = MCPSettings(remote_enabled=True, client_id="connector", subject="account",
                          issuer="https://id.example", jwks_url="https://id.example/keys",
                          resource="https://relay.example/mcp")
    path = tmp_path / "relay.db"
    migrate(path, project=cfg.management_repo)
    boundaries, listeners = [], []
    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", inline)

    def repository(token):
        def send(method, path, data=None):
            assert (method, path) == ("GET", f"/repos/{cfg.management_repo}")
            assert token == "management"
            return {"private": True}
        return send

    def app(gateway, **auth):
        boundaries.append((gateway, auth))
        return lambda scope, receive, send: None

    class FakeListener:
        def __init__(self, config):
            listeners.append(config)
        async def serve(self):
            await asyncio.Event().wait()

    async def start(token):
        await asyncio.sleep(0)
        assert token == "discord"

    async def close():
        pass

    async def watchdog(self):
        await asyncio.Event().wait()

    monkeypatch.setattr("relay.github.transport", repository)
    monkeypatch.setattr("relay.mcp_server.create_app", app)
    monkeypatch.setattr("relay.watchdog.Watchdog.run", watchdog)
    monkeypatch.setattr(runtime, "Listener", FakeListener)
    monkeypatch.setattr(runtime, "create_client", lambda _: SimpleNamespace(start=start, close=close))
    asyncio.run(runtime.serve(cfg, EXAMPLE, path, "management", "product", "discord"))
    assert boundaries[0][0] is boundaries[1][0]
    assert set(boundaries[0][1]) == {"local_tokens"} and set(boundaries[1][1]) == {"oauth"}
    assert [(entry.host, entry.port, entry.proxy_headers) for entry in listeners] == [
        ("127.0.0.1", 8371, False), ("127.0.0.1", 8372, True)]
