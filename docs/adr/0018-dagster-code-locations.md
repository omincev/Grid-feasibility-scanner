# ADR 0018: Ingestion and analytics are separate Dagster code locations; assumptions are lineage nodes

Status: accepted, 2026-10-18, owner decision.

## Context

Fetching facts and applying judgement to them change at different rates and should fail independently: a broken scoring change must never stop ingestion, and analysts need a visual overview of what is computed from what, with which assumptions.

## Decision

Dagster loads code locations from `workspace.yaml`: `ingestion` (`scanner.ingestion_definitions`: ingest, snapshots, capacity map, curated loader, their schedules) and `analytics` (`scanner.analytics_definitions`: features, scores, valuation, serving export, the daily compute job). Further locations may hold operational jobs such as alerts. Analytics declares the canonical assets it reads with the same asset keys, so lineage stays connected. Every assumption set, scenario file and method card is an observable asset whose data version is the file's content hash, so a changed file marks its downstream outputs stale; scores and valuation are partitioned by scenario. Each ingest asset shows its normalisation rules and the source dossier's Decisions section as metadata. The analytics location can run as `scanner_analytics`, which cannot write canonical data.

## Consequences

Two (or more) code servers to run. Recomputation after an assumption change happens in the daily job or on demand, not on every edit. The Dagster UI is an engineering view; experts read the serving views and the generated report (ADR 0017).
