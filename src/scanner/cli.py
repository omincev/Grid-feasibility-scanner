"""Dagster-independent command line: `uv run python -m scanner.cli <command>`.

Commands call the same plain functions as the Dagster assets. Settings (DATABASE_URL, RAW_STORE_URL,
...) come from the environment through scanner.settings. To add a command, write a function that
takes the parsed arguments and returns an exit code, and decorate it with @command; heavy imports go
inside the function so `--help` stays fast.
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scanner.settings import get_settings

if TYPE_CHECKING:
    from scanner.core.manifest import Manifest
    from scanner.runtime.context import RepoState

Handler = Callable[[argparse.Namespace], int]
Argument = tuple[tuple[str, ...], dict[str, Any]]
_COMMANDS: dict[str, tuple[str, tuple[Argument, ...], Handler]] = {}


def arg(*flags: str, **kwargs: Any) -> Argument:
    """One argument of a @command, with the parameters of ArgumentParser.add_argument."""
    return flags, kwargs


def command(name: str, description: str, *arguments: Argument) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        _COMMANDS[name] = (description, arguments, fn)
        return fn

    return register


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scanner.cli", description="Grid Opportunity Scanner runtime.")
    sub = ap.add_subparsers(dest="command", required=True, metavar="command")
    for name, (description, arguments, fn) in _COMMANDS.items():
        p = sub.add_parser(name, help=description, description=description)
        for flags, kwargs in arguments:
            p.add_argument(*flags, **kwargs)
        p.set_defaults(handler=fn)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    return handler(args)


@command("migrate", "Apply pending db/migrations to DATABASE_URL.")
def migrate_cmd(args: argparse.Namespace) -> int:
    from scanner.runtime.migrate import MigrationError, migrate

    try:
        migrate(get_settings().database_url)
    except MigrationError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


@command(
    "load-curated",
    "Load curated files from git (and RESTRICTED_CURATED_PATH) into the ref and core tables (ADR 0006).",
    arg("--dry-run", action="store_true", help="print the planned changes and write nothing"),
)
def load_curated_cmd(args: argparse.Namespace) -> int:
    from scanner.core.curated import CuratedError, load

    s = get_settings()
    try:
        report = load(s.database_url, s.repo_root, s.restricted_curated_path, dry_run=args.dry_run)
    except CuratedError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(report.text())
    return 0


@dataclass(frozen=True)
class DayResult:
    dataset: str
    day: date
    status: str  # ok, blocked (hard fingerprint deviation) or failed (exception)
    rows_written: int
    error: str | None = None


def dataset_label(m: Manifest) -> str:
    return f"{m.source}.{m.dataset}"


def ingest_days(manifest: Manifest, start: date, end: date | None, today: date) -> list[date]:
    """Local days from start to end inclusive (end defaults to today), clipped to the manifest's history."""
    first = max(start, manifest.history_start_local)
    last = min(end or today, manifest.history_end_local or today)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def select_manifests(manifests: Sequence[Manifest], name: str) -> list[Manifest]:
    """Ingestible manifests (active or backfill_only) matching a dataset or asset name, or all of them."""
    ingestible = [m for m in manifests if m.status in ("active", "backfill_only")]
    if name == "all":
        return ingestible
    chosen = [m for m in ingestible if name in (m.dataset, m.asset_name)]
    if not chosen:
        names = ", ".join(m.dataset for m in ingestible)
        raise ValueError(f"no active or backfill_only manifest named {name!r}; choose from: {names}, all")
    return chosen


