"""The normalisation rules of each ingest asset, as its Dagster description and metadata.

Read from the files that define the behaviour, so the UI cannot drift from the code: the manifest (UTC time
field, resolution, lookback and lookahead, fingerprint thresholds), the codebook entries (unit, sign convention,
resampling rule), the country files (source precedence per class) and the source dossier (its path and its
"Decisions" section, docs/sources/_TEMPLATE.md).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dagster import MetadataValue

from scanner.core.codebook import CodebookEntry, CodebookFile, IssuedAtField, IssuedAtFixedLocalTime
from scanner.core.manifest import Manifest
from scanner.core.registry import Country
from scanner.runtime.context import RepoState

DECISIONS = "Decisions"


def dossier_path(root: Path, source: str) -> Path | None:
    """docs/sources/<cc>/<source>.md, found as the curated loader finds it for ref.source."""
    return next(iter(sorted((root / "docs" / "sources").glob(f"*/{source}.md"))), None)


def section(markdown: str, title: str) -> str | None:
    """The body of the level-2 section `## <title>` up to the next level-2 heading; None if there is none."""
    m = re.search(rf"^## {re.escape(title)}[ \t]*\r?\n(.*?)(?=^## |\Z)", markdown, re.MULTILINE | re.DOTALL)
    return m[1].strip() if m else None


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _cell(value: object) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _issued_at(e: CodebookEntry) -> str:
    rule = e.issued_at
    if rule is None:
        return ""
    if isinstance(rule, IssuedAtField):
        return f"field {rule.field}"
    if isinstance(rule, IssuedAtFixedLocalTime):
        return f"{rule.local_time:%H:%M} local, day {rule.day_offset:+d}"
    return f"interval start + {rule.offset}"


def entries_table(entries: Iterable[tuple[CodebookFile, CodebookEntry]]) -> str:
    """Markdown table of codebook entries: field, class, unit, sign convention, resampling rule, issue time."""
    lines = [
        "| Entry | Field | Dimensions | Class | Kind | Unit | Sign convention | Resample | Issued at |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for _, e in entries:
        dims = ", ".join(f"{k}={v}" for k, v in e.dimensions.items())
        cells = (e.id, e.field, dims, e.equivalence_class, e.kind, e.unit, e.sign_convention, e.resample, _issued_at(e))
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(lines)


def precedence(countries: dict[str, Country], classes: Iterable[str]) -> dict[str, dict[str, list[str]]]:
    """Per equivalence class, the source ranking of each country file that has one for it (first preferred)."""
    return {
        cls: {cc: list(c.precedence[cls]) for cc, c in sorted(countries.items()) if cls in c.precedence}
        for cls in sorted(set(classes))
    }


def _precedence_md(ranks: dict[str, dict[str, list[str]]], source: str) -> str:
    def ranking(sources: list[str]) -> str:
        return " > ".join(f"**{s}**" if s == source else s for s in sources)

    lines = [
        f"- `{cls}`: " + "; ".join(f"{cc}: {ranking(r)}" for cc, r in by_country.items())
        for cls, by_country in ranks.items()
        if by_country
    ]
    unranked = [f"`{cls}`" for cls, by_country in ranks.items() if not by_country]
    if unranked:
        lines.append(
            f"- No country file ranks sources for {', '.join(unranked)}: a feature reading one of these refuses to "
            "mix two sources for one zone (scanner.features.inputs.select_source)."
        )
    return "\n".join(lines)


def fingerprint_text(m: Manifest) -> str:
    f = m.fingerprint
    return (
        "Hard deviations block the write and file a bead: a non-2xx status, zero rows where rows are expected, a "
        "field added, removed or retyped. Soft deviations are written, quarantined and labelled unverified: a row "
        f"count other than expected; after {f.warmup_runs} runs, a null rate above the highest seen plus "
        f"{f.null_rate_margin}, or values outside the range seen widened by {f.iqr_multiplier} x IQR "
        "(scanner.core.fingerprint, ADR 0013)."
    )


def _resolution_text(m: Manifest) -> str:
    if m.per_period_resolution:
        return "per period: each source period keeps its own resolution, one series per resolution (ADR 0002)"
    return f"{m.resolution}, stored at native resolution (ADR 0002)"


def _dossier(root: Path, source: str) -> tuple[str | None, str | None]:
    """(relative path, Decisions section) of the source's dossier."""
    path = dossier_path(root, source)
    if path is None:
        return None, None
    return _rel(root, path), section(path.read_text(encoding="utf-8"), DECISIONS)


