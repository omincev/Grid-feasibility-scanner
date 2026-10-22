"""One ingest run for one manifest and one local-day partition (docs/architecture.md).

Sequence: open run, fetch (partition day plus lookback before and lookahead after), store raw,
fingerprint the partition day, classify deviations, parse, write changed rows, close run. Hard
deviations block the write and enqueue a bead (ADR 0013): a final non-2xx status, a schema change,
zero rows when rows are expected, a parse error, or a fetch that fails without any response. Soft
deviations mark the rows of the fingerprinted partition day quarantined, not the lookback or lookahead
rows (docs/quality.md layer 2). Nothing here calls an LLM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from scanner.adapters import load_adapter
from scanner.adapters.base import ParseError, SeriesInfo
from scanner.core import db
from scanner.core.fingerprint import Baseline, Deviation, Thresholds, compare, compute, severity
from scanner.core.manifest import Manifest
from scanner.core.raw_store import RawStore
from scanner.core.time import expected_count, local_day, local_day_bounds, parse_source_utc
from scanner.runtime.context import RepoState
from scanner.settings import get_settings


@dataclass
class IngestOutcome:
    run_id: str
    status: str
    # rows_received and rows_expected both count canonical rows in the partition day only, so they
    # compare directly; rows_written counts what was written over the whole fetch window.
    rows_received: int = 0
    rows_expected: int | None = None
    rows_written: int = 0
    severity: str = "none"
    deviations: list[str] = field(default_factory=list)
    raw_uris: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IngestWindows:
    """UTC bounds of one run: [fetch_start, fetch_end) is fetched, parsed and written;
    [day_start, day_end) is the partition day that the fingerprint and row counts cover."""

    fetch_start: datetime
    day_start: datetime
    day_end: datetime
    fetch_end: datetime


def ingest_windows(manifest: Manifest, tz: str, day: date, recent_at: datetime | None = None) -> IngestWindows:
    """Window of one run. With `recent_at` (the tick time of a frequent schedule) and a manifest
    `recent_lookback`, the fetch starts that long before the tick, floored to the hour, instead of at the
    partition day minus the full lookback. The fingerprint still covers the partition day's records fetched."""
    day_start, day_end = local_day_bounds(day, tz)
    start = day_start - manifest.lookback_td
    if recent_at is not None and manifest.recent_lookback_td is not None:
        start = recent_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0) - manifest.recent_lookback_td
        start = min(start, day_end)
    return IngestWindows(
        fetch_start=start,
        day_start=day_start,
        day_end=day_end,
        fetch_end=day_end + manifest.lookahead_td,
    )


def expected_partition_rows(manifest: Manifest, tz: str, day: date, entries_per_zone: int) -> int | None:
    """Rows a complete local day should contain, or None if not countable (coarser than hourly, or a
    resolution that varies per source period)."""
    if manifest.per_period_resolution or manifest.resolution_td > timedelta(hours=1):
        return None
    if day < manifest.history_start_local:
        return 0
    if manifest.history_end_local is not None and day > manifest.history_end_local:
        return 0
    return expected_count(day, tz, manifest.resolution_td) * len(manifest.zones) * entries_per_zone


def expected_series_rows(
    rows: list[db.ObservationRow], series: dict[str, SeriesInfo], tz: str, day: date, w: IngestWindows
) -> int | None:
    """For per-period manifests: what the series present on the partition day should hold, each at its
    own resolution. A zone or series missing altogether is not counted (the zero-row check covers that)."""
    keys = {r.series_key for r in rows if w.day_start <= r.interval_start < w.day_end}
    resolutions = [series[k].resolution for k in keys]
    if any(res is None or res > timedelta(hours=1) for res in resolutions):
        return None
    return sum(expected_count(day, tz, res) for res in resolutions if res is not None)


