"""Scenarios as runs use them (assumptions/scenarios/README.md).

Score, opportunity and valuation runs take a scenario ID (default: the official scenario) and resolve each
parameter through it: the scenario names the set for a role, its method switches and overrides replace
values of that set (scanner.core.registry.Scenario.parameters), and the set's approval rules apply as
before (scanner.runtime.assumptions). Scenarios and sets are read from ref.scenario and ref.assumption_set,
the loaded copies of the files, so the hashes an output records are those of exactly the versions whose
values it used; run `load-curated` after editing.

Every row a run writes to feat.score, feat.opportunity, val.run and val.result carries OutputColumns:
scenario_id, scenario_hash, assumption_hashes ({set_id: content hash}), method_id, method_version and
method_versions ({method_id: version} of the row's method and the methods upstream of it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from scanner.core.methods import MethodRef
from scanner.core.registry import Scenario
from scanner.core.sensitivity import Sensitivity, combine
from scanner.runtime.assumptions import UsedAssumptions, load_set, provenance, resolve_used


class ScenarioNotLoaded(LookupError):
    def __init__(self, scenario_id: str | None):
        what = f"scenario {scenario_id} is" if scenario_id else "no official scenario is"
        super().__init__(f"{what} not in ref.scenario; run load-curated")
        self.scenario_id = scenario_id


@dataclass(frozen=True)
class LoadedScenario:
    scenario: Scenario
    content_hash: str

    @property
    def id(self) -> str:
        return self.scenario.id

    @property
    def sensitivity(self) -> Sensitivity:
        return self.scenario.sensitivity

    def set_id(self, role: str) -> str:
        try:
            return self.scenario.sets[role]
        except KeyError:
            raise ValueError(f"scenario {self.id} names no assumption set for role {role!r}") from None

    def use(
        self,
        conn: psycopg.Connection[Any],
        role: str,
        names: Iterable[str],
        *,
        allow_proposed: bool,
        optional: Iterable[str] = (),
    ) -> UsedAssumptions:
        """The role's set resolved with this scenario's values (required `names`, usable `optional` ones).

        Raises AssumptionSetNotLoaded, or MissingAssumption naming the parameters and owners.
        """
        set_id = self.set_id(role)
        owners, params, content_hash, sensitivity = load_set(conn, set_id)
        params, replaced = self.scenario.parameters(role, params)
        resolved = resolve_used(set_id, owners, params, names, allow_proposed=allow_proposed, optional=optional)
        used = tuple(n for n in replaced if n in resolved.values)
        return UsedAssumptions(resolved, content_hash, combine(sensitivity, self.sensitivity), role, used)

    def record(self) -> dict[str, Any]:
        return {
            "scenario_id": self.id,
            "content_hash": self.content_hash,
            "status": self.scenario.status,
            "approved": self.scenario.approved,
        }


def load_scenario(conn: psycopg.Connection[Any], scenario_id: str | None = None) -> LoadedScenario:
    """A loaded scenario, by default the official one; ScenarioNotLoaded if it is not in ref.scenario."""
    row = conn.execute(
        "select scenario_id, title, status, description, owners, approved_by, approved_on, sensitivity::text, "
        "sets, method_switches, overrides, content_hash from ref.scenario "
        "where (%(id)s::text is null and status = 'official') or scenario_id = %(id)s::text",
        {"id": scenario_id},
    ).fetchone()
    if row is None:
        raise ScenarioNotLoaded(scenario_id)
    sid, title, status, description, owners, approved_by, approved_on, sensitivity, sets, switches, over, digest = row
    scenario = Scenario(
        id=sid,
        title=title,
        status=status,
        description=description,
        owners=list(owners),
        approved_by=approved_by,
        approved_on=approved_on,
        sensitivity=sensitivity,
        sets=sets,
        method_switches=switches,
        overrides=over,
    )
    return LoadedScenario(scenario, digest)


def scenario_ids(conn: psycopg.Connection[Any]) -> list[str]:
    """Every loaded scenario, the official one first."""
    rows = conn.execute("select scenario_id from ref.scenario order by status <> 'official', scenario_id")
    return [r[0] for r in rows]


@dataclass(frozen=True)
class OutputColumns:
    """The scenario and method columns of one output row."""

    scenario_id: str
    scenario_hash: str
    assumption_hashes: dict[str, str]
    method_id: str
    method_version: int
    method_versions: dict[str, int | None] = field(default_factory=dict)

    def values(self) -> tuple[Any, ...]:
        """In the order of COLUMNS."""
        return (
            self.scenario_id,
            self.scenario_hash,
            Jsonb(self.assumption_hashes),
            self.method_id,
            str(self.method_version),
            Jsonb(self.method_versions),
        )


COLUMNS = "scenario_id, scenario_hash, assumption_hashes, method_id, method_version, method_versions"


def output_columns(
    sc: LoadedScenario,
    used: Sequence[UsedAssumptions],
    method: MethodRef,
    upstream: Mapping[str, int | None] | None = None,
) -> OutputColumns:
    return OutputColumns(
        sc.id,
        sc.content_hash,
        {u.set_id: u.content_hash for u in used},
        method.id,
        method.version,
        {**(upstream or {}), method.id: method.version},
    )


def scenario_provenance(sc: LoadedScenario, *used: UsedAssumptions) -> dict[str, Any]:
    """The assumption fields written with an output (scanner.runtime.assumptions.provenance) plus the scenario."""
    return {**provenance(*used), "scenario": sc.record()}
