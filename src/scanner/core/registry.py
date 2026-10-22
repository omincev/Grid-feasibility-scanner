"""Models and loaders for curated files: countries, entities, snapshots, market rules, assumptions,
benchmarks, claims and geometry metadata."""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from scanner.core.sensitivity import Sensitivity

ZONE_ID = r"^[a-z]{2}\.zone\.[a-z0-9_]+$"
ENTITY_ID = r"^[a-z]{2,3}\.[a-z_]+\.[a-z0-9_.-]+$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Zone(_Strict):
    id: str = Field(pattern=ZONE_ID)
    short: str
    name: str
    eic: str | None
    eic_verified: bool


class Country(_Strict):
    code: str = Field(pattern=r"^[a-z]{2}$")
    name: str
    status: Literal["active", "draft", "reference"]
    timezone: str
    currency: str
    reporting_currency: str
    tso: str | None
    geography_levels: list[str]
    zones: list[Zone]
    precedence: dict[str, list[str]]
    reference_zones: list[str]

    @model_validator(mode="after")
    def _zone_prefix(self) -> Country:
        for z in self.zones:
            if not z.id.startswith(f"{self.code}.zone."):
                raise ValueError(f"zone {z.id} does not belong to country {self.code}")
        if self.status == "active" and not self.zones:
            raise ValueError(f"active country {self.code} has no zones")
        return self


def load_countries(root: Path) -> dict[str, Country]:
    out: dict[str, Country] = {}
    for path in sorted((root / "registry" / "countries").glob("*.yaml")):
        c = Country(**yaml.safe_load(path.read_text()))
        if path.stem != c.code:
            raise ValueError(f"{path}: file name must equal country code {c.code}")
        out[c.code] = c
    return out


def all_zones(countries: dict[str, Country]) -> dict[str, tuple[Country, Zone]]:
    return {z.id: (c, z) for c in countries.values() for z in c.zones}


def country_entity_id(code: str) -> str:
    """Entity ID of a country itself (ADR 0004); the parent of its zones."""
    return f"{code}.country.{code}"


_FOLD = str.maketrans({"æ": "ae", "ø": "oe", "å": "aa", "Æ": "ae", "Ø": "oe", "Å": "aa", "$": "s"})


def slug(text: str) -> str:
    """An entity-ID slug (ADR 0004) from a source's name or code: æ/ø/å -> ae/oe/aa, "$" -> "s", other
    letters folded to ASCII, lower case, runs of anything else -> "_", trimmed. ValueError if empty."""
    folded = unicodedata.normalize("NFKD", text.translate(_FOLD)).encode("ascii", "ignore").decode().lower()
    out = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    if not out:
        raise ValueError(f"cannot build a slug from {text!r}")
    return out


EntityKind = Literal[
    "country", "zone", "tso_area", "dso_area", "substation", "transformer", "line", "municipality", "plant", "site"
]


class Entity(_Strict):
    """An entity declared in registry/entities/<cc>/entities.yaml (ADR 0004, registry/entities/README.md).

    Countries and zones come from the country files. Geometries go in registry/geometries; the two
    geometry fields exist for the documented file format and must stay null.
    """

    id: str = Field(pattern=ENTITY_ID)
    kind: EntityKind
    parent: str | None
    name: str
    eic: str | None = None
    attributes: dict[str, Any] = {}
    sensitivity: Sensitivity = "public"
    geometry_blob: str | None = None
    geometry_wkt: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Entity:
        if self.id.split(".")[1] != self.kind:
            raise ValueError(f"{self.id}: id must have the form <cc>.{self.kind}.<slug>")
        if self.kind in ("country", "zone"):
            raise ValueError(f"{self.id}: countries and zones are declared in registry/countries")
        if self.geometry_blob or self.geometry_wkt:
            raise ValueError(f"{self.id}: put geometries in registry/geometries/<cc>/<name>.geojson")
        return self


class EntitiesFile(_Strict):
    entities: list[Entity]


class EntityAlias(_Strict):
    """A source's name for one of our entities, approved by a human (ADR 0004, H6)."""

    entity_id: str = Field(pattern=ENTITY_ID)
    source: str
    source_id: str | None = None
    source_name: str
    approved_by: str
    approved_on: date
    evidence: dict[str, Any] | None = None


class AliasesFile(_Strict):
    aliases: list[EntityAlias]


class GeometryMeta(_Strict):
    """Provenance of registry/geometries/<cc>/<name>.geojson, kept in the sibling <name>.meta.yaml."""

    source_url: str
    source: str
    licence: str
    retrieved: date
    sha256_original: str = Field(pattern=r"^[0-9a-f]{64}$")
    sha256_note: str | None = None  # what the hash covers when the original was not one file (e.g. paged API)
    crs: Literal["EPSG:4326"]
    transformation: str
    approved_by: str | None = None
    sensitivity: Sensitivity = "public"
    notes: str | None = None