def ingest_dataset(
    state: RepoState,
    manifest: Manifest,
    start: date,
    end: date | None,
    on_day: Callable[[DayResult], None] = lambda r: None,
    days: Sequence[date] | None = None,
) -> list[DayResult]:
    """run_ingest for each local day, in order. A failed day is recorded and the loop goes on.

    `days` replaces the range with those days (catch-up). Days of one dataset must stay sequential:
    overlapping lookback windows written concurrently could insert duplicate revisions.
    """
    from scanner.core.time import local_day
    from scanner.runtime import ingest

    today = local_day(datetime.now(UTC), state.countries[manifest.country].timezone)
    results: list[DayResult] = []
    for day in days if days is not None else ingest_days(manifest, start, end, today):
        try:
            out = ingest.run_ingest(state, manifest, day)
            error = None if out.status == "ok" else ("; ".join(out.deviations) or out.status)
            result = DayResult(dataset_label(manifest), day, out.status, out.rows_written, error)
        except Exception as exc:  # recorded per day; the run record is closed by run_ingest itself
            result = DayResult(dataset_label(manifest), day, "failed", 0, f"{type(exc).__name__}: {exc}")
        on_day(result)
        results.append(result)
    return results


def summarise(labels: Sequence[str], results: Sequence[DayResult]) -> str:
    lines = [f"{'dataset':<40} {'days':>5} {'ok':>5} {'blocked':>7} {'failed':>6} {'rows_written':>12}"]
    for label in labels:
        rs = [r for r in results if r.dataset == label]
        n = Counter(r.status for r in rs)
        rows = sum(r.rows_written for r in rs)
        lines.append(f"{label:<40} {len(rs):>5} {n['ok']:>5} {n['blocked']:>7} {n['failed']:>6} {rows:>12}")
    bad = [f"{r.dataset} {r.day}" for r in results if r.status != "ok"]
    if bad:
        lines.append(f"{len(bad)} day(s) not ok: {', '.join(bad[:20])}{' ...' if len(bad) > 20 else ''}")
    return "\n".join(lines)