def ingest_rules(state: RepoState, m: Manifest) -> tuple[str, dict[str, Any]]:
    """(description, metadata) of the ingest asset of one manifest."""
    entries = [state.entries[e] for e in m.entries]
    ranks = precedence(state.countries, (e.equivalence_class for _, e in entries))
    dossier, decisions = _dossier(state.root, m.source)
    package = m.adapter.split(":")[0].split(".")[-1]
    manifest = state.root / "src" / "scanner" / "adapters" / package / "manifests" / f"{m.dataset}.yaml"
    codebook = sorted({f"codebook/{f.country}/{f.source}.{f.dataset}.yaml" for f, _ in entries})
    rules = [
        f"- Time: `{m.time_field_utc}` is the interval start in UTC; intervals are half-open "
        "[start, start + resolution) (ADR 0001).",
        f"- Resolution: {_resolution_text(m)}.",
        f"- Window: each run re-fetches from the partition day minus the lookback {m.lookback} (revisions) and "
        f"writes up to the lookahead {m.lookahead} past it (data published ahead of its interval); the "
        "fingerprint covers the partition day only."
        + (f" Frequent ticks fetch only {m.recent_lookback} before the tick." if m.recent_lookback else ""),
        f"- Fingerprint: {fingerprint_text(m)}",
        "- Units and signs are stored as published; the resampling rule applies only where features aggregate.",
    ]
    description = "\n\n".join(
        [
            f"{m.source} {m.dataset} -> core.observation, one partition per local day. Manifest status: {m.status}.",
            "**Normalisation** (manifest `"
            + (_rel(state.root, manifest) if manifest.exists() else m.dataset)
            + "`, codebook "
            + ", ".join(f"`{c}`" for c in codebook)
            + ")\n\n"
            + "\n".join(rules),
            entries_table(entries),
            "**Source precedence** (country files, `precedence`)\n\n" + _precedence_md(ranks, m.source),
            f"**Decisions** (dossier `{dossier}`)\n\n{decisions}"
            if decisions
            else f"Dossier: `{dossier}` (no Decisions section)",
        ]
    )
    metadata: dict[str, Any] = {
        "source": m.source,
        "dataset": m.dataset,
        "licence": m.licence,
        "time_field_utc": m.time_field_utc,
        "resolution": m.resolution,
        "lookback": m.lookback,
        "lookahead": m.lookahead,
        "entries": MetadataValue.md(entries_table(entries)),
        "precedence": MetadataValue.json(ranks),
        "fingerprint": MetadataValue.json(m.fingerprint.model_dump()),
        "codebook": ", ".join(codebook),
        "dossier": MetadataValue.path(dossier) if dossier else "none",
        "decisions": MetadataValue.md(decisions) if decisions else "none recorded in the dossier",
    }
    if manifest.exists():
        metadata["manifest"] = MetadataValue.path(_rel(state.root, manifest))
    return description, metadata


def source_rules(state: RepoState, source: str, dossiers: Iterable[str]) -> tuple[str, dict[str, Any]]:
    """(rules section of a description, metadata) for an asset that writes one codebook source's entries as
    facts rather than from a manifest (the capacity map). `dossiers` are paths relative to the repository."""
    entries = sorted((v for v in state.entries.values() if v[0].source == source), key=lambda v: v[1].id)
    parts = [entries_table(entries)]
    metadata: dict[str, Any] = {"entries": MetadataValue.md(entries_table(entries))}
    for i, rel in enumerate(dossiers):
        decisions = section((state.root / rel).read_text(encoding="utf-8"), DECISIONS)
        parts.append(f"**Decisions** (dossier `{rel}`)\n\n{decisions or 'none recorded'}")
        suffix = "" if i == 0 else f"_{i + 1}"
        metadata[f"dossier{suffix}"] = MetadataValue.path(rel)
        metadata[f"decisions{suffix}"] = MetadataValue.md(decisions) if decisions else "none recorded in the dossier"
    return "\n\n".join(parts), metadata