class Snapshot(_Strict):
    """One watched source (registry/snapshots.yaml, scanner.runtime.snapshots).

    http requests are GET unless `method: POST`, which sends `body` as JSON. `headers` are added to the
    request. `powerbi_embed_page`: a public page embedding a Power BI report (app.powerbi.com/view?r=...);
    the runtime reads the report's public resource key from it on every capture and sends it as
    X-PowerBI-ResourceKey, so no key is stored. Change hash normalisers (scanner.runtime.snapshots):
    `normalise: visible_text` for HTML pages that differ on every request (random element ids, e-mail
    obfuscation tokens); `ignore_keys`, JSON keys removed at any depth (per-request ids, timestamps).
    on_change `parse:<name>` runs that parser on every successful capture (scanner.runtime.snapshot_parsers).
    """

    id: str
    url: str | None
    kind: Literal["json", "web_page", "pdf", "terms", "file"]
    render: Literal["http", "browser"]
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = {}
    body: dict[str, Any] | None = None
    powerbi_embed_page: str | None = None
    normalise: Literal["visible_text"] | None = None
    ignore_keys: list[str] = []
    schedule: str
    timezone: str
    on_change: str = Field(pattern=r"^(research|none|parse:[a-z0-9_.]+)$")
    owner: str
    status: Literal["active", "to_research"]
    note: str | None = None

    @model_validator(mode="after")
    def _url_when_active(self) -> Snapshot:
        if self.status == "active" and not self.url:
            raise ValueError(f"snapshot {self.id}: active snapshots need a url")
        if len(self.schedule.split()) != 5:
            raise ValueError(f"snapshot {self.id}: schedule must be a 5-field cron expression")
        if self.method == "GET" and (self.body is not None or self.powerbi_embed_page):
            raise ValueError(f"snapshot {self.id}: body and powerbi_embed_page need method POST")
        if self.render == "browser" and (self.method != "GET" or self.headers):
            raise ValueError(f"snapshot {self.id}: browser captures are plain page loads (GET, no headers)")
        if self.ignore_keys and self.kind != "json":
            raise ValueError(f"snapshot {self.id}: ignore_keys applies to json snapshots only")
        if self.normalise and self.kind not in ("web_page", "terms"):
            raise ValueError(f"snapshot {self.id}: normalise visible_text applies to HTML snapshots only")
        return self


def load_snapshots(root: Path) -> list[Snapshot]:
    raw = yaml.safe_load((root / "registry" / "snapshots.yaml").read_text(encoding="utf-8"))
    snaps = [Snapshot(**s) for s in raw["snapshots"]]
    ids = [s.id for s in snaps]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate snapshot ids")
    return snaps


class MarketRule(_Strict):
    key: str = Field(pattern=r"^[a-z_]+(\.[a-z_0-9]+)*$")
    zone: str | None
    value: Any
    valid_from: date | None
    valid_to: date | None
    precision: Literal["day", "month"] = "day"
    source: str | None
    verified: bool
    note: str | None = None

    @model_validator(mode="after")
    def _order(self) -> MarketRule:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError(f"rule {self.key}: valid_to must be after valid_from")
        if self.verified and not self.source:
            raise ValueError(f"rule {self.key}: verified rules need a source")
        return self


class MarketRulesFile(_Strict):
    country: str
    owner: str
    rules: list[MarketRule]

    @model_validator(mode="after")
    def _no_overlap(self) -> MarketRulesFile:
        by_key: dict[tuple[str, str | None], list[MarketRule]] = {}
        for r in self.rules:
            by_key.setdefault((r.key, r.zone), []).append(r)
        for (key, zone), rules in by_key.items():
            spans = sorted(rules, key=lambda r: r.valid_from or date.min)
            for a, b in itertools.pairwise(spans):
                a_end = a.valid_to or date.max
                b_start = b.valid_from or date.min
                if b_start < a_end:
                    raise ValueError(f"{self.country}: overlapping rules for {key} zone={zone}")
        return self


def load_market_rules(root: Path) -> dict[str, MarketRulesFile]:
    out: dict[str, MarketRulesFile] = {}
    for path in sorted((root / "market_rules").glob("*.yaml")):
        f = MarketRulesFile(**yaml.safe_load(path.read_text()))
        out[f.country] = f
    return out


def rule_at(rules: MarketRulesFile, key: str, on: date, zone: str | None = None) -> MarketRule | None:
    """The rule in force for key on a date; zone-specific rules win over country-wide ones."""
    candidates = [
        r
        for r in rules.rules
        if r.key == key
        and r.zone in (zone, None)
        and (r.valid_from is None or r.valid_from <= on)
        and (r.valid_to is None or on < r.valid_to)
    ]
    candidates.sort(key=lambda r: r.zone is None)
    return candidates[0] if candidates else None


