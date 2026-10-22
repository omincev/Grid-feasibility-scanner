"""Offline checks of the VPS deploy helpers: scripts/deploy_checks.py against mocked PostgREST and Dagster GraphQL
answers, the raw read-back against an in-memory raw store, and scripts/offsite_copy.py to a local directory.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from scanner.core.raw_store import RawStore


def _load(root: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, root / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dc(root: Path) -> ModuleType:
    return _load(root, "deploy_checks")


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _entry(name: str, status: str = "LOADED", error: str | None = None) -> dict[str, Any]:
    loaded = {"__typename": "PythonError", "message": error} if error else {"__typename": "RepositoryLocation"}
    return {"name": name, "loadStatus": status, "locationOrLoadError": None if status == "LOADING" else loaded}


def _workspace(*entries: dict[str, Any]) -> httpx.Response:
    data = {"workspaceOrError": {"__typename": "Workspace", "locationEntries": list(entries)}}
    return httpx.Response(200, json={"data": data})


def test_postgrest_needs_200_and_a_json_array(dc: ModuleType) -> None:
    ok = _client(lambda r: httpx.Response(200, json=[{"entity_id": "dk.zone.dk1"}]))
    assert dc.check_postgrest(ok, "http://postgrest:3000") == "PostgREST /zone_card: HTTP 200, 1 row(s)"
    with pytest.raises(dc.CheckError, match="HTTP 503"):
        dc.check_postgrest(_client(lambda r: httpx.Response(503, text="down")), "http://postgrest:3000")


def test_locations_wait_while_loading_then_pass(dc: ModuleType) -> None:
    answers = [
        _workspace(_entry("ingestion"), _entry("analytics", "LOADING")),
        _workspace(_entry("ingestion"), _entry("analytics"), _entry("ops")),
    ]
    sleeps: list[float] = []
    client = _client(lambda r: answers.pop(0))
    detail = dc.check_locations(client, "http://d:3000", ["ingestion", "analytics", "ops"], 60, sleeps.append)
    assert detail == "Dagster code locations loaded: analytics, ingestion, ops" and sleeps == [5]


def test_a_location_with_a_load_error_fails(dc: ModuleType) -> None:
    client = _client(lambda r: _workspace(_entry("ingestion"), _entry("analytics", error="ImportError: boom\nmore")))
    with pytest.raises(dc.CheckError, match=r"analytics: ImportError: boom$"):
        dc.check_locations(client, "http://d:3000", ["ingestion", "analytics"], 0, lambda s: None)


def test_a_missing_location_times_out(dc: ModuleType) -> None:
    clock = iter([0.0, 0.0, 100.0])
    client = _client(lambda r: _workspace(_entry("ingestion")))
    with pytest.raises(dc.CheckError, match=r"missing \['ops'\]"):
        dc.check_locations(client, "http://d:3000", ["ingestion", "ops"], 10, lambda s: None, lambda: next(clock))


def _runs(*runs: tuple[str, float | None]) -> dict[str, Any]:
    results = [{"runId": i, "jobName": "j", "status": "STARTED", "startTime": t} for i, t in runs]
    return {"data": {"runsOrError": {"__typename": "Runs", "results": results}}}


def test_drain_polls_until_no_run_is_in_progress(dc: ModuleType) -> None:
    answers = [_runs(("a", 1.0)), _runs()]
    client = _client(lambda r: httpx.Response(200, json=answers.pop(0)))
    assert dc.drain(client, "http://d:3000", 60, lambda s: None) == []


def test_cancel_orphans_marks_only_runs_started_before_the_cutoff(dc: ModuleType) -> None:
    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if "terminateRuns" in body["query"]:
            result = {"__typename": "TerminateRunsResult", "terminateRunResults": [{"__typename": "x"}]}
            return httpx.Response(200, json={"data": {"terminateRuns": result}})
        assert body["variables"] == {"statuses": ["STARTING", "STARTED", "CANCELING"]}
        return httpx.Response(200, json=_runs(("old", 100.0), ("new", 200.0), ("never", None)))

    assert dc.cancel_orphans(_client(handle), "http://d:3000", 150.0) == ["old", "never"]
    assert sent[-1]["variables"] == {"ids": ["old", "never"]}
    assert "MARK_AS_CANCELED_IMMEDIATELY" in sent[-1]["query"]


class _Rows:
    def __init__(self, rows: list[tuple[str, datetime]]):
        self.rows = rows

    def execute(self, query: str, params: dict[str, int]) -> Any:
        rows = self.rows[: params["n"]]
        return type("Cursor", (), {"fetchall": lambda self: rows})()


def test_raw_readback_checks_each_capture_against_its_sidecar(dc: ModuleType) -> None:
    store = RawStore("memory://deploy-checks")
    at = datetime(2026, 10, 18, 12, tzinfo=UTC)
    refs = [
        store.put(source="eds", dataset="d", run_id=f"r{i}", seq=0, content=b"same", ext="json", fetched_at=at, meta={})
        for i in range(2)
    ]
    rows = _Rows([(r.uri, at) for r in refs])
    assert dc.raw_readback(rows, store, 3).endswith("2 newest capture(s) read back, SHA-256 as recorded")
    assert dc.raw_readback(_Rows([]), store, 3).endswith("no captures yet")
    with store.fs.open(store.resolve(refs[0].uri), "wb") as fh:  # the bytes changed under the reference
        fh.write(b"tampered")
    with pytest.raises(dc.CheckError, match="SHA-256 differs"):
        dc.raw_readback(rows, store, 3)


def test_offsite_copy_to_a_directory_url(root: Path, tmp_path: Path) -> None:
    oc = _load(root, "offsite_copy")
    dump = tmp_path / "scanner-20261018T023000Z.dump"
    dump.write_bytes(b"PGDMP" + bytes(100))
    target = oc.copy(dump, (tmp_path / "offsite").as_uri(), {"auto_mkdir": True})
    assert Path(target).read_bytes() == dump.read_bytes()
