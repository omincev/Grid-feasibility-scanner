"""Cross-file contract checks run by `make contracts` and CI.

Each check returns a list of human-readable errors. `python -m scanner.core.contracts` exits non-zero
if any check fails.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

from scanner import REPO_ROOT
from scanner.core.codebook import load_classes, load_codebook_files, load_entries
from scanner.core.manifest import load_manifests, manifest_paths
from scanner.core.methods import REFERENCED, MethodRef, load_method_cards
from scanner.core.registry import (
    AssumptionSet,
    Scenario,
    all_zones,
    load_assumptions,
    load_benchmarks,
    load_countries,
    load_market_rules,
    load_scenarios,
    load_snapshots,
)


def check_codebook(root: Path) -> list[str]:
    errors: list[str] = []
    classes = load_classes(root)
    countries = load_countries(root)
    for path, f in load_codebook_files(root).items():
        if path.parent.name != f.country:
            errors.append(f"{path}: folder {path.parent.name} does not match country {f.country}")
        if f.country not in countries:
            errors.append(f"{path}: unknown country {f.country}")
        expected_name = f"{f.source}.{f.dataset}.yaml"
        if path.name != expected_name:
            errors.append(f"{path}: file must be named {expected_name}")
        for e in f.entries:
            if e.equivalence_class not in classes:
                errors.append(f"{e.id}: unknown equivalence class {e.equivalence_class}")
            if not e.id.startswith(f"{f.country}."):
                errors.append(f"{e.id}: id must start with country code {f.country}")
    return errors


def check_manifests(root: Path) -> list[str]:
    errors: list[str] = []
    entries = load_entries(root)
    zones = all_zones(load_countries(root))
    names: set[str] = set()
    for path, m in zip(manifest_paths(root), load_manifests(root), strict=True):
        if path.stem != m.dataset:
            errors.append(f"{path}: file name must equal dataset {m.dataset}")
        if m.asset_name in names:
            errors.append(f"{path}: duplicate asset name {m.asset_name}")
        names.add(m.asset_name)
        for zone_id in m.zones.values():
            if zone_id not in zones:
                errors.append(f"{path}: zone {zone_id} not declared in registry/countries")
        for entry_id in m.entries:
            if entry_id not in entries:
                errors.append(f"{path}: codebook entry {entry_id} does not exist")
                continue
            header, _ = entries[entry_id]
            if (header.source, header.dataset) != (m.source, m.dataset):
                errors.append(f"{path}: entry {entry_id} belongs to {header.source}.{header.dataset}")
    return errors


def check_countries(root: Path) -> list[str]:
    errors: list[str] = []
    countries = load_countries(root)
    zones = all_zones(countries)
    classes = load_classes(root)
    for c in countries.values():
        for z in c.reference_zones:
            if z not in zones:
                errors.append(f"{c.code}: reference zone {z} is not declared")
        for cls in c.precedence:
            if cls not in classes:
                errors.append(f"{c.code}: precedence for unknown class {cls}")
    return errors


_EIC_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-"
_EIC_RE = re.compile(r"^[0-9]{2}[A-Z][A-Z0-9-]{12}[A-Z0-9]$")


def eic_check_character(code15: str) -> str:
    """Check character for the first 15 characters of an EIC (ENTSO-E EIC Data Exchange IG v1.2).

    Values 0-9, A-Z = 10-35, '-' = 36; weights 16 down to 2; check value 36 - ((sum - 1) mod 37).
    """
    total = sum(_EIC_ALPHABET.index(c) * (16 - i) for i, c in enumerate(code15))
    return _EIC_ALPHABET[36 - (total - 1) % 37]


def check_eic(root: Path) -> list[str]:
    """Zone EICs are well formed, have a valid check character and are unique (ENTSO-E base tier, ADR 0011).

    ENTSO-E queries are keyed by EIC alone, so a typo silently fetches another area or nothing;
    ref.entity also has a unique index on eic.
    """
    errors: list[str] = []
    seen: dict[str, str] = {}
    for zone_id, (_, z) in all_zones(load_countries(root)).items():
        if z.eic is None:
            if z.eic_verified:
                errors.append(f"{zone_id}: eic_verified is true but eic is null")
            continue
        if not _EIC_RE.match(z.eic):
            errors.append(f"{zone_id}: EIC {z.eic!r} is not 16 characters of 0-9, A-Z and '-'")
        elif eic_check_character(z.eic[:15]) != z.eic[15]:
            errors.append(f"{zone_id}: EIC {z.eic} has an invalid check character")
        if z.eic in seen:
            errors.append(f"{zone_id}: EIC {z.eic} is also used by {seen[z.eic]}")
        seen[z.eic] = zone_id
    return errors


def check_market_rules(root: Path) -> list[str]:
    countries = load_countries(root)
    zones = all_zones(countries)
    errors: list[str] = []
    for code, f in load_market_rules(root).items():
        if code not in countries:
            errors.append(f"market_rules/{code}.yaml: unknown country")
        for r in f.rules:
            if r.zone and r.zone not in zones:
                errors.append(f"market_rules/{code}.yaml: rule {r.key} references unknown zone {r.zone}")
    return errors


def check_features(root: Path) -> list[str]:
    from scanner.features.registry import FEATURES

    classes = load_classes(root)
    errors: list[str] = []
    for key, spec in FEATURES.items():
        for cls in spec.accepted_classes:
            if cls not in classes:
                errors.append(f"feature {key}: accepts unknown class {cls}")
    return errors


def check_facts(root: Path) -> list[str]:
    """Every codebook entry a fact parser writes as core.fact.attribute exists, belongs to that source
    dataset and field (and dimension, where a row is selected by one), like check_manifests for series."""
    from scanner.adapters.energinet.connection_queue import ENTRIES as QUEUE_ENTRIES
    from scanner.adapters.kapacitetskort.stations import LAYERS

    entries = load_entries(root)
    wanted = [(f.entry_id, "kapacitetskort", layer.name, f.field, f.select) for layer in LAYERS for f in layer.facts]
    wanted += [
        (entry_id, "energinet", "connection_queue", "Kapacitet i alt MW", None) for entry_id in QUEUE_ENTRIES.values()
    ]
    errors: list[str] = []
    for entry_id, source, dataset, field, select in wanted:
        if entry_id not in entries:
            errors.append(f"fact attribute {entry_id} has no codebook entry")
            continue
        header, e = entries[entry_id]
        if (header.source, header.dataset) != (source, dataset):
            errors.append(
                f"fact attribute {entry_id} belongs to {header.source}.{header.dataset}, not {source}.{dataset}"
            )
        if e.field != field:
            errors.append(f"fact attribute {entry_id} maps field {field}, the codebook says {e.field}")
        if select and e.dimensions.get(select[0]) != select[1]:
            errors.append(
                f"fact attribute {entry_id} selects {select[0]}={select[1]}, the codebook says {e.dimensions}"
            )
    return errors


def check_geometries(root: Path) -> list[str]:
    """Every geometry feature names a declared entity; every entity parent and benchmark entity exists."""
    errors: list[str] = []
    ids = set(all_zones(load_countries(root)))
    entity_files = sorted((root / "registry" / "entities").glob("*/entities.yaml"))
    entities = [
        e for f in entity_files for e in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("entities", [])
    ]
    ids |= {e["id"] for e in entities}
    for e in entities:
        if e.get("parent") and e["parent"] not in ids:
            errors.append(f"{e['id']}: parent {e['parent']} is not declared")
    for path in sorted((root / "registry" / "geometries").glob("*/*.geojson")):
        if not path.with_suffix(".meta.yaml").exists():
            errors.append(f"{path}: missing sibling .meta.yaml")
        for feat in json.loads(path.read_text(encoding="utf-8"))["features"]:
            if feat["properties"].get("entity_id") not in ids:
                errors.append(f"{path}: entity_id {feat['properties'].get('entity_id')} is not declared")
    for b in load_benchmarks(root):
        if b.entity and b.entity not in ids:
            errors.append(f"benchmark {b.id}: entity {b.entity} is not declared")
    return errors


def check_other(root: Path) -> list[str]:
    load_snapshots(root)
    load_assumptions(root)
    return []


# Packages whose modules declare the methods they implement (scanner.core.methods.MethodRef).
METHOD_PACKAGES = ("scanner.features", "scanner.scores", "scanner.valuation")


def referenced_methods() -> dict[str, MethodRef]:
    """Every MethodRef declared by a module of METHOD_PACKAGES (importing them registers their refs)."""
    for name in METHOD_PACKAGES:
        package = importlib.import_module(name)
        for info in pkgutil.walk_packages(getattr(package, "__path__", []), f"{name}."):
            importlib.import_module(info.name)
    return dict(REFERENCED)


def _unresolved(ref: str, scenario: Scenario, sets: dict[str, AssumptionSet]) -> str | None:
    role, name = ref.split(".", 1)
    set_id = scenario.sets.get(role)
    if set_id is None:
        return f"scenario {scenario.id} names no set for role {role}"
    if set_id not in sets:
        return f"scenario {scenario.id}: set {set_id} (role {role}) does not exist"
    if name not in sets[set_id].parameters:
        return f"set {set_id} (role {role} in scenario {scenario.id}) has no parameter {name}"
    return None


def check_methods(root: Path, referenced: dict[str, MethodRef] | None = None) -> list[str]:
    """Every method the code references has a card with its version and parameters, and every card's
    parameters resolve through every scenario's sets (docs/methods/README.md)."""
    cards = load_method_cards(root)
    scenarios, sets = load_scenarios(root), load_assumptions(root)
    errors: list[str] = []
    for ref in (referenced_methods() if referenced is None else referenced).values():
        card = cards.get(ref.id)
        if card is None:
            errors.append(f"method {ref.id} is referenced in code but has no card docs/methods/{ref.id}.md")
            continue
        if card.version != ref.version:
            errors.append(f"method {ref.id}: code implements version {ref.version}, the card says {card.version}")
        if set(card.parameters) != set(ref.parameters):
            errors.append(
                f"method {ref.id}: card parameters {sorted(card.parameters)} differ from the code's "
                f"{sorted(ref.parameters)}"
            )
    for card in cards.values():
        for p in card.parameters:
            errors += [
                f"method {card.id}: parameter {p} does not resolve: {why}"
                for s in scenarios.values()
                if (why := _unresolved(p, s, sets))
            ]
    return errors


