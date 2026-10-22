from __future__ import annotations

from pathlib import Path

import pytest

from scanner.core.contracts import check_eic, eic_check_character, run_all


def test_repository_contracts_hold(root: Path) -> None:
    results = run_all(root)
    errors = {k: v for k, v in results.items() if v}
    assert not errors, errors


@pytest.mark.parametrize(
    "eic",
    # Worked example from the EIC Data Exchange IG, then bidding zones from ENTSO-E's EIC registry.
    ["11XRWENET12345-2", "10YDK-1--------W", "10YDK-2--------M", "10YNL----------L", "10Y1001C--00096J"],
)
def test_eic_check_character(eic: str) -> None:
    assert eic_check_character(eic[:15]) == eic[15]


def _country(root: Path, zones: str) -> None:
    (root / "registry" / "countries").mkdir(parents=True)
    (root / "registry" / "countries" / "xx.yaml").write_text(
        "code: xx\nname: Test\nstatus: draft\ntimezone: UTC\ncurrency: EUR\nreporting_currency: EUR\n"
        f"tso: null\ngeography_levels: [zone]\nzones:\n{zones}precedence: {{}}\nreference_zones: []\n",
        encoding="utf-8",
    )


def test_check_eic_rejects_typos_duplicates_and_unbacked_verification(tmp_path: Path) -> None:
    _country(
        tmp_path,
        "  - {id: xx.zone.a, short: A, name: A, eic: 10YDK-1--------X, eic_verified: true}\n"
        "  - {id: xx.zone.b, short: B, name: B, eic: 10YNL----------L, eic_verified: true}\n"
        "  - {id: xx.zone.c, short: C, name: C, eic: 10YNL----------L, eic_verified: true}\n"
        "  - {id: xx.zone.d, short: D, name: D, eic: null, eic_verified: true}\n"
        "  - {id: xx.zone.e, short: E, name: E, eic: dk1, eic_verified: false}\n",
    )
    errors = check_eic(tmp_path)
    assert len(errors) == 4
    assert any("xx.zone.a" in e and "check character" in e for e in errors)
    assert any("xx.zone.c" in e and "also used by xx.zone.b" in e for e in errors)
    assert any("xx.zone.d" in e for e in errors)
    assert any("xx.zone.e" in e for e in errors)
