"""Operate a single SprintRelay service and its private state directory."""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from .config import load_config
from .migration import backup, migrate, readonly
from .schedule import Calendar


@contextmanager
def service_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("State directory must be private (chmod 700)")
    fd = os.open(path / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another SprintRelay process is using this directory") from exc
        yield


def check(config):
    calendar = Calendar.from_dict(config.calendar)
    if calendar.availability.keys() - config.members.keys():
        raise ValueError("Availability references an unknown team member")


def doctor(config):
    from .settings import PROVIDERS

    print("MCP local listener: loopback; remote OAuth:", "configured" if config.mcp.remote_enabled else "disabled")
    for provider in PROVIDERS:
        setting = config.agents[provider]
        if not setting.enabled:
            state = "disabled"
        elif not (setting.verified and setting.terms_confirmed):
            state = "unverified"
        elif provider == "muse":
            state = "awaiting authenticated patrol" if config.mcp.remote_enabled else "remote MCP disabled"
        else:
            socket = Path("/run/sprintrelay-workers") / (provider + ".sock")
            state = "gateway socket present; runtime probe required" if socket.is_socket() else "gateway unavailable"
        print(f"{provider}: {state}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "doctor", "run", "migrate", "backup", "status"])
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    parser.add_argument("--dry-run", action="store_true", help="Trial migration in memory")
    parser.add_argument("--report", action="store_true", help="Include the private latest patrol report in status")
    parser.add_argument("--output", type=Path, help="New private backup file")
    args = parser.parse_args(argv)
    path = args.state_dir / "sprint-relay.sqlite3"
    try:
        config = load_config(args.config)
        check(config)
        if args.report and args.command != "status":
            parser.error("--report requires status")
        if (args.dry_run and args.command != "migrate") or (args.output and args.command != "backup"):
            parser.error("Use --dry-run with migrate and --output with backup")
        if args.command == "check":
            print("Configuration and calendar OK")
            return
        if args.command == "doctor":
            doctor(config)
            return
        if args.command == "status":
            from contextlib import closing
            with closing(readonly(path)) as db:
                states = dict(db.execute("SELECT state,COUNT(*) FROM jobs WHERE project=? GROUP BY state",
                                         (config.management_repo,)))
                result = {"jobs": states}
                if args.report:
                    from .patrol import latest_report
                    result["patrol_report"] = latest_report(db, config.management_repo, config.privacy.raw_retention_days)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "migrate" and args.dry_run:
            print("Migration verified" if migrate(path, project=config.management_repo, dry_run=True) else "Already current")
            return
        os.umask(0o077)
        with service_directory(args.state_dir):
            if args.command == "backup":
                if not args.output:
                    parser.error("backup requires --output")
                backup(path, args.output)
                print("SQLite backup verified")
                return
            if args.command == "migrate":
                print("Schema upgraded" if migrate(path, project=config.management_repo) else "Already current")
                return
            from .runtime import serve

            names = ("GITHUB_MANAGEMENT_TOKEN", "GITHUB_PRODUCT_TOKEN", "DISCORD_TOKEN")
            management, product, discord = (os.environ.get(name, "") for name in names)
            if not all((management, product, discord)) or management == product:
                raise ValueError("Set distinct management and product GitHub credentials and the Discord token")
            migrate(path, project=config.management_repo)
            asyncio.run(serve(config, args.config, path, management, product, discord))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"SprintRelay: {exc}\n")


if __name__ == "__main__":
    main()
