"""Apply dbmate-format migrations without dbmate (CI, local stack and `scanner.cli migrate`).

Reads db/migrations/*.sql in order, runs each file's `-- migrate:up` section in its own transaction,
and records versions in public.schema_migrations exactly like dbmate, so both tools can be mixed.
Uses psycopg, so no psql binary is needed. `skip_spatial` skips files whose name contains 'spatial'
(for environments without PostGIS; never in production).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg

from scanner import REPO_ROOT

MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


class MigrationError(RuntimeError):
    def __init__(self, name: str, cause: psycopg.Error):
        super().__init__(f"FAILED {name}\n{cause}")
        self.name = name


def up_section(sql: str) -> str:
    up = sql.split("-- migrate:up", 1)[1]
    return up.split("-- migrate:down", 1)[0]


def migrate(
    database_url: str,
    *,
    skip_spatial: bool = False,
    migrations_dir: Path = MIGRATIONS_DIR,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Apply pending migrations; return the file names applied. Raises MigrationError on the first failure."""
    applied_now: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("create table if not exists public.schema_migrations (version varchar(128) primary key)")
        applied = {row[0] for row in conn.execute("select version from public.schema_migrations")}
        for path in sorted(migrations_dir.glob("*.sql")):
            version = path.name.split("_", 1)[0]
            if version in applied:
                continue
            if skip_spatial and "spatial" in path.name:
                log(f"skip {path.name} (spatial)")
                continue
            try:
                with conn.transaction():
                    # No parameters: psycopg sends the whole section as one multi-statement query.
                    conn.execute(up_section(path.read_text(encoding="utf-8")))
                    conn.execute("insert into public.schema_migrations (version) values (%s)", (version,))
            except psycopg.Error as exc:
                raise MigrationError(path.name, exc) from exc
            log(f"applied {path.name}")
            applied_now.append(path.name)
    return applied_now
