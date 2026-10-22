"""Backups through the db container (scanner.runtime.backup, CLI `backup` and `restore`).

Offline: naming, pruning and the partial-file rule, with `docker` replaced by a fake. Database test (marked
db): a real dump of TEST_DATABASE_URL's database through the container named by DB_CONTAINER (default
grid-scanner-db-1), pruning, and a restore into a new scratch database that is dropped afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from scanner import cli
from scanner.runtime import backup as b

T = datetime(2026, 10, 18, 15, 30, tzinfo=UTC)


def test_database_and_user_from_the_url() -> None:
    assert b.database_and_user("postgresql://scanner:pw@localhost:5432/scanner_x") == ("scanner_x", "scanner")
    with pytest.raises(b.BackupError):
        b.database_and_user("postgresql://localhost:5432/")


def test_prune_keeps_the_newest_dumps_of_one_database(tmp_path: Path) -> None:
    names = [f"scanner-2026101{d}T000000Z.dump" for d in range(1, 6)]
    others = ["scanner_x-20261011T000000Z.dump", "scanner-20261019T000000Z.dump.partial", "scanner-notes.dump"]
    for n in names + others:
        (tmp_path / n).write_bytes(b"x")
    deleted = b.prune(tmp_path, "scanner", keep=2)
    assert [p.name for p in deleted] == names[:3]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names[3:] + others)
    with pytest.raises(b.BackupError):
        b.prune(tmp_path, "scanner", keep=0)


class FakeDocker:
    """Stands in for subprocess.run of `docker exec ...`: pg_dump writes `dump`, pg_restore --list prints a TOC."""

    def __init__(self, dump: bytes = b"PGDMP fake", dump_rc: int = 0, toc: str = ";     TOC Entries: 12\n"):
        self.dump, self.dump_rc, self.toc = dump, dump_rc, toc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], stdin: Any, stdout: Any, stderr: Any, check: bool) -> Any:
        self.calls.append(args)
        out = b""
        if "pg_dump" in args:
            stdout.write(self.dump)
            return subprocess.CompletedProcess(args, self.dump_rc, None, b"pg_dump: error" if self.dump_rc else b"")
        if "--list" in args:
            assert stdin.read() == self.dump
            out = self.toc.encode()
        return subprocess.CompletedProcess(args, 0, out, b"")


def test_backup_writes_a_checked_dump_and_prunes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(b.subprocess, "run", docker)
    for i in range(3):
        result = b.backup(
            database="scanner", user="scanner", container="db1", out_dir=tmp_path, keep=2, now=T + timedelta(hours=i)
        )
    assert result.path.name == "scanner-20261018T173000Z.dump" and result.toc_entries == 12
    assert [p.name for p in result.deleted] == ["scanner-20261018T153000Z.dump"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "scanner-20261018T163000Z.dump",
        "scanner-20261018T173000Z.dump",
    ]
    assert docker.calls[0] == ["docker", "exec", "db1", "pg_dump", "-U", "scanner", "-d", "scanner", "-Fc"]
    assert docker.calls[1] == ["docker", "exec", "-i", "db1", "pg_restore", "--list"]


@pytest.mark.parametrize("docker", [FakeDocker(dump_rc=1), FakeDocker(toc="not an archive")])
def test_a_failed_or_unreadable_dump_is_no_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker: FakeDocker
) -> None:
    old = tmp_path / "scanner-20261001T000000Z.dump"
    old.write_bytes(b"old")
    monkeypatch.setattr(b.subprocess, "run", docker)
    with pytest.raises(b.BackupError):
        b.backup(database="scanner", user="scanner", container="db1", out_dir=tmp_path, keep=1, now=T)
    assert list(tmp_path.iterdir()) == [old]  # no partial file left, nothing pruned


def test_restore_refuses_names_that_are_not_scratch_databases(tmp_path: Path) -> None:
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"x")
    for name in ("postgres", "template1", "Scanner", "a;drop", ""):
        with pytest.raises(b.BackupError, match="scratch"):
            b.restore(dump, database=name, user="scanner", container="db1")


def test_cli_arguments() -> None:
    parse = cli.build_parser().parse_args
    a = parse(["backup"])
    assert (a.handler, a.dir, a.keep, a.container) == (cli.backup_cmd, None, None, None)
    a = parse(["backup", "--dir", "D:/backups", "--keep", "14", "--container", "db2"])
    assert (a.dir, a.keep, a.container) == (Path("D:/backups"), 14, "db2")
    a = parse(["restore", "x.dump", "--database", "scanner_restore_check"])
    assert (a.handler, a.dump, a.database) == (cli.restore_cmd, Path("x.dump"), "scanner_restore_check")
    with pytest.raises(SystemExit):
        parse(["restore", "x.dump"])


def _counts(url: str) -> tuple[Any, ...]:
    with psycopg.connect(url) as c:
        row = c.execute(
            "select (select max(version) from public.schema_migrations), (select count(*) from ops.ingest_run), "
            "(select count(*) from core.observation), (select count(*) from ref.entity), "
            "(select count(*) from pg_views where schemaname = 'serving')"
        ).fetchone()
    assert row is not None
    return tuple(row)


@pytest.mark.db
def test_backup_and_restore_round_trip_through_the_container(tmp_path: Path) -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    container = os.environ.get("DB_CONTAINER", "grid-scanner-db-1")
    if (
        shutil.which("docker") is None
        or subprocess.run(["docker", "exec", container, "true"], capture_output=True).returncode
    ):
        pytest.skip(f"docker container {container} not reachable")
    database, user = b.database_and_user(url)
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    first = b.backup(database=database, user=user, container=container, out_dir=tmp_path, keep=1, now=earlier)
    second = b.backup(database=database, user=user, container=container, out_dir=tmp_path, keep=1)
    assert second.deleted == [first.path] and list(tmp_path.iterdir()) == [second.path]
    assert second.toc_entries > 100

    scratch = f"{database}_restore_{uuid.uuid4().hex[:8]}"
    scratch_url = make_conninfo(url, dbname=scratch)
    try:
        b.restore(second.path, database=scratch, user=user, container=container)
        assert _counts(scratch_url) == _counts(url)
        with pytest.raises(b.BackupError, match="new database"):
            b.restore(second.path, database=scratch, user=user, container=container)
    finally:
        subprocess.run(["docker", "exec", container, "dropdb", "-U", user, "--if-exists", scratch], check=True)
