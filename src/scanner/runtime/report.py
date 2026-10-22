"""The methods and scenarios report: exports/methods_and_scenarios.html and its markdown twin .md.

For readers who are not engineers: every scenario with its sets and switches, the scenario comparison (zone
and site scores, ranks and BESS ranges per scenario, with differences to the official scenario), the
assumption register (every parameter of every set, its value or proposal, note, owners and the scenarios that
use or change it) and every method card. Read from serving.scenario_catalog, serving.scenario_comparison,
serving.assumption_register and serving.method_catalog as role `anon`, like the static export, so the report
holds public data only. The HTML is the markdown rendered with markdown-it-py plus inline CSS: one file, no
scripts, no external resources. `scanner.cli export` (and the pipeline's export step) writes it.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from markdown_it import MarkdownIt

from scanner import REPO_ROOT
from scanner.settings import get_settings

NAME = "methods_and_scenarios"
VIEWS = ("scenario_catalog", "scenario_comparison", "assumption_register", "method_catalog")

CSS = """
body { font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #1d232b; background: #fff;
       max-width: 1200px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 28px; margin-bottom: 4px; } h2 { margin-top: 40px; border-bottom: 2px solid #d8dee6; }
h3 { margin-top: 28px; } table { border-collapse: collapse; margin: 12px 0; display: block; overflow-x: auto; }
th, td { border: 1px solid #d8dee6; padding: 4px 8px; text-align: left; vertical-align: top; font-size: 13px; }
th { background: #f2f5f8; } code { font-size: 12px; background: #f2f5f8; padding: 1px 3px; border-radius: 3px;
       word-break: break-word; } blockquote { margin: 0; padding: 4px 12px; border-left: 4px solid #d8dee6;
       color: #4a5562; }
@media (prefers-color-scheme: dark) {
  body { color: #e3e8ee; background: #14181d; } th { background: #222831; } th, td { border-color: #39424d; }
  code { background: #222831; } h2 { border-color: #39424d; } blockquote { border-color: #39424d; color: #aab4bf; }
}
"""


@dataclass(frozen=True)
class ReportData:
    generated_at: datetime
    scenarios: list[dict[str, Any]]
    comparison: list[dict[str, Any]]
    register: list[dict[str, Any]]
    methods: list[dict[str, Any]]


def read_report_data(conn: psycopg.Connection[Any]) -> ReportData:
    """The four serving views as rows (dicts), read in the caller's role."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for view in VIEWS:
        cur = conn.execute(f'select * from serving."{view}"')
        cols = [d.name for d in cur.description or []]
        rows[view] = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    scenarios = sorted(rows["scenario_catalog"], key=lambda s: (s["status"] != "official", s["scenario_id"]))
    return ReportData(
        datetime.now(UTC),
        scenarios,
        rows["scenario_comparison"],
        sorted(rows["assumption_register"], key=lambda r: (r["set_id"], r["parameter"])),
        sorted(rows["method_catalog"], key=lambda m: m["method_id"]),
    )


def _cell(value: Any) -> str:
    """Text for a markdown table cell: JSON for structures, no pipes or line breaks."""
    if value is None:
        text = "—"
    elif isinstance(value, dict | list):
        text = f"`{json.dumps(value, ensure_ascii=False, separators=(', ', ': '))}`"
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(_cell(v) for v in r) + " |" for r in rows]
    return [*out, ""]


def _num(value: float | None, digits: int = 1) -> str | None:
    return None if value is None else f"{value:,.{digits}f}"


def _signed(value: float | None, digits: int = 1) -> str | None:
    return None if value is None else f"{value:+,.{digits}f}"


def _score(row: dict[str, Any] | None) -> str | None:
    if row is None or row["congestion_score"] is None:
        return None
    return f"{row['congestion_score']:.1f} (#{row['rank']})"


def _range(row: dict[str, Any] | None) -> str | None:
    if row is None or row["revenue_low_eur_per_mw_year"] is None:
        return None
    return f"{_num(row['revenue_low_eur_per_mw_year'], 0)} – {_num(row['revenue_high_eur_per_mw_year'], 0)}"


def _delta(row: dict[str, Any] | None) -> str | None:
    if row is None or row["score_delta"] is None:
        return None
    rank = int(row["rank_delta"])
    places = f"{abs(rank)} place{'s' if abs(rank) > 1 else ''}"
    moved = "same rank" if rank == 0 else f"{places} {'lower' if rank > 0 else 'higher'}"
    return f"{_signed(row['score_delta'])} points, {moved}"


def _flags(per: dict[str, dict[str, Any]], ids: Sequence[str]) -> str | None:
    """Each flag with the scenarios that raise it, e.g. "no_station_data (demo_default, spec_v0)"."""
    flags = sorted({f for r in per.values() for f in r["score_flags"] or []})
    named = [f"{f} ({', '.join(s for s in ids if f in (per[s]['score_flags'] if s in per else []))})" for f in flags]
    return ", ".join(named) or None


def _comparison(data: ReportData, kind: str, label: str) -> list[str]:
    ids = [s["scenario_id"] for s in data.scenarios]
    official = next((s["scenario_id"] for s in data.scenarios if s["status"] == "official"), None)
    by_entity: dict[str, dict[str, dict[str, Any]]] = {}
    for r in data.comparison:
        if r["kind"] == kind:
            by_entity.setdefault(r["entity_id"], {})[r["scenario_id"]] = r
    if not by_entity:
        return ["No scores yet.", ""]

    def order(entity: str) -> tuple[int, str]:
        first = by_entity[entity].get(official or "") or next(iter(by_entity[entity].values()))
        return int(first["rank"]), entity

    header = [label, "Bidding zone"] + [f"Score (rank) · {s}" for s in ids]
    header += [f"{s} vs {official}" for s in ids if s != official]
    header += [f"BESS EUR/MW/yr · {s}" for s in ids] + ["Flags"]
    rows = []
    for entity in sorted(by_entity, key=order):
        per = by_entity[entity]
        any_row = next(iter(per.values()))
        name = f"{any_row['name']} (benchmark)" if any_row["is_benchmark"] else any_row["name"]
        row = [name, any_row["zone_id"], *(_score(per.get(s)) for s in ids)]
        row += [_delta(per.get(s)) for s in ids if s != official]
        row += [_range(per.get(s)) for s in ids]
        rows.append([*row, _flags(per, ids)])
    return _table(header, rows)


def render_markdown(data: ReportData) -> str:
    official = next((s for s in data.scenarios if s["status"] == "official"), None)
    lines = [
        "# Methods and scenarios",
        "",
        f"Generated {data.generated_at:%Y-%m-%d %H:%M} UTC from the public serving views of the Grid Opportunity "
        "Scanner. Every score, opportunity type and return range is computed once per scenario; the UI shows the "
        "official scenario. Values whose status is `proposed_only` are proposals that the owners have not "
        'approved yet; outputs that use them carry the label "proposed assumptions (not approved)".',
        "",
        (
            f"Official scenario: **{official['title']}** (`{official['scenario_id']}`)."
            if official
            else "None official."
        ),
        "",
        "Contents: 1. Scenarios · 2. Scenario comparison · 3. Assumption register · 4. Method cards",
        "",
        "## 1. Scenarios",
        "",
    ]
    for s in data.scenarios:
        lines += [f"### {s['title']} (`{s['scenario_id']}`, {s['status']})", "", s["description"], ""]
        sets = s["sets"] or {}
        lines += _table(
            ["Role", "Assumption set", "Content hash"],
            [[role, set_id, (s["assumption_hashes"] or {}).get(set_id, "")[:12]] for role, set_id in sets.items()],
        )
        changed = [("method switch", k, v) for k, v in (s["method_switches"] or {}).items()]
        changed += [("override", k, v) for k, v in (s["overrides"] or {}).items()]
        if changed:
            lines += ["Values this scenario changes:", ""]
            lines += _table(["Kind", "Parameter", "Value"], changed)
        approval = f"approved by {s['approved_by']} on {s['approved_on']}" if s["approved_by"] else "not approved"
        runs = {k: f"`{s[f'{k}_run_id']}`" if s[f"{k}_run_id"] else "none yet" for k in ("score", "opportunity")}
        valuation = f"`{s['valuation_run_id']}`" if s["valuation_run_id"] else "none yet"
        lines += [
            f"Owners: {', '.join(s['owners'])}; {approval}. Scenario file hash `{s['content_hash'][:12]}`. "
            f"Latest runs: score {runs['score']}, opportunity types {runs['opportunity']}, valuation {valuation}.",
            "",
        ]
    lines += [
        "## 2. Scenario comparison",
        "",
        "Score from 0 to 100 and rank (#1 = highest) per scenario; differences are against the official scenario. "
        "A site shows its zone's BESS range (zone-level, perfect-foresight upper bound, day-ahead arbitrage only).",
        "",
        "### Zones",
        "",
        *_comparison(data, "zone", "Zone"),
        "### Municipalities (site scores)",
        "",
        *_comparison(data, "municipality", "Municipality"),
        "## 3. Assumption register",
        "",
        "Every parameter of every assumption set. `value_set`: an owner set the value; `proposed_only`: only a "
        'proposal exists; `missing`: neither. "Scenario values" are the values scenarios set instead.',
        "",
    ]
    by_set: dict[str, list[dict[str, Any]]] = {}
    for r in data.register:
        by_set.setdefault(r["set_id"], []).append(r)
    for set_id, params in by_set.items():
        first = params[0]
        used = ", ".join(first["scenarios"]) or "no scenario"
        lines += [
            f"### {set_id}",
            "",
            f"Kind {first['set_kind']}, status {first['set_status']}, owners {', '.join(first['owners'])}, used by "
            f"{used}. Content hash `{first['content_hash'][:12]}`, file `{first['source_path']}`.",
            "",
        ]
        lines += _table(
            ["Parameter", "Value", "Proposed", "Status", "Scenario values", "Note / source"],
            [[p["parameter"], p["value"], p["proposed"], p["status"], p["scenario_values"], p["note"]] for p in params],
        )
    lines += ["## 4. Method cards", ""]
    for m in data.methods:
        lines += [
            f"### {m['title']} (`{m['method_id']}` version {m['version']}, {m['status']})",
            "",
            f"- Owner: {', '.join(m['owner'])}",
            f"- Inputs: {', '.join(f'`{i}`' for i in m['inputs']) or 'none'}",
            f"- Parameters: {', '.join(f'`{p}`' for p in m['parameters']) or 'none (scenario-independent)'}",
            f"- Outputs: {', '.join(f'`{o}`' for o in m['outputs'])}",
            "- Known biases:",
            *(f"  - {b}" for b in m["known_biases"]),
            "",
            m["body"].strip(),
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def render_html(markdown: str) -> str:
    body = MarkdownIt("commonmark", {"html": False}).enable("table").render(markdown)
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape('Methods and scenarios')}</title>\n<style>{CSS}</style>\n</head>\n"
        f"<body>\n{body}</body>\n</html>\n"
    )


def write_report(out_dir: Path = REPO_ROOT / "exports", database_url: str | None = None) -> list[str]:
    """Write the report as HTML and markdown to out_dir; return the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(database_url or get_settings().database_url) as conn:
        conn.execute("set local role anon")
        data = read_report_data(conn)
    markdown = render_markdown(data)
    paths = [out_dir / f"{NAME}.md", out_dir / f"{NAME}.html"]
    paths[0].write_text(markdown, encoding="utf-8", newline="\n")
    paths[1].write_text(render_html(markdown), encoding="utf-8", newline="\n")
    return [str(p) for p in paths]
