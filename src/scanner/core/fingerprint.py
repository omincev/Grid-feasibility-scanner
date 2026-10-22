"""Run fingerprints and deviation classification (ADR 0013, docs/quality.md layer 2)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from statistics import quantiles
from typing import Any, Literal

Severity = Literal["none", "soft", "hard"]


@dataclass(frozen=True)
class Fingerprint:
    status: int
    content_type: str
    size: int
    schema: dict[str, str]
    rows: int
    rows_expected: int | None
    null_rate: dict[str, float]
    numeric: dict[str, dict[str, float]]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Deviation:
    severity: Literal["soft", "hard"]
    level: Literal["transport", "structure", "content"]
    detail: str


@dataclass
class Thresholds:
    warmup_runs: int = 14
    null_rate_margin: float = 0.05
    iqr_multiplier: float = 3.0


DEFAULT_THRESHOLDS = Thresholds()


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int | float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def compute(
    *, status: int, content_type: str, size: int, records: list[dict[str, Any]], rows_expected: int | None
) -> Fingerprint:
    """Fingerprint one fetch. Schema types ignore nulls; a field that is always null has type 'null'."""
    fields: dict[str, set[str]] = {}
    nulls: dict[str, int] = {}
    nums: dict[str, list[float]] = {}
    for r in records:
        for k, v in r.items():
            t = _type_name(v)
            fields.setdefault(k, set())
            if t == "null":
                nulls[k] = nulls.get(k, 0) + 1
            else:
                fields[k].add(t)
            if t == "number" and isinstance(v, int | float) and math.isfinite(v):
                nums.setdefault(k, []).append(float(v))
    schema = {k: "|".join(sorted(v)) if v else "null" for k, v in fields.items()}
    n = len(records)
    null_rate = {k: nulls.get(k, 0) / n for k in fields} if n else {}
    numeric: dict[str, dict[str, float]] = {}
    for k, values in nums.items():
        if len(values) >= 4:
            q1, q2, q3 = quantiles(values, n=4)
        else:
            q1 = q2 = q3 = sorted(values)[len(values) // 2]
        numeric[k] = {"min": min(values), "max": max(values), "q1": q1, "median": q2, "q3": q3}
    return Fingerprint(status, content_type, size, schema, n, rows_expected, null_rate, numeric)


@dataclass
class Baseline:
    """Aggregated history of previous fingerprints for one manifest."""

    runs: int = 0
    schema: dict[str, str] = field(default_factory=dict)
    max_null_rate: dict[str, float] = field(default_factory=dict)
    low: dict[str, float] = field(default_factory=dict)
    high: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_history(cls, history: list[Fingerprint], iqr_multiplier: float = 3.0) -> Baseline:
        b = cls(runs=len(history))
        if not history:
            return b
        b.schema = dict(history[-1].schema)
        for fp in history:
            for k, rate in fp.null_rate.items():
                b.max_null_rate[k] = max(b.max_null_rate.get(k, 0.0), rate)
            for k, s in fp.numeric.items():
                iqr = s["q3"] - s["q1"]
                lo, hi = s["min"] - iqr_multiplier * iqr, s["max"] + iqr_multiplier * iqr
                b.low[k] = min(b.low.get(k, lo), lo)
                b.high[k] = max(b.high.get(k, hi), hi)
        return b


def compare(fp: Fingerprint, baseline: Baseline, t: Thresholds = DEFAULT_THRESHOLDS) -> list[Deviation]:
    """Classify deviations. Hard checks always apply; soft checks only after warm-up."""
    devs: list[Deviation] = []
    if not 200 <= fp.status < 300:
        devs.append(Deviation("hard", "transport", f"HTTP status {fp.status}"))
    if fp.rows == 0 and (fp.rows_expected or 0) > 0:
        devs.append(Deviation("hard", "content", f"zero rows, expected {fp.rows_expected}"))
    if baseline.schema:
        added = set(fp.schema) - set(baseline.schema)
        removed = set(baseline.schema) - set(fp.schema)
        retyped = {
            k
            for k in set(fp.schema) & set(baseline.schema)
            if "null" not in (fp.schema[k], baseline.schema[k]) and fp.schema[k] != baseline.schema[k]
        }
        for k in sorted(added):
            devs.append(Deviation("hard", "structure", f"field added: {k}"))
        for k in sorted(removed):
            devs.append(Deviation("hard", "structure", f"field removed: {k}"))
        for k in sorted(retyped):
            devs.append(Deviation("hard", "structure", f"field {k} type {baseline.schema[k]} -> {fp.schema[k]}"))
    if fp.rows_expected is not None and 0 < fp.rows != fp.rows_expected:
        devs.append(Deviation("soft", "content", f"rows {fp.rows} != expected {fp.rows_expected}"))
    if baseline.runs >= t.warmup_runs:
        for k, rate in fp.null_rate.items():
            limit = baseline.max_null_rate.get(k, 0.0) + t.null_rate_margin
            if rate > limit:
                devs.append(Deviation("soft", "content", f"null rate {k} {rate:.3f} > {limit:.3f}"))
        for k, s in fp.numeric.items():
            if k in baseline.low and (s["min"] < baseline.low[k] or s["max"] > baseline.high[k]):
                devs.append(
                    Deviation(
                        "soft",
                        "content",
                        f"{k} range [{s['min']}, {s['max']}] outside [{baseline.low[k]}, {baseline.high[k]}]",
                    )
                )
    return devs


def severity(devs: list[Deviation]) -> Severity:
    if any(d.severity == "hard" for d in devs):
        return "hard"
    if devs:
        return "soft"
    return "none"
