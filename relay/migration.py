"""Offline SQLite migration and verified backups; never rewrite the GitHub ledger."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile


VERSION = 2


def _checked(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Use a private, regular state file (0600)")


def readonly(path):
    _checked(path)
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _integrity(db):
    if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise ValueError("SQLite integrity check failed")


def backup(source, destination):
    """Create a consistent SQLite copy, including committed WAL pages."""
    _checked(source)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Backup destination already exists")
    if destination.parent.stat().st_mode & 0o077:
        raise ValueError("Use a private backup directory (0700)")
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".backup-")
    os.close(fd)
    staging = Path(name)
    try:
        with closing(readonly(source)) as src, closing(sqlite3.connect(staging)) as target:
            src.backup(target)
            _integrity(target)
        os.link(staging, destination)
    finally:
        staging.unlink(missing_ok=True)


def _upgrade(db, project):
    from .jobs import migrate_jobs
    from .workflow import migrate_workflow

    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, VERSION):
        raise ValueError(f"Unsupported schema version {version}")
    if version == VERSION:
        return False
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    try:
        migrate_jobs(db)
        db.row_factory = sqlite3.Row
        migrate_workflow(db, project=project)
        if not db.in_transaction:
            raise RuntimeError("Migration unexpectedly ended its transaction")
        db.execute(f"PRAGMA user_version={VERSION}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return True


def migrate(path, *, project=None, dry_run=False):
    """Dry-run against an in-memory copy. Back up an existing DB before upgrade."""
    path = Path(path)
    if dry_run:
        with closing(sqlite3.connect(":memory:")) as copy:
            if path.exists():
                with closing(readonly(path)) as source:
                    source.backup(copy)
            return _upgrade(copy, project)
    if path.exists():
        with closing(readonly(path)) as db:
            current = db.execute("PRAGMA user_version").fetchone()[0]
            if current == VERSION:
                return False
            if current not in (0, 1):
                raise ValueError(f"Unsupported schema version {current}")
        # Preserve each pre-upgrade image, including the original after a failed attempt.
        from uuid import uuid4
        backup(path, path.with_name(f"pre-v2-{uuid4().hex}.sqlite3"))
    else:
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        return _upgrade(db, project)
