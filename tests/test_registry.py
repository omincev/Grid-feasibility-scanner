from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scanner.core.registry import MissingAssumption, load_assumptions, load_market_rules, rule_at


def test_market_rule_lookup(root: Path) -> None:
    dk = load_market_rules(root)["dk"]
    assert rule_at(dk, "day_ahead.resolution", date(2025, 9, 30)).value == "PT1H"  # type: ignore[union-attr]
    assert rule_at(dk, "day_ahead.resolution", date(2025, 10, 1)).value == "PT15M"  # type: ignore[union-attr]
    basis = rule_at(dk, "imbalance.dominating_direction_basis", date(2026, 3, 4))
    assert basis is not None and basis.value == "satisfied_demand"


def test_missing_assumptions_name_owners(root: Path) -> None:
    bess = load_assumptions(root)["bess_mvp"]
    with pytest.raises(MissingAssumption) as exc:
        bess.require("power_mw", "duration_h", "charge_efficiency")
    assert exc.value.names == ["charge_efficiency"]
    assert exc.value.owners == ["oleg"]