def partition_records(
    manifest: Manifest, records: list[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Records whose UTC time falls in [start, end) and whose zone the manifest covers."""
    out = []
    for rec in records:
        raw_t = rec.get(manifest.time_field_utc)
        if raw_t is None:
            out.append(rec)  # keep: the fingerprint must see malformed records
            continue
        t = parse_source_utc(str(raw_t))
        if not start <= t < end:
            continue
        if manifest.zone_field and rec.get(manifest.zone_field) not in manifest.zones:
            continue
        out.append(rec)
    return out


def quarantine_partition_day(rows: list[db.ObservationRow], w: IngestWindows) -> list[db.ObservationRow]:
    """The rows of a run with a soft deviation: those whose interval_start lies in the fingerprinted
    partition day [day_start, day_end) are quarantined; lookback and lookahead rows stay as parsed (ok),
    because the fingerprint did not look at them (docs/quality.md layer 2)."""
    return [replace(r, quality="quarantined") if w.day_start <= r.interval_start < w.day_end else r for r in rows]


def _bead(conn: Any, manifest: Manifest, day: date, what: str, body: str, raw_uris: list[str]) -> None:
    db.enqueue_bead(
        conn,
        kind="adapter-fix",
        title=f"{manifest.source}/{manifest.dataset}: {what} on {day.isoformat()}",
        body=body[:2000],
        raw_uri=raw_uris[0] if raw_uris else None,
        source=manifest.source,
        dataset=manifest.dataset,
    )


def run_ingest(state: RepoState, manifest: Manifest, day: date, recent_at: datetime | None = None) -> IngestOutcome:
    """Ingest one partition day. `recent_at`: see ingest_windows; None is the full-lookback run."""
    settings = get_settings()
    country = state.countries[manifest.country]
    entries = state.entries_for(manifest)
    adapter = load_adapter(manifest.adapter)
    store = RawStore(settings.raw_store_url)

    w = ingest_windows(manifest, country.timezone, day, recent_at)
    conn = db.connect(settings.database_url)
    db.lock_dataset(conn, manifest.source, manifest.dataset)  # released by conn.close() below
    run_id = db.start_run(
        conn,
        source=manifest.source,
        dataset=manifest.dataset,
        partition_key=day.isoformat(),
        window_start=w.fetch_start,
        window_end=w.fetch_end,
        code_version=os.environ.get("GIT_SHA"),
    )
    outcome = IngestOutcome(run_id=str(run_id), status="running")
    # Rows per interval per zone: each codebook entry yields one series per zone (dimensions included).
    rows_per_zone = len(entries)
    today = local_day(datetime.now(UTC), country.timezone)
    stage = "fetch"
    try:
        results = adapter.fetch(manifest, w.fetch_start, w.fetch_end)
        stage = "process"
        records: list[dict[str, Any]] = []
        for seq, r in enumerate(results):
            ref = store.put(
                source=manifest.source,
                dataset=manifest.dataset,
                run_id=str(run_id),
                seq=seq,
                content=r.content,
                ext=r.ext,
                fetched_at=r.fetched_at,
                meta={"url": r.url, "params": r.params, "status": r.status, "headers": r.headers},
            )
            outcome.raw_uris.append(ref.uri)
            if 200 <= r.status < 300:
                records.extend(adapter.records(r))

        # Fingerprint the partition day. Today's partition is incomplete by nature: no count check.
        day_records = partition_records(manifest, records, w.day_start, w.day_end)
        expected_records = (
            expected_partition_rows(manifest, country.timezone, day, manifest.records_per_interval_per_zone)
            if day < today
            else None
        )
        fp = compute(
            status=max((r.status for r in results), default=0),
            content_type=results[0].content_type if results else "",
            size=sum(len(r.content) for r in results),
            records=day_records,
            rows_expected=expected_records,
        )
        cfg = manifest.fingerprint
        # Structure and distributions can only be learnt from, and compared on, records. Without any (an
        # error status, or a partition whose first interval is not published yet) only transport and
        # zero-row checks apply.
        history = [h for h in db.fingerprint_history(conn, manifest.source, manifest.dataset) if h.rows]
        baseline = Baseline.from_history(history, cfg.iqr_multiplier) if day_records else Baseline(runs=len(history))
        devs = compare(fp, baseline, Thresholds(cfg.warmup_runs, cfg.null_rate_margin, cfg.iqr_multiplier))
        if manifest.per_period_resolution and not day_records and manifest.history_start_local <= day < today:
            # No record count is expected per manifest here, so compare() cannot see an empty past day.
            devs.append(Deviation("hard", "content", "zero rows for a past day"))
        sev = severity(devs)
        outcome.severity = sev
        outcome.deviations = [f"{d.severity}:{d.level}:{d.detail}" for d in devs]
        db.record_fingerprint(conn, run_id, fp, devs, sev)

        if sev == "hard":
            _bead(conn, manifest, day, "hard deviation", "\n".join(outcome.deviations), outcome.raw_uris)
            outcome.status = "blocked"
            return outcome

        rows: list[db.ObservationRow] = []
        series: dict[str, SeriesInfo] = {}
        for r in results:
            if not 200 <= r.status < 300:
                continue
            batch = adapter.parse(manifest, country, entries, r, w.fetch_start, w.fetch_end)
            rows.extend(batch.rows)
            series.update(batch.series)
        if sev == "soft":
            rows = quarantine_partition_day(rows, w)

        series_ids = {
            key: db.ensure_series(
                conn,
                db.SeriesSpec(
                    series_key=key,
                    entry_id=info.entry.id,
                    entity_id=info.entity_id,
                    unit=info.entry.unit,
                    resolution=info.resolution or manifest.resolution_td,
                    kind=info.entry.kind,
                    sensitivity=info.entry.sensitivity,
                    licence=manifest.licence,
                ),
            )
            for key, info in series.items()
        }
        conn.commit()
        outcome.rows_received = sum(1 for row in rows if w.day_start <= row.interval_start < w.day_end)
        if day >= today:
            outcome.rows_expected = None
        elif manifest.per_period_resolution:
            outcome.rows_expected = expected_series_rows(rows, series, country.timezone, day, w)
        else:
            outcome.rows_expected = expected_partition_rows(manifest, country.timezone, day, rows_per_zone)
        outcome.rows_written = db.write_observations(conn, rows, series_ids, run_id)
        outcome.status = "ok"
        return outcome
    except ParseError as exc:
        conn.rollback()
        outcome.status = "failed"
        outcome.severity = "hard"
        outcome.deviations.append(f"hard:parse:{exc}")
        _bead(conn, manifest, day, "parse error", str(exc), outcome.raw_uris)
        raise
    except Exception as exc:
        conn.rollback()
        outcome.status = "failed"
        if stage == "fetch":
            # The adapter's retries are exhausted and there is no response to store (ADR 0013: hard).
            outcome.severity = "hard"
            outcome.deviations.append(f"hard:transport:{type(exc).__name__}: {exc}")
            _bead(conn, manifest, day, "fetch failed", outcome.deviations[-1], outcome.raw_uris)
        else:
            outcome.deviations.append(f"error:{type(exc).__name__}:{exc}")
        raise
    finally:
        db.finish_run(
            conn,
            run_id,
            status=outcome.status if outcome.status != "running" else "failed",
            rows_received=outcome.rows_received,
            rows_expected=outcome.rows_expected,
            rows_written=outcome.rows_written,
            raw_uris=outcome.raw_uris,
            error=None if outcome.status in ("ok", "blocked") else "; ".join(outcome.deviations)[:4000],
        )
        conn.close()
