"""PDF extraction pipeline (ADR 0009, WP4.7). Skeleton with the contract; parsers and the LLM client
are implemented per document family.

Order: parser for the last known layout -> validation -> LLM fallback -> validation -> load as claims
(parser output 'auto', LLM output 'pending_review') -> parser-fix bead on any fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from scanner.extraction.citations import CitationFailure, ExtractedValue, verify


@dataclass(frozen=True)
class Document:
    document_id: str
    family: str
    page_texts: dict[int, str]
    raw_uri: str
    sha256: str


@dataclass
class Extraction:
    method: Literal["parser", "llm"]
    values: list[ExtractedValue]
    model: str | None = None
    prompt_hash: str | None = None
    failures: list[str] = field(default_factory=list)


class Parser(Protocol):
    family: str

    def parse(self, doc: Document) -> list[ExtractedValue]: ...


class LlmExtractor(Protocol):
    model: str
    prompt_hash: str

    def extract(self, doc: Document) -> list[ExtractedValue]: ...


class Validator(Protocol):
    """Family-specific checks beyond citations: row sums, units, overlap with the previous edition, ranges."""

    def check(self, doc: Document, values: list[ExtractedValue]) -> list[str]: ...


def validate(doc: Document, values: list[ExtractedValue], validators: list[Validator]) -> list[str]:
    failures: list[CitationFailure] = verify(values, doc.page_texts)
    out = [f"{f.claim_id}: {f.reason}" for f in failures]
    for v in validators:
        out.extend(v.check(doc, values))
    if not values:
        out.append("no values extracted")
    return out


def run(
    doc: Document, parser: Parser | None, llm: LlmExtractor | None, validators: list[Validator]
) -> Extraction | None:
    """Return the first extraction that passes validation, or None. Callers load claims and file beads."""
    if parser is not None:
        try:
            values = parser.parse(doc)
            failures = validate(doc, values, validators)
        except Exception as exc:  # layout changed: fall through to the LLM
            values, failures = [], [f"parser error: {type(exc).__name__}: {exc}"]
        if not failures:
            return Extraction("parser", values)
    if llm is not None:
        values = llm.extract(doc)
        failures = validate(doc, values, validators)
        if not failures:
            return Extraction("llm", values, model=llm.model, prompt_hash=llm.prompt_hash)
    return None
