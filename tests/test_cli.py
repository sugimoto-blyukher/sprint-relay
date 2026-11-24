from pathlib import Path

import pytest

from relay.__main__ import main, service_directory


def test_example_can_be_checked_without_tokens(monkeypatch, capsys):
    for name in ("GITHUB_TOKEN", "DISCORD_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    example = Path(__file__).parents[1] / "config.example.toml"
    main(["check", "--config", str(example)])
    assert "OK" in capsys.readouterr().out


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


def test_run_wires_private_service_in_manual_mode(tmp_path, monkeypatch):
    from relay.bot import Service, create_client

    calls = []
    monkeypatch.setattr("relay.__main__.os.umask", lambda mask: 0o077)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-github")
    monkeypatch.setenv("DISCORD_TOKEN", "fixture-discord")
    monkeypatch.setenv("AI_PROVIDER", "manual")

    def transport(token):
        assert token == "fixture-github"

        def get_repository(method, path):
            assert (method, path) == ("GET", "/repos/example/relay-management")
            return {"private": True}
        return get_repository

    def connect(config, token, github, workflow, provider, path):
        client = create_client(Service(config, github, workflow, provider, path))
        assert client.command_tree is not None
        assert workflow.changes.github is github
        assert workflow.provider is provider
        calls.append(token)

    monkeypatch.setattr("relay.github.transport", transport)
    monkeypatch.setattr("relay.bot.run", connect)
    example = Path(__file__).parents[1] / "config.example.toml"
    main(["run", "--config", str(example), "--state-dir", str(tmp_path / "state")])
    assert calls == ["fixture-discord"]
