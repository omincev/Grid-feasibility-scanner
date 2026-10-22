"""Checks infra/deploy.sh runs in a one-off container on the compose network (infra/README.md, "VPS").

Usage: python scripts/deploy_checks.py <command> [options]

  smoke           PostgREST /zone_card answers 200 with a JSON array; every code location in the Dagster workspace
                  is loaded (GraphQL `workspaceOrError`), waiting up to --wait seconds; the newest raw captures read
                  back through RAW_STORE_URL with the SHA-256 their sidecars record. Exit 1 on any failure.
  raw             Only the raw-store read-back (--samples N).
  drain           Wait until no Dagster run is starting, started or canceling; exit 3 after --timeout seconds.
  cancel-orphans  Mark runs still in progress that started before --started-before (epoch seconds) as canceled.
                  deploy.sh calls it after replacing the Dagster containers: those runs' processes died with them,
                  and the default run launcher cannot check them, so they would hold their `source` tag's slot in
                  the run queue until run monitoring's max runtime (infra/dagster.yaml) fails them.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

IN_PROGRESS = ["STARTING", "STARTED", "CANCELING"]
EXPECTED_LOCATIONS = ("ingestion", "analytics", "ops")

WORKSPACE_QUERY = """
query {
  workspaceOrError {
    __typename
    ... on Workspace {
      locationEntries { name loadStatus locationOrLoadError { __typename ... on PythonError { message } } }
    }
    ... on PythonError { message }
  }
}
"""

RUNS_QUERY = """
query Runs($statuses: [RunStatus!]) {
  runsOrError(filter: {statuses: $statuses}) {
    __typename
    ... on Runs { results { runId jobName status startTime } }
    ... on PythonError { message }
  }
}
"""

CANCEL_MUTATION = """
mutation Cancel($ids: [String!]!) {
  terminateRuns(runIds: $ids, terminatePolicy: MARK_AS_CANCELED_IMMEDIATELY) {
    __typename
    ... on TerminateRunsResult { terminateRunResults { __typename } }
    ... on PythonError { message }
  }
}
"""

# The newest raw references: ingest runs' captures and snapshot captures, newest first.
RAW_SQL = """
select uri, at from (
    (select unnest(r.raw_uris) as uri, r.started_at as at
     from (select raw_uris, started_at from ops.ingest_run
           where cardinality(raw_uris) > 0 order by started_at desc limit %(n)s) r)
    union all
    (select raw_uri, fetched_at from ops.snapshot where raw_uri is not null order by fetched_at desc limit %(n)s)
) x
order by at desc
limit %(n)s
"""


class CheckError(RuntimeError):
    pass


def graphql(client: httpx.Client, dagster: str, query: str, variables: dict[str, Any] | None = None) -> Any:
    response = client.post(f"{dagster}/graphql", json={"query": query, "variables": variables or {}})
    if response.status_code != 200:
        raise CheckError(f"Dagster GraphQL answered HTTP {response.status_code}")
    body = response.json()
    if body.get("errors"):
        raise CheckError(f"Dagster GraphQL errors: {body['errors']}")
    return body["data"]


def check_postgrest(client: httpx.Client, postgrest: str) -> str:
    response = client.get(f"{postgrest}/zone_card", params={"limit": "100"})
    if response.status_code != 200:
        raise CheckError(f"PostgREST /zone_card answered HTTP {response.status_code}: {response.text[:300]}")
    rows = response.json()
    if not isinstance(rows, list):
        raise CheckError("PostgREST /zone_card did not return a JSON array")
    return f"PostgREST /zone_card: HTTP 200, {len(rows)} row(s)"


def location_states(data: Any) -> dict[str, str]:
    """Each code location's state: 'loaded', 'loading', or the load error's message."""
    workspace = data["workspaceOrError"]
    if workspace["__typename"] != "Workspace":
        raise CheckError(f"workspace failed to load: {workspace.get('message', workspace['__typename'])}")
    states = {}
    for entry in workspace["locationEntries"]:
        loaded = entry["locationOrLoadError"] or {}
        if entry["loadStatus"] == "LOADING":
            states[entry["name"]] = "loading"
        elif loaded.get("__typename") == "RepositoryLocation":
            states[entry["name"]] = "loaded"
        else:
            states[entry["name"]] = (loaded.get("message") or loaded.get("__typename") or "not loaded").strip()
    return states


def check_locations(
    client: httpx.Client,
    dagster: str,
    expected: Sequence[str],
    wait_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Every expected location loaded; while any is still loading (or the webserver is starting), retry."""
    deadline = clock() + wait_s
    while True:
        try:
            states = location_states(graphql(client, dagster, WORKSPACE_QUERY))
        except (httpx.HTTPError, CheckError) as exc:
            states, error = {}, str(exc)
        else:
            error = ""
        missing = [n for n in expected if n not in states]
        loading = [n for n, s in states.items() if s == "loading"]
        if states and not missing and not loading:
            break
        if clock() >= deadline:
            raise CheckError(f"Dagster code locations not ready: {error or states or 'none'}; missing {missing}")
        sleep(5)
    failed = {n: s for n, s in states.items() if s != "loaded"}
    if failed:
        detail = "; ".join(f"{n}: {(s.splitlines() or [''])[0][:300]}" for n, s in failed.items())
        raise CheckError(f"Dagster code location(s) failed to load: {detail}")
    return f"Dagster code locations loaded: {', '.join(sorted(states))}"