class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: Any = None
    proposed: Any = None
    note: str | None = None

    def available(self, allow_proposed: bool) -> bool:
        return self.value is not None or (allow_proposed and self.proposed is not None)


# Label carried by every output computed with at least one proposed (not approved) value.
PROPOSED_LABEL = "proposed assumptions (not approved)"


@dataclass(frozen=True)
class ResolvedAssumptions:
    """Parameter values taken from one assumption set, and which of them fell back to `proposed`."""

    set_id: str
    values: dict[str, Any]
    from_proposed: tuple[str, ...]

    @property
    def proposed_used(self) -> bool:
        return bool(self.from_proposed)


def resolve_parameters(
    set_id: str, owners: list[str], parameters: dict[str, Parameter], names: Iterable[str], *, allow_proposed: bool
) -> ResolvedAssumptions:
    """Values for the named parameters (assumptions/README.md).

    A null `value` falls back to `proposed` only when allow_proposed is set (callers pass
    Settings.allow_proposed_assumptions); the result lists those parameters so outputs can say so.
    Raises MissingAssumption listing every parameter still without a value, and the owners.
    """
    values: dict[str, Any] = {}
    proposed: list[str] = []
    missing: list[str] = []
    for n in names:
        p = parameters.get(n)
        if p is not None and p.value is not None:
            values[n] = p.value
        elif p is not None and p.available(allow_proposed):
            values[n] = p.proposed
            proposed.append(n)
        else:
            missing.append(n)
    if missing:
        raise MissingAssumption(set_id, missing, owners)
    return ResolvedAssumptions(set_id, values, tuple(proposed))


class AssumptionSet(_Strict):
    id: str
    kind: str
    status: Literal["proposed", "approved"]
    owners: list[str]
    approved_by: str | None
    approved_on: date | None
    sensitivity: Sensitivity
    origin: str
    parameters: dict[str, Parameter]

    @model_validator(mode="after")
    def _approval(self) -> AssumptionSet:
        if self.status == "approved" and not (self.approved_by and self.approved_on):
            raise ValueError(f"{self.id}: approved sets need approved_by and approved_on")
        return self

    def require(self, *names: str) -> dict[str, Any]:
        """Approved values for the named parameters; raises listing every missing one and the owners."""
        return self.resolve(*names, allow_proposed=False).values

    def resolve(self, *names: str, allow_proposed: bool) -> ResolvedAssumptions:
        """Values for the named parameters, falling back to `proposed` when allowed (resolve_parameters)."""
        return resolve_parameters(self.id, self.owners, self.parameters, names, allow_proposed=allow_proposed)


class MissingAssumption(RuntimeError):
    def __init__(self, set_id: str, names: list[str], owners: list[str]):
        super().__init__(f"assumption set {set_id} is missing {names}; owners: {owners}")
        self.set_id, self.names, self.owners = set_id, names, owners


SCENARIOS_DIR = "scenarios"


def assumption_paths(root: Path) -> list[Path]:
    """assumptions/**/*.yaml except the scenario files in assumptions/scenarios/."""
    base = root / "assumptions"
    return [p for p in sorted(base.rglob("*.yaml")) if p.relative_to(base).parts[0] != SCENARIOS_DIR]


def load_assumptions(root: Path) -> dict[str, AssumptionSet]:
    out: dict[str, AssumptionSet] = {}
    for path in assumption_paths(root):
        a = AssumptionSet(**yaml.safe_load(path.read_text(encoding="utf-8")))
        if path.stem != a.id:
            raise ValueError(f"{path}: file name must equal set id {a.id}")
        out[a.id] = a
    return out


SLUG_ID = r"^[a-z][a-z0-9_]*$"
PARAMETER_REF = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"  # <set role>.<parameter name>


