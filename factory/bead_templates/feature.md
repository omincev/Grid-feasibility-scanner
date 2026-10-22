# Bead: feature | estimate | valuation

Risk tier: high. Released only when the spec section is signed off (H7).

## Spec
`docs/features.md#<anchor>` or `docs/valuation.md#<anchor>` at commit <sha>.

## Acceptance criteria
- Reads only accepted equivalence classes (`scanner.features.registry`)
- Writes run ID, code version and combined sensitivity on every row
- Unit tests on hand-computed examples; benchmark checks run and are reported
- Refuses to run on null assumptions and names the owners

## Verify
`make contracts && make test`
