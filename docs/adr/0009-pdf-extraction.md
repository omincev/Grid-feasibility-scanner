# ADR 0009: PDFs are parsed first, LLM-extracted as a checked fallback

Status: accepted, 2026-10-17.

## Context

Some data exists only in PDFs that are reissued on a schedule (regulator reports, queue statistics, curtailment summaries), and their layouts change between editions. A fixed parser breaks on every layout change; an LLM alone can invent numbers.

## Decision

When the snapshot watcher sees a new edition of a watched PDF, the runtime runs this sequence:

1. Run the deterministic parser for the last known layout (`src/scanner/extraction/parsers/<doc_family>.py`).
2. Validate the output: citation check (every value appears verbatim in the text of the cited page; OCR text for scans), internal consistency (row sums equal totals, units match headers), overlap with the previous edition (repeated historical figures must match stored values), and range checks from the codebook.
3. If the parser fails or validation fails, call the LLM extractor with the pinned prompt in `factory/prompts/pdf_extraction.md` and the same output schema, then run the same validation.
4. Parser output that passes validation is loaded as claims with status `auto`. LLM output that passes validation is loaded with status `pending_review` and is invisible to serving views until the domain owner approves it. Output that fails validation is not loaded.
5. On any fallback, file a `parser-fix` bead so the factory updates the parser for the new layout.

For high-stakes document families, the manifest can require dual extraction (two models, or parser plus model); only values on which both agree are queued.

Every claim records page, location, extraction method, model ID and prompt hash.

## Consequences

LLM extraction never reaches users unchecked. Layout changes cost one review and one parser fix, not a broken pipeline. The citation check catches invented numbers without needing a second model.