class Scenario(_Strict):
    """An analytics scenario: assumptions/scenarios/<id>.yaml (assumptions/scenarios/README.md).

    It names the assumption set it uses for each role (`sets`, role -> set ID) and may replace parameter
    values of those sets: `method_switches` for categorical method choices (policies, input lists) and
    `overrides` for other values (hypotheses), both keyed `<role>.<parameter>`. Ingestion and normalisation
    rules never live here (codebook, manifests, dossiers). Values a scenario sets are approved only when the
    scenario is (approved_by and approved_on); otherwise they count as proposed values (Parameter.proposed).
    Exactly one scenario has status `official` (scanner.core.contracts.check_scenarios).
    """

    id: str = Field(pattern=SLUG_ID)
    title: str
    status: Literal["official", "exploratory"]
    description: str
    owners: list[str]
    approved_by: str | None = None
    approved_on: date | None = None
    sensitivity: Sensitivity = "public"
    sets: dict[str, str]
    method_switches: dict[str, Any] = {}
    overrides: dict[str, Any] = {}

    @model_validator(mode="after")
    def _check(self) -> Scenario:
        if bool(self.approved_by) != bool(self.approved_on):
            raise ValueError(f"scenario {self.id}: approved_by and approved_on go together")
        for role in self.sets:
            if not re.match(SLUG_ID, role):
                raise ValueError(f"scenario {self.id}: role {role!r} is not a lower-case identifier")
        for key in [*self.method_switches, *self.overrides]:
            if not re.match(PARAMETER_REF, key):
                raise ValueError(f"scenario {self.id}: {key!r} must have the form <role>.<parameter>")
            if key.split(".")[0] not in self.sets:
                raise ValueError(f"scenario {self.id}: {key} names role {key.split('.')[0]}, which has no set")
        if both := set(self.method_switches) & set(self.overrides):
            raise ValueError(f"scenario {self.id}: {sorted(both)} are both method switches and overrides")
        return self

    @property
    def approved(self) -> bool:
        return self.approved_by is not None

    def values_for(self, role: str) -> dict[str, Any]:
        """The parameter values this scenario sets for one role: method switches and overrides."""
        prefix = f"{role}."
        merged = {**self.method_switches, **self.overrides}
        return {k.removeprefix(prefix): v for k, v in merged.items() if k.startswith(prefix)}

    def parameters(self, role: str, parameters: dict[str, Parameter]) -> tuple[dict[str, Parameter], tuple[str, ...]]:
        """The role's set parameters with this scenario's values applied, and the names it replaced.

        A replaced value is a `value` if the scenario is approved, else a `proposed` value (so outputs that use
        it need SCANNER_ALLOW_PROPOSED_ASSUMPTIONS and carry the proposed label). ValueError for a name the set
        does not declare.
        """
        own = self.values_for(role)
        if unknown := sorted(set(own) - set(parameters)):
            raise ValueError(f"scenario {self.id}: set {self.sets.get(role)} has no parameters {unknown}")
        note = f"set by scenario {self.id}"
        replaced = {
            k: Parameter(value=v, note=note) if self.approved else Parameter(proposed=v, note=note)
            for k, v in own.items()
        }
        return {**parameters, **replaced}, tuple(sorted(own))


def load_scenarios(root: Path) -> dict[str, Scenario]:
    out: dict[str, Scenario] = {}
    for path in sorted((root / "assumptions" / SCENARIOS_DIR).glob("*.yaml")):
        s = Scenario(**yaml.safe_load(path.read_text(encoding="utf-8")))
        if path.stem != s.id:
            raise ValueError(f"{path}: file name must equal scenario id {s.id}")
        out[s.id] = s
    return out


class Benchmark(_Strict):
    id: str
    entity: str | None
    zone: str | None
    description: str
    expected: dict[str, Any]
    owner: str


def load_benchmarks(root: Path) -> list[Benchmark]:
    out: list[Benchmark] = []
    for path in sorted((root / "benchmarks").glob("*.yaml")):
        out.extend(Benchmark(**b) for b in yaml.safe_load(path.read_text())["benchmarks"])
    return out


class ClaimDocument(_Strict):
    """The captured document claims cite; becomes a core.source_document row."""

    id: str
    source: str
    url: str | None
    title: str | None
    fetched_at: AwareDatetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_uri: str
    content_type: str
    sensitivity: Sensitivity = "public"


class Claim(_Strict):
    """A figure taken from a document, with its citation (ADR 0009, claims/README.md)."""

    id: str
    attribute: str
    entity: str | None
    value: float | str
    unit: str | None
    valid_from: date | None = None
    valid_to: date | None = None
    quote: str | None = None
    page: int | None = Field(default=None, ge=0)
    location: dict[str, Any] | None = None
    method: Literal["parser", "llm", "manual"]
    model: str | None = None
    prompt_hash: str | None = None
    status: Literal["auto", "pending_review", "approved", "rejected"]
    reviewed_by: str | None = None
    reviewed_on: date | None = None
    sensitivity: Sensitivity = "public"

    @model_validator(mode="after")
    def _check(self) -> Claim:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError(f"claim {self.id}: valid_to must be after valid_from")
        if self.method == "llm" and not (self.model and self.prompt_hash):
            raise ValueError(f"claim {self.id}: llm claims need model and prompt_hash (ADR 0009)")
        if self.status == "approved" and not self.reviewed_by:
            raise ValueError(f"claim {self.id}: approved claims need reviewed_by")
        return self


class ClaimsFile(_Strict):
    """claims/<cc>/<family>.yaml: one document and the claims taken from it."""

    document: ClaimDocument
    claims: list[Claim]

    @model_validator(mode="after")
    def _unique(self) -> ClaimsFile:
        ids = [c.id for c in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.document.id}: duplicate claim ids")
        return self
