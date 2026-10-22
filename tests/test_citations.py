from __future__ import annotations

import pytest

from scanner.extraction.citations import ExtractedValue, parse_number, verify

PAGE = {3: "Nel 2025 sono stati tagliati circa\n640 GWh di eolico e 1.234,5 MWh in Sicilia."}


def test_valid_citation_passes() -> None:
    v = [
        ExtractedValue("a", 640.0, "circa 640 GWh di eolico", "640", 3),
        ExtractedValue("b", 1234.5, "1.234,5 MWh in Sicilia", "1.234,5", 3, decimal=","),
    ]
    assert verify(v, PAGE) == []


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (ExtractedValue("c", 650.0, "circa 640 GWh", "640", 3), "value 650.0 != printed 640.0"),
        (ExtractedValue("d", 700.0, "circa 700 GWh", "700", 3), "quote not found verbatim on cited page"),
        (ExtractedValue("e", 640.0, "circa 640 GWh", "640", 9), "page 9 not in document"),
    ],
)
def test_invented_or_misplaced_values_fail(value: ExtractedValue, reason: str) -> None:
    assert [f.reason for f in verify([value], PAGE)] == [reason]


def test_parse_number_locales() -> None:
    assert parse_number("1,234.5") == 1234.5
    assert parse_number("1.234,5", ",") == 1234.5
    assert parse_number("− 12") == -12.0
    with pytest.raises(ValueError):
        parse_number("12 %")
