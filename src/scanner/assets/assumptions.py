"""Assumption files in the lineage (analytics location, scanner.analytics_definitions).

One observable source asset per assumption set (`assumptions/<set_id>`) and per method card
(`methods/<method_id>`), and one asset `assumptions/scenarios` partitioned by scenario, one partition per
assumptions/scenarios/<id>.yaml. The data version of each is the SHA-256 of its file: the content hash
load-curated stores in ref.assumption_set, ref.scenario and ref.method_card and that every score, opportunity
and valuation row records (scanner.runtime.scenarios.OutputColumns). Scores, opportunities and valuation are
partitioned by the same `scenario` partitions, so a changed scenario file shows only its own partition as
stale; a changed set or card shows every asset that uses it as stale.

How a change reaches the outputs (deliberately simple):

- `sensor_assumption_files` (every minute, running by default everywhere: it only reads files) hashes the files.
  When a hash changed it launches `job_observe_assumptions`, which records the new data versions, so the
  assets that use the file show as stale ("Unsynced") within about a minute. It also adds and deletes
  `scenario` partitions to match the scenario files.
- Nothing recomputes on its own. The daily `job_compute` (scanner.assets.compute) loads the curated files
  first (`curated/analytics_load`, as `scanner.cli pipeline` does) and recomputes every scenario; for an
  earlier result, launch it from the UI. Why: assumption files change through reviewed merges, a few times a
  week; the containers mount the working tree, so recomputing on every change would also fire on edits in
  progress; and only the full job guarantees the loader runs before any score. A partition materialised on its
  own refuses to run while ref holds an older copy of its scenario or sets than the files (stale_copies), so the
  lineage never claims a result used a file version it did not use.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import psycopg
import yaml
from dagster import (
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    BackfillPolicy,
    DataVersion,
    DataVersionsByPartition,
    DefaultSensorStatus,
    DynamicPartitionsDefinition,
    MaterializeResult,
    MetadataValue,
    ObserveResult,
    OpExecutionContext,
    RunRequest,
    SensorEvaluationContext,
    SensorResult,
    SourceAsset,
    asset,
    define_asset_job,
    in_process_executor,
    observable_source_asset,
    sensor,
)

from scanner import REPO_ROOT
from scanner.assets.keys import FACT_WRITERS, ingest_key, ingested
from scanner.core.curated import load
from scanner.core.methods import MethodCard, MethodRef, card_paths, parse_card
from scanner.core.registry import SCENARIOS_DIR, AssumptionSet, Scenario, assumption_paths
from scanner.features.registry import InputSpec
from scanner.runtime.assumptions import AssumptionSetNotLoaded, load_set
from scanner.runtime.context import RepoState
from scanner.runtime.scenarios import ScenarioNotLoaded, load_scenario
from scanner.settings import get_settings

SCENARIO_PARTITIONS = DynamicPartitionsDefinition(name="scenario")
SCENARIOS_KEY = AssetKey(["assumptions", "scenarios"])
CURATED_LOAD_KEY = AssetKey(["curated", "analytics_load"])
GROUP = "assumptions"
METHODS_GROUP = "methods"
HASH_NOTE = "Data version: SHA-256 of the file, the content hash load-curated stores in ref and output rows record."


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def set_key(set_id: str) -> AssetKey:
    return AssetKey(["assumptions", set_id])


def method_key(method_id: str) -> AssetKey:
    return AssetKey(["methods", method_id])


def set_paths(root: Path) -> dict[str, Path]:
    return {p.stem: p for p in assumption_paths(root)}


def scenario_paths(root: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted((root / "assumptions" / SCENARIOS_DIR).glob("*.yaml"))}


def method_paths(root: Path) -> dict[str, Path]:
    return {p.stem: p for p in card_paths(root)}


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _parse(path: Path, build: Any) -> tuple[Any, str | None]:
    """(model, None), or (None, error) when the file does not parse: an observation must not fail on a file
    that is being edited; load-curated and the contract checks reject it."""
    try:
        text = path.read_text(encoding="utf-8")
        return build(text), None
    except Exception as exc:  # any parse or validation error is reported as metadata
        return None, f"{type(exc).__name__}: {exc}"


def _set_metadata(root: Path, path: Path, a: AssumptionSet | None, error: str | None) -> dict[str, Any]:
    if a is None:
        return {"path": MetadataValue.path(_rel(root, path)), "parse_error": error or ""}
    without_value = sorted(k for k, p in a.parameters.items() if p.value is None)
    return {
        "path": MetadataValue.path(_rel(root, path)),
        "kind": a.kind,
        "status": a.status,
        "owners": ", ".join(a.owners),
        "approved_by": a.approved_by or "not approved",
        "approved_on": a.approved_on.isoformat() if a.approved_on else "",
        "sensitivity": a.sensitivity,
        "parameters": len(a.parameters),
        "without_approved_value": ", ".join(without_value) or "none",
    }


def _card_metadata(root: Path, path: Path, c: MethodCard | None, error: str | None) -> dict[str, Any]:
    if c is None:
        return {"path": MetadataValue.path(_rel(root, path)), "parse_error": error or ""}
    return {
        "path": MetadataValue.path(_rel(root, path)),
        "title": c.title,
        "version": c.version,
        "status": c.status,
        "owners": ", ".join(c.owner),
        "parameters": ", ".join(c.parameters) or "none",
        "inputs": ", ".join(c.inputs),
    }


def _load_set(text: str) -> AssumptionSet:
    return AssumptionSet.model_validate(yaml.safe_load(text))


def _load_scenario(text: str) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(text))


def _set_asset(root: Path, set_id: str, path: Path) -> SourceAsset:
    a, error = _parse(path, _load_set)

    @observable_source_asset(
        key=set_key(set_id),
        group_name=GROUP,
        description=f"Assumption set `{_rel(root, path)}` (assumptions/README.md). {HASH_NOTE}",
        metadata=_set_metadata(root, path, a, error),
    )
    def _observe() -> ObserveResult:
        return ObserveResult(
            data_version=DataVersion(file_hash(path)), metadata=_set_metadata(root, path, *_parse(path, _load_set))
        )

    return _observe


def _card_asset(root: Path, method_id: str, path: Path) -> SourceAsset:
    c, error = _parse(path, parse_card)

    @observable_source_asset(
        key=method_key(method_id),
        group_name=METHODS_GROUP,
        description=f"Method card `{_rel(root, path)}` (docs/methods/README.md): how the outputs of method "
        f"{method_id} are computed; raise its version when the calculation changes. {HASH_NOTE}",
        metadata=_card_metadata(root, path, c, error),
    )
    def _observe() -> ObserveResult:
        return ObserveResult(
            data_version=DataVersion(file_hash(path)), metadata=_card_metadata(root, path, *_parse(path, parse_card))
        )

    return _observe


def _scenario_table(root: Path) -> str:
    lines = [
        "| Scenario | Status | Owners | Approved by | Approved on | Sets | Path |",
        "|---|---|---|---|---|---|---|",
    ]
    for sid, path in scenario_paths(root).items():
        s, error = _parse(path, _load_scenario)
        if s is None:
            lines.append(f"| {sid} | parse error: {error} | | | | | {_rel(root, path)} |")
            continue
        sets = ", ".join(f"{role}={set_id}" for role, set_id in s.sets.items())
        approved_on = s.approved_on.isoformat() if s.approved_on else ""
        cells = (
            sid,
            s.status,
            ", ".join(s.owners),
            s.approved_by or "not approved",
            approved_on,
            sets,
            _rel(root, path),
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _scenarios_asset(root: Path) -> SourceAsset:
    @observable_source_asset(
        key=SCENARIOS_KEY,
        partitions_def=SCENARIO_PARTITIONS,
        group_name=GROUP,
        description="Scenario files `assumptions/scenarios/<id>.yaml` (assumptions/scenarios/README.md), one "
        f"partition per file. {HASH_NOTE} The table in the metadata is read when the code location loads.",
        metadata={
            "path": MetadataValue.path(f"assumptions/{SCENARIOS_DIR}/"),
            "scenarios": MetadataValue.md(_scenario_table(root)),
        },
    )
    def _observe(context: OpExecutionContext) -> DataVersionsByPartition:
        partitions = set(context.instance.get_dynamic_partitions(SCENARIO_PARTITIONS.name))
        return DataVersionsByPartition(
            {sid: DataVersion(file_hash(p)) for sid, p in scenario_paths(root).items() if sid in partitions}
        )

    return _observe


def assumption_assets(root: Path = REPO_ROOT) -> list[SourceAsset]:
    """Every assumption set, the scenario files and every method card, as observable source assets."""
    return [
        *(_set_asset(root, sid, p) for sid, p in set_paths(root).items()),
        _scenarios_asset(root),
        *(_card_asset(root, mid, p) for mid, p in method_paths(root).items()),
    ]


def current_hashes(root: Path = REPO_ROOT) -> dict[str, str]:
    """{file relative to the repository: SHA-256} of every file the observable assets watch."""
    files = [*set_paths(root).values(), *scenario_paths(root).values(), *method_paths(root).values()]
    return {_rel(root, p): file_hash(p) for p in files}


# --- what each analytics asset depends on ---------------------------------------------------------------


def input_keys(state: RepoState, specs: Iterable[InputSpec]) -> list[AssetKey]:
    """The upstream assets whose data these inputs read: the ingest asset of every manifest carrying an entry
    the input accepts (its listed entries, else every entry of its class), and the fact writer of entries no
    manifest carries (the capacity map)."""
    keys: dict[AssetKey, None] = {}
    for spec in specs:
        entries = set(spec.entries) or {
            eid for eid, (_, e) in state.entries.items() if e.equivalence_class == spec.equivalence_class
        }
        for m in state.manifests:
            if ingested(m) and entries & set(m.entries):
                keys[ingest_key(m)] = None
        for eid in sorted(entries):
            if eid in state.entries and (writer := FACT_WRITERS.get(state.entries[eid][0].source)):
                keys[writer] = None
    return list(keys)


def roles(*methods: MethodRef) -> list[str]:
    """The scenario roles whose sets the methods read (their `<role>.<parameter>` names)."""
    return sorted({p.split(".", 1)[0] for m in methods for p in m.parameters})


def set_keys(scenarios: Iterable[Scenario], *methods: MethodRef) -> list[AssetKey]:
    """The sets any scenario names for the roles the methods read."""
    wanted = roles(*methods)
    return [set_key(s) for s in sorted({sc.sets[r] for sc in scenarios for r in wanted if r in sc.sets})]


def method_keys(*methods: MethodRef) -> list[AssetKey]:
    return [method_key(m.id) for m in methods]


# --- the loaded copies must be the files' versions ----------------------------------------------------------


def stale_copies(conn: psycopg.Connection[Any], root: Path, scenario_id: str, used_roles: Sequence[str]) -> list[str]:
    """Files whose copy in ref differs from the file: the scenario's own file and the sets it names for
    `used_roles`. Empty when ref matches the files (load-curated ran after the last change). Sets that are not
    in this repository (restricted curated path) are not checked."""
    path = scenario_paths(root).get(scenario_id)
    rel = f"assumptions/{SCENARIOS_DIR}/{scenario_id}.yaml"
    if path is None:
        return [f"{rel} (no such file)"]
    try:
        sc = load_scenario(conn, scenario_id)
    except ScenarioNotLoaded:
        return [f"{rel} (not loaded)"]
    out = [] if sc.content_hash == file_hash(path) else [rel]
    sets = set_paths(root)
    for role in used_roles:
        set_id = sc.set_id(role)
        if set_id not in sets:
            continue
        try:
            digest = load_set(conn, set_id)[2]
        except AssumptionSetNotLoaded:
            digest = None
        if digest != file_hash(sets[set_id]):
            out.append(_rel(root, sets[set_id]))
    return out


# --- the curated load, the observation job and the sensor -----------------------------------------------


# Curated folders (scanner.core.curated.FOLDERS) whose files have their own observable assets here.
OBSERVED_FOLDERS = ("assumptions", "scenarios", "methods")


def _loaded_version(database_url: str) -> str:
    """Hash of the other curated files (registry, codebook, market rules, geometries, ...) as ref.curated_file
    records them. It changes only when the load changed one of those, so the assets downstream of the load show
    as stale only then; a changed assumption file marks only the assets that use it, through its own asset."""
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "select source_path, content_hash from ref.curated_file where folder <> all(%s) order by source_path",
            (list(OBSERVED_FOLDERS),),
        ).fetchall()
    return hashlib.sha256("\n".join(f"{p} {h}" for p, h in rows).encode()).hexdigest()


def curated_load_asset(deps: Sequence[AssetKey]) -> Any:
    @asset(
        key=CURATED_LOAD_KEY,
        deps=list(deps),
        group_name=GROUP,
        kinds={"python", "postgres"},
        backfill_policy=BackfillPolicy.single_run(),
        description="First step of job_compute, as of `scanner.cli pipeline`: the curated loader "
        "(scanner.core.curated.load) writes the assumption sets, scenarios and method cards, and every other "
        "curated file, to ref, so every analytics run reads what the files say. The same idempotent load as "
        "`curated/files` in the ingestion location: unchanged files are skipped. Connects with DATABASE_URL "
        "(it writes ref as scanner_curator), not ANALYTICS_DATABASE_URL. Data version: the hash of the loaded "
        "curated files other than assumption sets, scenarios and method cards, which have their own assets.",
    )
    def analytics_load(context: AssetExecutionContext) -> MaterializeResult:
        s = get_settings()
        report = load(s.database_url, s.repo_root, s.restricted_curated_path)
        context.log.info(report.text())
        return MaterializeResult(
            data_version=DataVersion(_loaded_version(s.database_url)),
            metadata={"file_changes": report.changes, "report": report.text()},
        )

    return analytics_load


def observe_job(observables: Sequence[SourceAsset]) -> Any:
    return define_asset_job(
        "job_observe_assumptions",
        selection=AssetSelection.assets(*(a.key for a in observables)),
        executor_def=in_process_executor,
        description="Records the content hash of every assumption set, scenario file and method card.",
    )


def build_sensor(job: Any, root: Path = REPO_ROOT) -> Any:
    @sensor(
        name="sensor_assumption_files",
        job=job,
        minimum_interval_seconds=60,
        default_status=DefaultSensorStatus.RUNNING,
        description="Keeps the `scenario` partitions equal to assumptions/scenarios/*.yaml and observes the "
        "assumption files when any of them changed (scanner.assets.assumptions).",
    )
    def sensor_assumption_files(context: SensorEvaluationContext) -> SensorResult:
        ids = list(scenario_paths(root))
        existing = context.instance.get_dynamic_partitions(SCENARIO_PARTITIONS.name)
        requests = []
        if added := [i for i in ids if i not in existing]:
            requests.append(SCENARIO_PARTITIONS.build_add_request(added))
        if removed := [i for i in existing if i not in ids]:
            requests.append(SCENARIO_PARTITIONS.build_delete_request(removed))
        digest = hashlib.sha256(json.dumps(current_hashes(root), sort_keys=True).encode()).hexdigest()
        changed = digest != context.cursor or bool(added)
        return SensorResult(
            run_requests=[RunRequest()] if changed else [],
            dynamic_partitions_requests=requests,
            cursor=digest,
        )

    return sensor_assumption_files