def in_progress_runs(client: httpx.Client, dagster: str) -> list[dict[str, Any]]:
    result = graphql(client, dagster, RUNS_QUERY, {"statuses": IN_PROGRESS})["runsOrError"]
    if result["__typename"] != "Runs":
        raise CheckError(f"runs query failed: {result.get('message', result['__typename'])}")
    runs: list[dict[str, Any]] = result["results"]
    return runs


def drain(
    client: httpx.Client,
    dagster: str,
    timeout_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict[str, Any]]:
    """Wait until no run is in progress; returns the runs still in progress at the timeout (empty if drained)."""
    deadline = clock() + timeout_s
    while True:
        runs = in_progress_runs(client, dagster)
        if not runs or clock() >= deadline:
            return runs
        print(f"waiting for {len(runs)} run(s): {', '.join(r['jobName'] for r in runs[:5])}", flush=True)
        sleep(10)


def cancel_orphans(client: httpx.Client, dagster: str, started_before: float) -> list[str]:
    """Mark in-progress runs that started before `started_before` (or never recorded a start) as canceled."""
    ids = [
        r["runId"]
        for r in in_progress_runs(client, dagster)
        if r["startTime"] is None or float(r["startTime"]) < started_before
    ]
    if ids:
        result = graphql(client, dagster, CANCEL_MUTATION, {"ids": ids})["terminateRuns"]
        if result["__typename"] != "TerminateRunsResult":
            raise CheckError(f"terminateRuns failed: {result.get('message', result['__typename'])}")
    return ids


def raw_readback(conn: Any, store: Any, samples: int) -> str:
    """Read the newest raw captures back through the store; each must match its sidecar's SHA-256."""
    uris = [uri for uri, _ in conn.execute(RAW_SQL, {"n": samples}).fetchall()]
    if not uris:
        return f"raw store {store.base_url}: no captures yet"
    for uri in uris:
        data = store.get(uri)
        expected = store.sidecar(uri)["sha256"]
        if hashlib.sha256(data).hexdigest() != expected:
            raise CheckError(f"raw capture {uri}: SHA-256 differs from its sidecar")
    return f"raw store {store.base_url}: {len(uris)} newest capture(s) read back, SHA-256 as recorded"


def _raw(samples: int) -> str:
    import psycopg

    from scanner.core.raw_store import RawStore
    from scanner.settings import get_settings

    s = get_settings()
    with psycopg.connect(s.database_url) as conn:
        conn.read_only = True
        return raw_readback(conn, RawStore(s.raw_store_url), samples)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("smoke", "raw", "drain", "cancel-orphans"))
    ap.add_argument("--postgrest", default="http://postgrest:3000")
    ap.add_argument("--dagster", default="http://dagster-webserver:3000")
    ap.add_argument("--locations", default=",".join(EXPECTED_LOCATIONS), help="code locations that must load")
    ap.add_argument("--wait", type=float, default=240, help="smoke: seconds to wait for the code locations")
    ap.add_argument("--samples", type=int, default=3, help="smoke, raw: raw captures to read back")
    ap.add_argument("--timeout", type=float, default=900, help="drain: seconds to wait")
    ap.add_argument("--started-before", type=float, default=None, help="cancel-orphans: epoch seconds")
    args = ap.parse_args(argv)

    with httpx.Client(timeout=30) as client:
        try:
            if args.command == "drain":
                left = drain(client, args.dagster, args.timeout)
                print(f"{len(left)} run(s) still in progress" if left else "no run in progress")
                return 3 if left else 0
            if args.command == "cancel-orphans":
                if args.started_before is None:
                    ap.error("cancel-orphans needs --started-before")
                ids = cancel_orphans(client, args.dagster, args.started_before)
                print(f"marked {len(ids)} orphaned run(s) as canceled: {', '.join(ids)}" if ids else "no orphaned run")
                return 0
            if args.command == "raw":
                print(f"ok   {_raw(args.samples)}")
                return 0
        except (CheckError, httpx.HTTPError) as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1

        checks: list[Callable[[], str]] = [
            lambda: check_postgrest(client, args.postgrest),
            lambda: check_locations(client, args.dagster, args.locations.split(","), args.wait),
            lambda: _raw(args.samples),
        ]
        failed = 0
        for check in checks:
            try:
                print(f"ok   {check()}", flush=True)
            except Exception as exc:  # every check reports; the exit code says whether any failed
                print(f"FAIL {type(exc).__name__}: {exc}", flush=True)
                failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
