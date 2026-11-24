"""Run the Discord service or check its deployment configuration."""
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from .config import load_config
from .schedule import Calendar


@contextmanager
def service_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise ValueError("State directory must be private (chmod 700)")
    with (path / "service.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another SprintRelay process is using this directory") from exc
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "run"])
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        calendar = Calendar.from_dict(config.calendar)
        unknown = calendar.availability.keys() - config.members.keys()
        if unknown:
            raise ValueError("Availability references an unknown team member")
        if args.command == "check":
            print("Configuration and calendar OK")
            return
        from .ai import get_provider
        from .bot import run
        from .changes import ChangeManager
        from .github import GitHub
        from .workflow import Workflow

        github_token = os.environ.get("GITHUB_TOKEN")
        discord_token = os.environ.get("DISCORD_TOKEN")
        if not github_token or not discord_token:
            raise ValueError("Set GITHUB_TOKEN and DISCORD_TOKEN in the service environment")
        os.umask(0o077)
        with service_directory(args.state_dir):
            path = args.state_dir / "sprint-relay.sqlite3"
            provider = get_provider()
            github = GitHub(config, github_token, path)
            management = github.request("GET", f"/repos/{config.management_repo}")
            if management.get("private") is not True:
                raise ValueError("Configure a private GitHub management repository for approved specifications")
            changes = ChangeManager(path, config, github)
            workflow = Workflow(path, config, github, provider, changes=changes)
            run(config, discord_token, github, workflow, provider, path)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"SprintRelay: {exc}\n")


if __name__ == "__main__":
    main()
