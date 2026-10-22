# Bead: parser-fix

Type: parser-fix. Risk tier: medium. Created after an LLM fallback in the PDF pipeline (ADR 0009).

## Inputs
- New edition: <raw_uri>, family <family>
- Approved values for this edition: `claims/<cc>/<family>.yaml` (status approved)
- Current parser: `src/scanner/extraction/parsers/<family>.py`

## Acceptance criteria
- The parser reproduces every approved value on the new edition, with citations that pass `extraction.citations.verify`
- It still reproduces the previous edition's values (fixture kept)

## Verify
`uv run pytest tests/extraction/test_<family>.py -q`
