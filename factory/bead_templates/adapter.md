# Bead: adapter

Type: adapter. Risk tier: low | medium. Blocks: <ids>. Blocked by: <dossier approval bead>.

## Goal
Ingest <dataset> from <source> as described in `docs/sources/<cc>/<source>.md` (approved on <date>).

## Inputs available in the sandbox
- Approved dossier and codebook file `codebook/<cc>/<source>.<dataset>.yaml`
- Recorded fixtures: `tests/fixtures/<source>/<dataset>__*.json` (list them)

## Expected files
- `src/scanner/adapters/<source>/adapter.py` (new or extended)
- `src/scanner/adapters/<source>/manifests/<dataset>.yaml`
- `tests/test_<source>_<dataset>.py`

## Acceptance criteria
- Parses every fixture; interval counts per local day match `expected_intervals`, including DST days
- No imputation, no unit conversion, explicit nulls kept; unexpected content raises ParseError
- Series keys and issue times follow the codebook entries
- Full history backfills through `scanner.cli ingest` within the source's limits (rate limits, maximum query span); a test with a mocked transport shows the chunking or reuse, and the dossier's "Backfill" section records request count and expected duration
- `make contracts` and `make test` pass

## Verify
`make contracts && uv run pytest tests/test_<source>_<dataset>.py -q`

## Out of scope
Calling the live API. Editing codebook, registry or market rules (file a `human` bead instead).
