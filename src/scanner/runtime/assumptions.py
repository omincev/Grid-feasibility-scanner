"""Assumption sets as derived outputs use them: gated values plus the provenance written with each output.

Values are read from ref.assumption_set, the loaded copy of assumptions/*.yaml, so the content hash
recorded with an output is the hash of exactly the file version whose values it used (and the
foreign keys on feat.score, feat.opportunity and val.run resolve). Run `load-curated` after editing a set.

Every output records, per set: its ID, content hash, whether proposed values were used and which. If any
were, the output also carries scanner.core.registry.PROPOSED_LABEL.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg

from scanner.core.registry import PROPOSED_LABEL, Parameter, ResolvedAssumptions, resolve_parameters
from scanner.core.sensitivity import Sensitivity


class AssumptionSetNotLoaded(LookupError):
    def __init__(self, set_id: str):
        super().__init__(f"assumption set {set_id} is not in ref.assumption_set; run load-curated")
        self.set_id = set_id


@dataclass(frozen=True)
class UsedAssumptions:
    resolved: ResolvedAssumptions
    content_hash: str
    sensitivity: Sensitivity
    role: str | None = None  # the scenario role the set was used for (scanner.runtime.scenarios)
    scenario_parameters: tuple[str, ...] = ()  # parameters whose value the scenario replaced

    @property
    def set_id(self) -> str:
        return self.resolved.set_id

    @property
    def values(self) -> dict[str, Any]:
        return self.resolved.values

    def record(self) -> dict[str, Any]:
        out = {
            "set_id": self.set_id,
            "content_hash": self.content_hash,
            "proposed_used": self.resolved.proposed_used,
            "proposed_parameters": list(self.resolved.from_proposed),
        }
        if self.role is not None:
            out |= {"role": self.role, "scenario_parameters": list(self.scenario_parameters)}
        return out


def load_set(conn: psycopg.Connection[Any], set_id: str) -> tuple[list[str], dict[str, Parameter], str, Sensitivity]:
    """Owners, parameters, content hash and sensitivity of a loaded set; AssumptionSetNotLoaded if absent."""
    row = conn.execute(
        "select owners, parameters, content_hash, sensitivity::text from ref.assumption_set where set_id = %s",
        (set_id,),
    ).fetchone()
    if row is None:
        raise AssumptionSetNotLoaded(set_id)
    owners, raw, content_hash, sensitivity = row
    return list(owners), {k: Parameter.model_validate(v) for k, v in raw.items()}, content_hash, sensitivity


def resolve_used(
    set_id: str,
    owners: list[str],
    params: dict[str, Parameter],
    names: Iterable[str],
    *,
    allow_proposed: bool,
    optional: Iterable[str] = (),
) -> ResolvedAssumptions:
    """`names` (required) and those of `optional` that have a usable value."""
    wanted = [*names, *(n for n in optional if n in params and params[n].available(allow_proposed))]
    return resolve_parameters(set_id, owners, params, wanted, allow_proposed=allow_proposed)


def use_assumptions(
    conn: psycopg.Connection[Any],
    set_id: str,
    names: Iterable[str],
    *,
    allow_proposed: bool,
    optional: Iterable[str] = (),
) -> UsedAssumptions:
    """Resolve `names` (required) and those of `optional` that have a usable value from the loaded set.

    Raises AssumptionSetNotLoaded, or MissingAssumption naming the parameters and owners.
    """
    owners, params, content_hash, sensitivity = load_set(conn, set_id)
    resolved = resolve_used(set_id, owners, params, names, allow_proposed=allow_proposed, optional=optional)
    return UsedAssumptions(resolved, content_hash, sensitivity)


def provenance(*used: UsedAssumptions) -> dict[str, Any]:
    """The assumption fields written with an output (JSON): sets with hashes, proposed flag, labels."""
    proposed = any(u.resolved.proposed_used for u in used)
    return {
        "assumption_sets": [u.record() for u in used],
        "proposed_assumptions": proposed,
        "labels": [PROPOSED_LABEL] if proposed else [],
    }
