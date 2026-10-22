"""Logical backups of the database through the `db` container (infra/README.md, "Backups").

`backup` runs `pg_dump -Fc` inside the container (`docker exec`; no PostgreSQL client on the host) and
streams it to `<dir>/<database>-<UTC yyyymmddThhmmssZ>.dump`. The file is written as `.partial` and renamed
only after `pg_restore --list` reads it back, so a failed or truncated dump never counts as a backup and
never causes an older one to be deleted. Then only the newest `keep` dumps of that database are kept.

`restore` loads a dump into a new scratch database in the same container (`createdb`, then
`pg_restore --no-owner --exit-on-error`). It refuses a database that exists, so it never overwrites one; after
a failed restore, drop the scratch database before trying again. Roles are cluster-wide and not in the dump:
restoring on another server needs the migrations' roles there.
"""

from __future__ import annotations

import re
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from psycopg.conninfo import conninfo_to_dict

STAMP = "%Y%m%dT%H%M%SZ"
_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class BackupError(RuntimeError):
    pass


@dataclass
class BackupResult:
    path: Path
    size: int
    toc_entries: int
    deleted: list[Path] = field(default_factory=list)


def database_and_user(url: str) -> tuple[str, str]:
    """Database name and user of a connection URL (the container connects over its local socket)."""
    params = conninfo_to_dict(url)
    database, user = params.get("dbname"), params.get("user")
    if not database or not user:
        raise BackupError("DATABASE_URL must name the database and the user")
    return str(database), str(user)


def dumps(out_dir: Path, database: str) -> list[Path]:
    """Finished dumps of `database` in out_dir, oldest first."""
    pattern = re.compile(rf"^{re.escape(database)}-\d{{8}}T\d{{6}}Z\.dump$")
    return sorted(p for p in out_dir.glob(f"{database}-*.dump") if pattern.match(p.name))


def prune(out_dir: Path, database: str, keep: int) -> list[Path]:
    """Delete all but the newest `keep` dumps of `database`; returns the deleted paths."""
    if keep < 1:
        raise BackupError("keep at least one backup")
    old = dumps(out_dir, database)[:-keep]
    for p in old:
        p.unlink()
    return old


def _docker(container: str, *command: str, stdin_path: Path | None = None, stdout_path: Path | None = None) -> str:
    """Run a command in the container; stdout goes to stdout_path when given, else it is returned as text."""
    args = ["docker", "exec", *(["-i"] if stdin_path else []), container, *command]
    with ExitStack() as files:
        stdin: IO[bytes] | int = files.enter_context(stdin_path.open("rb")) if stdin_path else subprocess.DEVNULL
        stdout: IO[bytes] | int = files.enter_context(stdout_path.open("wb")) if stdout_path else subprocess.PIPE
        try:
            proc = subprocess.run(args, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, check=False)
        except OSError as exc:  # no docker executable
            raise BackupError(f"cannot run docker: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise BackupError(f"{command[0]} in {container} failed ({proc.returncode}): {detail[:2000]}")
    return "" if stdout_path else proc.stdout.decode("utf-8", "replace")


def toc_entries(container: str, dump: Path) -> int:
    """Entries in a dump's table of contents, as `pg_restore --list` in the container reads them."""
    listing = _docker(container, "pg_restore", "--list", stdin_path=dump)
    m = re.search(r"TOC Entries: (\d+)", listing)
    if m is None:
        raise BackupError(f"{dump.name}: pg_restore --list printed no table of contents")
    return int(m[1])


def backup(
    *, database: str, user: str, container: str, out_dir: Path, keep: int, now: datetime | None = None
) -> BackupResult:
    """Dump `database` into out_dir, check the dump, then prune to the newest `keep` (module docstring)."""
    if keep < 1:
        raise BackupError("keep at least one backup")
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{database}-{(now or datetime.now(UTC)).astimezone(UTC):{STAMP}}.dump"
    if final.exists():
        raise BackupError(f"{final} exists already")
    partial = final.with_name(final.name + ".partial")
    try:
        _docker(container, "pg_dump", "-U", user, "-d", database, "-Fc", stdout_path=partial)
        entries = toc_entries(container, partial)
    except BackupError:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(final)
    return BackupResult(final, final.stat().st_size, entries, prune(out_dir, database, keep))


def restore(dump: Path, *, database: str, user: str, container: str) -> None:
    """Create `database` (it must not exist) and restore the dump into it (module docstring)."""
    if not _NAME.match(database) or database == "postgres" or database.startswith("template"):
        raise BackupError(f"{database!r} is not a scratch database name (lower case letters, digits, _)")
    if not dump.is_file():
        raise BackupError(f"no dump at {dump}")
    try:
        _docker(container, "createdb", "-U", user, database)
    except BackupError as exc:
        raise BackupError(f"restore needs a new database; {exc}") from exc
    _docker(container, "pg_restore", "-U", user, "-d", database, "--no-owner", "--exit-on-error", stdin_path=dump)