def check_scenarios(root: Path) -> list[str]:
    """Exactly one official scenario; every set it names exists; every switched or overridden parameter
    exists in its role's set; the score method choices are ones the code implements."""
    from scanner.scores.method import PARAMETERS, parse_score_method

    scenarios, sets = load_scenarios(root), load_assumptions(root)
    errors: list[str] = []
    official = sorted(s.id for s in scenarios.values() if s.status == "official")
    if len(official) != 1:
        errors.append(f"exactly one scenario must be official, found {official or 'none'}")
    for s in scenarios.values():
        missing = {role: set_id for role, set_id in s.sets.items() if set_id not in sets}
        errors += [f"scenario {s.id}: set {set_id} (role {role}) does not exist" for role, set_id in missing.items()]
        for key in [*s.method_switches, *s.overrides]:
            if (why := _unresolved(key, s, sets)) and key.split(".")[0] not in missing:
                errors.append(f"scenario {s.id}: {key} does not resolve: {why}")
        method_set = sets.get(s.sets.get("score_method", ""))
        if method_set is None:
            continue
        values = {k: p.value if p.value is not None else p.proposed for k, p in method_set.parameters.items()}
        values |= {k: v for k, v in s.values_for("score_method").items() if k in values}  # unknown ones: above
        try:
            parse_score_method({k: v for k, v in values.items() if k in PARAMETERS})
        except (ValueError, KeyError) as exc:
            errors.append(f"scenario {s.id}: score method: {type(exc).__name__}: {exc}")
    return errors


CHECKS: dict[str, Callable[[Path], list[str]]] = {
    "codebook": check_codebook,
    "manifests": check_manifests,
    "countries": check_countries,
    "eic": check_eic,
    "market_rules": check_market_rules,
    "features": check_features,
    "facts": check_facts,
    "geometries": check_geometries,
    "methods": check_methods,
    "scenarios": check_scenarios,
    "other": check_other,
}


def run_all(root: Path = REPO_ROOT) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    for name, fn in CHECKS.items():
        try:
            results[name] = fn(root)
        except Exception as exc:  # a file that fails to parse is itself a contract failure
            results[name] = [f"{type(exc).__name__}: {exc}"]
    return results


def main() -> int:
    results = run_all()
    failed = False
    for name, errors in results.items():
        status = "ok" if not errors else f"{len(errors)} error(s)"
        print(f"[{name}] {status}")
        for e in errors:
            print(f"  - {e}")
        failed |= bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
