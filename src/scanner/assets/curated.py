"""Curated files from git into the ref and core tables (WP0.4, ADR 0006). Run after every merge."""

from dagster import AssetExecutionContext, AssetKey, MaterializeResult, asset

from scanner.core.curated import load
from scanner.settings import get_settings


@asset(
    key=AssetKey(["curated", "files"]),
    group_name="curated",
    kinds={"python", "postgres"},
    description="Validates and loads registry, codebook, market rules, assumptions, benchmarks, geometries "
    "and claims (scanner.core.curated). Unchanged files are skipped.",
)
def curated_files(context: AssetExecutionContext) -> MaterializeResult:
    s = get_settings()
    report = load(s.database_url, s.repo_root, s.restricted_curated_path)
    context.log.info(report.text())
    return MaterializeResult(metadata={"file_changes": report.changes, "report": report.text()})


CURATED_ASSETS = [curated_files]