@command(
    "ingest",
    "Ingest local days per dataset (sequential within a dataset), continuing past failed days.",
    arg("--dataset", required=True, help="dataset name (e.g. DayAheadPrices), asset name, or 'all'"),
    arg("--from", dest="start", required=True, type=date.fromisoformat, help="first local day, YYYY-MM-DD"),
    arg("--to", dest="end", type=date.fromisoformat, default=None, help="last local day, inclusive (default today)"),
    arg("--workers", type=int, default=1, help="datasets run in parallel threads (default 1)"),
)
def ingest_cmd(args: argparse.Namespace) -> int:
    from scanner.runtime.context import repo_state

    state = repo_state()
    try:
        manifests = select_manifests(state.manifests, args.dataset)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    lock = threading.Lock()

    def on_day(r: DayResult) -> None:
        with lock:
            detail = f" {r.error}" if r.error else ""
            print(f"{r.dataset} {r.day} {r.status} written={r.rows_written}{detail}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        per_dataset = pool.map(lambda m: ingest_dataset(state, m, args.start, args.end, on_day), manifests)
        results = [r for rs in per_dataset for r in rs]
    print(summarise([dataset_label(m) for m in manifests], results))
    return 0 if all(r.status == "ok" for r in results) else 1


@command(
    "catch-up",
    "Ingest the local days each dataset is missing since its history start (or --since) up to yesterday, from "
    "ops.ingest_run, through the same path as `ingest`. Reports blocked and incomplete days and the current-state "
    "sources that cannot be caught up as gaps (scanner.runtime.catchup). --plan only lists what is missing.",
    arg("--dataset", default="all", help="dataset name, asset name, or 'all' (default)"),
    arg("--since", type=date.fromisoformat, default=None, help="first local day, YYYY-MM-DD (default: history start)"),
    arg("--max-days", type=int, default=None, help="at most this many days per dataset (default: all)"),
    arg("--plan", action="store_true", help="print the plan and ingest nothing"),
)
def catch_up_cmd(args: argparse.Namespace) -> int:
    import psycopg

    from scanner.runtime import catchup
    from scanner.runtime.context import repo_state

    state = repo_state()
    try:
        manifests = select_manifests(state.manifests, args.dataset)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    with psycopg.connect(get_settings().database_url) as conn:
        conn.read_only = True
        plans = catchup.plan_catch_up(conn, state, manifests, args.since)
    print(catchup.format_plans(plans, catchup.unrecoverable(state)))
    if args.plan:
        return 0

    def on_day(r: DayResult) -> None:
        detail = f" {r.error}" if r.error else ""
        print(f"{r.dataset} {r.day} {r.status} written={r.rows_written}{detail}", flush=True)

    results = catchup.run_catch_up(state, plans, args.max_days, on_day)
    print(summarise([dataset_label(m) for m in manifests], results))
    return 0 if all(r.status == "ok" for r in results) else 1


@command(
    "snapshot",
    "Capture active snapshots from registry/snapshots.yaml (WP1.4).",
    arg("--id", default="all", help="snapshot id, or 'all' (default)"),
)
def snapshot_cmd(args: argparse.Namespace) -> int:
    from scanner.core.registry import load_snapshots
    from scanner.runtime.snapshots import run_snapshot

    active = [s for s in load_snapshots(get_settings().repo_root) if s.status == "active"]
    chosen = active if args.id == "all" else [s for s in active if s.id == args.id]
    if not chosen:
        print(f"no active snapshot {args.id!r}; active: {', '.join(s.id for s in active)}", file=sys.stderr)
        return 2
    failed = 0
    for s in chosen:
        out = run_snapshot(s)
        detail = f" error={out.error}" if out.error else ""
        detail += f" facts_written={out.facts_written}" if s.on_change.startswith("parse:") else ""
        detail += "".join(f" note={n}" for n in out.notes)
        print(f"{s.id}: status={out.status_code} changed={out.changed}{detail}", flush=True)
        failed += out.error is not None
    return 1 if failed else 0


@command(
    "capacity-map",
    "Load the shared Danish capacity map (stations, DSO areas, facts) into ref and core (WP1.5).",
)
def capacity_map_cmd(args: argparse.Namespace) -> int:
    import psycopg

    from scanner.runtime.capacity_map import load_capacity_map

    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        try:
            report = load_capacity_map(conn)
        except Exception as exc:
            print(f"capacity map run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    print(report.text())
    return 0


@command(
    "raw-uris",
    "migrate: rewrite absolute raw-capture references in the database (file://C:/..., file:///data/raw/...) to "
    "the store-relative raw://<source>/<dataset>/... form, resolved through RAW_STORE_URL. One-off and "
    "idempotent; a dry run unless --yes.",
    arg("action", choices=("migrate",)),
    arg("--yes", action="store_true", help="write the rewritten references (default: only report them)"),
    arg("--verify", action="store_true", help="also check that every reference resolves in RAW_STORE_URL"),
)
def raw_uris_cmd(args: argparse.Namespace) -> int:
    import psycopg

    from scanner.core.raw_store import RawStore
    from scanner.runtime.raw_uris import migrate

    s = get_settings()
    with psycopg.connect(s.database_url) as conn:
        conn.read_only = not args.yes
        reports = migrate(conn, apply=args.yes, verify=RawStore(s.raw_store_url) if args.verify else None)
        if args.yes:
            conn.commit()  # one transaction: every reference or none
        else:
            conn.rollback()
    for r in reports:
        print(r.text())
    total = sum(r.rewritten for r in reports)
    print(f"{total} reference(s) rewritten" if args.yes else f"{total} reference(s) to rewrite; add --yes to write")
    return 0


def mint_token(secret: str, days: int, now: datetime | None = None) -> str:
    """HS256 JWT {"role": "authenticated", "iat", "exp"} that PostgREST accepts for the raw-data views."""
    import base64
    import hashlib
    import hmac
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    issued = int((now or datetime.now(UTC)).timestamp())
    payload = {"role": "authenticated", "iat": issued, "exp": issued + days * 86400}
    signing_input = ".".join(
        b64(json.dumps(part, separators=(",", ":")).encode()) for part in ({"alg": "HS256", "typ": "JWT"}, payload)
    )
    return f"{signing_input}.{b64(hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest())}"


@command(
    "token",
    "Print a bearer token for the raw-data API views (role authenticated), signed with API_JWT_SECRET. "
    "Revoke every token by changing the secret and deploying.",
    arg("--days", type=int, default=14, help="days the token stays valid (default 14)"),
)
def token_cmd(args: argparse.Namespace) -> int:
    secret = get_settings().api_jwt_secret
    if secret is None or len(secret.get_secret_value()) < 32:
        print("API_JWT_SECRET is not set (or shorter than 32 characters); set it in the env file.", file=sys.stderr)
        return 1
    if args.days < 1:
        print("--days must be at least 1", file=sys.stderr)
        return 1
    print(mint_token(secret.get_secret_value(), args.days))
    return 0


@command(
    "backup",
    "pg_dump DATABASE_URL's database through the db container (docker exec) into BACKUP_DIR, check the dump "
    "with pg_restore --list, and keep the newest BACKUP_KEEP dumps (scanner.runtime.backup).",
    arg("--dir", type=Path, default=None, help="directory for the dumps (default BACKUP_DIR, .data/backups)"),
    arg("--keep", type=int, default=None, help="dumps of this database to keep (default BACKUP_KEEP, 7)"),
    arg("--container", default=None, help="the db container (default DB_CONTAINER, grid-scanner-db-1)"),
)
def backup_cmd(args: argparse.Namespace) -> int:
    from scanner.runtime.backup import BackupError, backup, database_and_user

    s = get_settings()
    try:
        database, user = database_and_user(s.database_url)
        result = backup(
            database=database,
            user=user,
            container=args.container or s.db_container,
            out_dir=args.dir or s.backup_dir,
            keep=s.backup_keep if args.keep is None else args.keep,
        )
    except BackupError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{result.path} ({result.size} bytes, {result.toc_entries} TOC entries)")
    for p in result.deleted:
        print(f"deleted {p}")
    return 0


@command(
    "restore",
    "Restore a dump written by `backup` into a NEW scratch database through the db container; refuses an "
    "existing database (scanner.runtime.backup). Connects as DATABASE_URL's user.",
    arg("dump", type=Path, help="the .dump file"),
    arg("--database", required=True, help="name of the scratch database to create, e.g. scanner_restore_check"),
    arg("--container", default=None, help="the db container (default DB_CONTAINER, grid-scanner-db-1)"),
)
def restore_cmd(args: argparse.Namespace) -> int:
    from scanner.runtime.backup import BackupError, database_and_user, restore

    s = get_settings()
    try:
        _, user = database_and_user(s.database_url)
        restore(args.dump, database=args.database, user=user, container=args.container or s.db_container)
    except BackupError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"restored {args.dump} into {args.database}")
    return 0


def export_all() -> list[str]:
    """Every serving view to exports/, then the methods and scenarios report."""
    from scanner.runtime.export import export_static
    from scanner.runtime.report import write_report

    return [*export_static(), *write_report()]


@command(
    "export",
    "Write every serving view to exports/ as JSON or GeoJSON (WP1.11), and the methods and scenarios report.",
)
def export_cmd(args: argparse.Namespace) -> int:
    for path in export_all():
        print(path)
    return 0


@command(
    "report",
    "Write exports/methods_and_scenarios.html and .md: assumption register, method cards, scenario comparison.",
)
def report_cmd(args: argparse.Namespace) -> int:
    from scanner.runtime.report import write_report

    for path in write_report():
        print(path)
    return 0


def _outcome_name(o: Any) -> str:
    """FeatureOutcome names its feature group; score and valuation outcomes have a name."""
    return str(getattr(o, "name", None) or o.feature)


def _print_outcomes(outcomes: Sequence[Any]) -> int:
    """Print feature, score or valuation outcomes (name, run_id, status, rows_written, notes); 1 if any was
    skipped."""
    for o in outcomes:
        print(f"{_outcome_name(o)}: run {o.run_id} {o.status}, {o.rows_written} rows", flush=True)
        for note in o.notes:
            print(f"  - {note}")
    return 1 if any(o.status == "skipped" for o in outcomes) else 0


def run_all_features(country: str, end_day: date | None) -> list[Any]:
    """Every feature group with a runner (F1, F2, F4, F5 and F7), one compute run each."""
    from scanner.features.run import RUNNERS, run_features

    return list(run_features(list(RUNNERS), country=country, end_day=end_day))


@command(
    "features",
    "Every feature group (F1, F2, F4, F5, F7) from stored series and capacity-map facts (WP1.7).",
    arg(
        "--country",
        default="dk",
        help="country whose local days and regime breaks the run uses and whose stations F7 reads (default dk); "
        "zone features cover every active and reference country's zones",
    ),
    arg("--end-day", type=date.fromisoformat, default=None, help="window ends at the start of this local day"),
)
def features_cmd(args: argparse.Namespace) -> int:
    return _print_outcomes(run_all_features(args.country, args.end_day))


SCENARIO_ARGS = (
    arg(
        "--scenario",
        dest="scenarios",
        action="append",
        default=None,
        metavar="ID",
        help="scenario ID from assumptions/scenarios/ (repeatable; default the official scenario)",
    ),
    arg("--all-scenarios", action="store_true", help="every scenario in assumptions/scenarios/, official first"),
)


def chosen_scenarios(args: argparse.Namespace) -> list[str | None]:
    """The scenarios a command runs, in order; None stands for the official scenario as loaded."""
    if args.all_scenarios and args.scenarios:
        raise ValueError("give --scenario or --all-scenarios, not both")
    if args.all_scenarios:
        from scanner.core.registry import load_scenarios

        found = load_scenarios(get_settings().repo_root).values()
        return [s.id for s in sorted(found, key=lambda s: (s.status != "official", s.id))]
    return list(args.scenarios) if args.scenarios else [None]


def run_scores(scenario: str | None = None) -> list[Any]:
    """Congestion scores, then opportunity types over that congestion run, for one scenario."""
    from scanner.scores.run import run_congestion_scores, run_opportunity_types

    allow = get_settings().allow_proposed_assumptions
    congestion = run_congestion_scores(scenario=scenario, allow_proposed=allow)
    return [
        congestion,
        run_opportunity_types(scenario=congestion.scenario_id, allow_proposed=allow, score_run_id=congestion.run_id),
    ]


def run_valuation(country: str, end_day: date | None, scenario: str | None = None) -> list[Any]:
    from scanner.valuation.run import run_bess_v0

    allow = get_settings().allow_proposed_assumptions
    return [run_bess_v0(scenario=scenario, country=country, end_day=end_day, allow_proposed=allow)]


def _per_scenario(args: argparse.Namespace, run: Callable[[str | None], list[Any]]) -> int:
    """Run one scenario after another; a scenario whose sets or values are missing ends the command with 1."""
    from scanner.core.registry import MissingAssumption
    from scanner.runtime.assumptions import AssumptionSetNotLoaded
    from scanner.runtime.scenarios import ScenarioNotLoaded

    try:
        scenarios = chosen_scenarios(args)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    outcomes: list[Any] = []
    for scenario in scenarios:
        try:
            outcomes += run(scenario)
        except (MissingAssumption, AssumptionSetNotLoaded, ScenarioNotLoaded) as exc:
            allowed = get_settings().allow_proposed_assumptions
            print(f"{scenario or 'official'}: {exc} (proposed values allowed: {allowed})", file=sys.stderr)
            return 1
    return _print_outcomes(outcomes)


@command(
    "scores",
    "Congestion scores (zones, municipalities) and opportunity types from the latest feature runs (WP1.8), "
    "per scenario.",
    *SCENARIO_ARGS,
)
def scores_cmd(args: argparse.Namespace) -> int:
    return _per_scenario(args, run_scores)


@command(
    "valuation",
    "BESS return range v0 per zone from stored day-ahead prices (WP1.9), per scenario.",
    arg("--country", default="dk", help="country whose zones and reference zones are valued (default dk)"),
    arg("--end-day", type=date.fromisoformat, default=None, help="window ends at the start of this local day"),
    *SCENARIO_ARGS,
)
def valuation_cmd(args: argparse.Namespace) -> int:
    return _per_scenario(args, lambda scenario: run_valuation(args.country, args.end_day, scenario))


Step = Callable[[], tuple[str, str]]  # runs one pipeline step; returns (status, detail)


@dataclass(frozen=True)
class StepResult:
    step: str
    status: str  # ok, partial (some outputs were skipped or partial), failed, or not run
    detail: str


def runs_step(fn: Callable[[], list[Any]]) -> Step:
    """A pipeline step over compute runs: failed when every run was skipped (nothing written), partial when
    any run was partial or skipped, else ok. The detail lists each run ID and status."""

    def step() -> tuple[str, str]:
        outcomes = fn()
        _print_outcomes(outcomes)
        statuses = [o.status for o in outcomes]
        status = "failed" if all(s == "skipped" for s in statuses) else "ok" if set(statuses) == {"ok"} else "partial"
        return status, ", ".join(f"{_outcome_name(o)} {o.run_id} {o.status}" for o in outcomes)

    return step


def run_pipeline(steps: Sequence[tuple[str, Step]]) -> list[StepResult]:
    """Run steps in order; after the first failed one (it raised or wrote nothing) the rest are not run."""
    results: list[StepResult] = []
    for name, step in steps:
        if results and results[-1].status in ("failed", "not run"):
            results.append(StepResult(name, "not run", ""))
            continue
        print(f"== {name}", flush=True)
        try:
            status, detail = step()
        except Exception as exc:  # a compute run that raised is already closed as failed
            status, detail = "failed", f"{type(exc).__name__}: {exc}"
        results.append(StepResult(name, status, detail))
    return results


def load_curated_step() -> tuple[str, str]:
    """Load the curated files (scenarios and sets included), so every run reads what the files say."""
    from scanner.core.curated import load

    s = get_settings()
    report = load(s.database_url, s.repo_root, s.restricted_curated_path)
    return "ok", f"{report.changes} file change(s)"


def _export_step() -> tuple[str, str]:
    return "ok", f"{len(export_all())} files in exports/"


def official_scenario_id() -> str:
    """The official scenario as the files declare it (the pipeline loads them first)."""
    from scanner.core.registry import load_scenarios

    found = [s.id for s in load_scenarios(get_settings().repo_root).values() if s.status == "official"]
    if len(found) != 1:
        raise ValueError(f"exactly one scenario must be official, found {found}")
    return found[0]


@command(
    "pipeline",
    "load-curated -> features (once) -> scores and valuation per scenario -> export and report, stopping at "
    "the first failed step, then a summary. Ingest, snapshots and the capacity map run separately on their own "
    "schedules.",
    arg("--country", default="dk", help="country for features and valuation (default dk)"),
    arg("--end-day", type=date.fromisoformat, default=None, help="window ends at the start of this local day"),
    *SCENARIO_ARGS,
)
def pipeline_cmd(args: argparse.Namespace) -> int:
    try:
        scenarios = [s or official_scenario_id() for s in chosen_scenarios(args)]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    steps: list[tuple[str, Step]] = [
        ("load-curated", load_curated_step),
        ("features", runs_step(lambda: run_all_features(args.country, args.end_day))),
    ]
    for scenario in scenarios:
        steps += [
            (f"scores:{scenario}", runs_step(lambda s=scenario: run_scores(s))),
            (f"valuation:{scenario}", runs_step(lambda s=scenario: run_valuation(args.country, args.end_day, s))),
        ]
    steps.append(("export", _export_step))
    results = run_pipeline(steps)
    width = max(len(r.step) for r in results)
    print(f"\n{'step':<{width}} {'status':<8} detail")
    for r in results:
        print(f"{r.step:<{width}} {r.status:<8} {r.detail}")
    return 0 if all(r.status in ("ok", "partial") for r in results) else 1


@command(
    "quarantine",
    "Review quarantined ingest runs (ADR 0013, scanner.runtime.quarantine). list: runs with deviations and "
    "corroboration evidence; decide: record accept (clears the unverified label) or reject (excludes the "
    "rows); accept-corroborated: accept undecided runs an independent source fully corroborates (needs --yes "
    "to write). A person's tool: never scheduled.",
    arg("action", choices=("list", "decide", "accept-corroborated")),
    arg("--dataset", default=None, help="list, accept-corroborated: only this dataset, e.g. DayAheadPrices"),
    arg("--run-id", default=None, help="decide: the ingest run ID"),
    arg("--decision", choices=("accept", "reject"), default=None, help="decide: accept or reject"),
    arg("--by", default="", help="decide, accept-corroborated: name of the person deciding"),
    arg("--reason", default="", help="decide, accept-corroborated: why"),
    arg("--yes", action="store_true", help="accept-corroborated: write the decisions (default: only print them)"),
)
def quarantine_cmd(args: argparse.Namespace) -> int:
    import uuid

    import psycopg

    from scanner.runtime import quarantine as q

    try:  # argument errors are reported before connecting
        if args.action == "decide":
            if args.run_id is None or args.decision is None:
                raise ValueError("decide needs --run-id and --decision")
            run_id = uuid.UUID(args.run_id)
            q.validate_decision(args.decision, args.by, args.reason)
        elif args.action == "accept-corroborated":
            q.validate_decision("accept", args.by, args.reason)
        with psycopg.connect(get_settings().database_url) as conn:
            if args.action == "list":
                conn.read_only = True
                runs = q.quarantined_runs(conn, args.dataset)
                print(q.format_runs(runs))
                print(q.format_summary(runs))
                return 0
            if args.action == "decide":
                decision_id = q.record_decision(conn, run_id, args.decision, args.by, args.reason)
                print(f"decision {decision_id}: {args.decision} run {run_id}")
                return 0
            conn.read_only = not args.yes
            plan, ids = q.accept_corroborated(
                conn, decided_by=args.by, reason=args.reason, dataset=args.dataset, yes=args.yes
            )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    verb = "accepted" if args.yes else "would accept"
    for r in plan:
        print(f"{verb} {r.label} {r.partition_key} run {r.run_id} ({r.rows} rows)")
    print(f"{len(ids)} decision(s) written" if args.yes else f"{len(plan)} run(s); add --yes to write")
    return 0


@command(
    "prune",
    "Delete old feature runs (feat.feature_value rows and their ops.compute_run rows), keeping the newest N per "
    "feature and every run that a served score or an approved scenario's output references "
    "(scanner.runtime.prune). A dry run unless --yes.",
    arg("--keep", type=int, required=True, help="newest ok or partial runs to keep per feature (at least 1)"),
    arg(
        "--protect",
        choices=("served", "all"),
        default="served",
        help="served: the latest run per scenario and every run of approved scenarios (default); "
        "all: any score, opportunity or valuation row",
    ),
    arg("--yes", action="store_true", help="delete (default: only print what would be deleted)"),
)
def prune_cmd(args: argparse.Namespace) -> int:
    from scanner.runtime.prune import prune_feature_runs

    try:
        print(prune_feature_runs(get_settings().database_url, args.keep, protect=args.protect, yes=args.yes))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not args.yes:
        print("dry run; add --yes to delete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
