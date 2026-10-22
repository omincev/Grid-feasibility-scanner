"""Citation check (ADR 0009): an extracted value must appear verbatim on the page it cites.

This is the deterministic guard against invented numbers. It does not need a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class ExtractedValue:
    claim_id: str
    value: float
    quote: str  # verbatim text from the page that contains the number
    number_text: str  # the number exactly as printed, e.g. "1.234,5" or "640"
    page: int  # 0-based
    decimal: str = "."  # "." or "," as used in the document


@dataclass(frozen=True)
class CitationFailure:
    claim_id: str
    reason: str


def normalise(text: str) -> str:
    return _WS.sub(" ", text).strip()


def parse_number(text: str, decimal: str = ".") -> float:
    """Parse a printed number using the document's decimal separator. Thousands separators are removed."""
    if decimal not in (".", ","):
        raise ValueError("decimal must be '.' or ','")
    thousands = "," if decimal == "." else "."
    s = text.strip().replace("−", "-").replace(" ", "").replace(" ", "")
    s = s.replace(thousands, "")
    if decimal == ",":
        s = s.replace(",", ".")
    if not re.fullmatch(r"[-+]?\d+(\.\d+)?", s):
        raise ValueError(f"not a plain number: {text!r}")
    return float(s)


def verify(values: list[ExtractedValue], page_texts: dict[int, str], rel_tol: float = 1e-9) -> list[CitationFailure]:
    failures: list[CitationFailure] = []
    pages = {k: normalise(v) for k, v in page_texts.items()}
    for v in values:
        page = pages.get(v.page)
        if page is None:
            failures.append(CitationFailure(v.claim_id, f"page {v.page} not in document"))
            continue
        quote = normalise(v.quote)
        if quote not in page:
            failures.append(CitationFailure(v.claim_id, "quote not found verbatim on cited page"))
            continue
        if v.number_text not in quote:
            failures.append(CitationFailure(v.claim_id, "number_text not inside quote"))
            continue
        try:
            parsed = parse_number(v.number_text, v.decimal)
        except ValueError as exc:
            failures.append(CitationFailure(v.claim_id, str(exc)))
            continue
        if abs(parsed - v.value) > rel_tol * max(1.0, abs(v.value)):
            failures.append(CitationFailure(v.claim_id, f"value {v.value} != printed {parsed}"))
    return failures
