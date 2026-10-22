# Supervisor (WP4.2)

A small Python loop, built in Phase 4. Behaviour:

1. Drain `ops.bead_outbox` into `bd` as `draft` beads (read-only database role limited to `ops.bead_outbox`), using the cheap model only to write a title and summary from public fields.
2. Poll `bd ready` for beads labelled `approved`.
3. For each, up to `max_parallel_workers`: create a git worktree from `main`, copy the bead's fixtures (public only), start the worker image from `factory/sandbox/` with the bead text, the matching template and `factory/prompts/worker_system.md`.
4. Run deterministic steps around the agent: `uv sync`, `ruff check --fix`, `make contracts`, `make test`. The agent only implements and fixes failures.
5. Enforce caps from `factory/config.yaml`. On the third CI failure, relabel the bead `human` with logs.
6. On success, push the branch with the supervisor's own credentials and open the PR with bead ID, risk tier and verify output.
7. Record a run row (model, tokens, cost, CI rounds, outcome) for factory evals.

The supervisor holds git and model credentials; workers never do. It runs on a separate machine from the production runtime.
