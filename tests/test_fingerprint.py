from __future__ import annotations

from scanner.core.fingerprint import Baseline, Thresholds, compare, compute, severity


def _recs(n: int, price: float = 50.0) -> list[dict[str, object]]:
    return [{"TimeUTC": f"t{i}", "PriceArea": "DK1", "Price": price + i % 7} for i in range(n)]


def test_clean_run_after_warmup() -> None:
    history = [compute(status=200, content_type="json", size=1, records=_recs(96), rows_expected=96) for _ in range(15)]
    fp = compute(status=200, content_type="json", size=1, records=_recs(96), rows_expected=96)
    assert compare(fp, Baseline.from_history(history)) == []


def test_hard_on_status_and_schema() -> None:
    base = Baseline.from_history(
        [compute(status=200, content_type="json", size=1, records=_recs(96), rows_expected=96)]
    )
    fp = compute(status=403, content_type="html", size=1, records=[], rows_expected=96)
    assert severity(compare(fp, base)) == "hard"
    renamed = [{"TimeUTC": "t", "PriceArea": "DK1", "PriceEUR": 1.0}]
    fp2 = compute(status=200, content_type="json", size=1, records=renamed, rows_expected=1)
    details = [d.detail for d in compare(fp2, base)]
    assert "field added: PriceEUR" in details and "field removed: Price" in details


def test_extreme_values_are_soft_never_hard() -> None:
    history = [compute(status=200, content_type="json", size=1, records=_recs(96), rows_expected=96) for _ in range(15)]
    spike = compute(status=200, content_type="json", size=1, records=_recs(96, price=4000.0), rows_expected=96)
    devs = compare(spike, Baseline.from_history(history))
    assert devs and severity(devs) == "soft"


def test_soft_checks_wait_for_warmup() -> None:
    history = [compute(status=200, content_type="json", size=1, records=_recs(96), rows_expected=96) for _ in range(3)]
    spike = compute(status=200, content_type="json", size=1, records=_recs(96, price=4000.0), rows_expected=96)
    assert compare(spike, Baseline.from_history(history), Thresholds(warmup_runs=14)) == []


def test_row_count_mismatch_soft_zero_rows_hard() -> None:
    base = Baseline()
    assert (
        severity(compare(compute(status=200, content_type="j", size=1, records=_recs(92), rows_expected=96), base))
        == "soft"
    )
    assert (
        severity(compare(compute(status=200, content_type="j", size=1, records=[], rows_expected=96), base)) == "hard"
    )
