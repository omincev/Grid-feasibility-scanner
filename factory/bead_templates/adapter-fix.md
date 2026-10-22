# Bead: adapter-fix

Type: adapter-fix. Risk tier: low. Created from `ops.bead_outbox` entry <id>.

## Failure
<deviation lines from the outbox entry>

## Evidence
Raw capture: <raw_uri> (copied into `tests/fixtures/<source>/<dataset>__regression_<date>.json` by the supervisor; public data only).

## Acceptance criteria
- A new test reproduces the failure on the regression fixture and passes after the fix
- All existing fixture tests still pass
- If the source changed meaning (not just format), stop and file a `human` bead for the codebook owner

## Verify
`make contracts && make test`
