# The factory

Status: design for Phase 4; not built yet. No `bd` queue, supervisor or worker sandbox exists in the repository today, and the runtime's outbox entries (`ops.bead_outbox`) are not drained anywhere. Until then, work is done by people with Claude Code sessions and subagents in git worktrees.

The factory builds and maintains the runtime's code. It is a light factory: humans own intent and approve every merge; agents draft plans, write code, research sources and extract evidence. Its design follows the patterns that hold up in published production systems (Stripe Minions, OpenAI Symphony, Vercel's ai-sdk-factory, Cloudflare's triage factory): a durable work ledger, a scheduler that claims items, disposable single-task workers in isolated sandboxes, deterministic gates around agent steps, and a human merge.

## Components

| Component | Implementation | Location |
|---|---|---|
| Work ledger | beads (`bd`), git-backed, stealth mode, pinned version | `.beads/` |
| Intake | intent documents and the runtime's bead outbox | `factory/intents/`, `ops.bead_outbox` |
| Planner | interactive Claude Code session producing a draft bead graph | run by a human |
| Supervisor | Python loop: claim, worktree, sandbox, run, gate, PR | `factory/supervisor/` |
| Coding worker | headless Claude Code or Agent SDK, one per bead | `factory/sandbox/` image |
| Research worker | same runtime plus Playwright, browsing allowlist, no push | `factory/sandbox/` research image |
| Gates | ruff, mypy, pytest, `make contracts`, holdout tests in CI | `.github/workflows/ci.yml` |
| Review | GitHub PR, CODEOWNERS, branch protection | `.github/` |

## Bead lifecycle

1. Created as `draft` by the planner, the outbox drain, or a worker filing a `discovered-from` follow-up.
2. A human edits and labels it `approved`. Only approved, unblocked beads appear to the supervisor.
3. The supervisor claims it, creates a worktree from `main`, mounts fixtures, starts a worker with the bead text and the matching template from `factory/bead_templates/`.
4. Deterministic nodes run before and after the agent: install, lint autofix, tests, contracts. The agent fills only "implement" and "fix failures".
5. At most two CI rounds. On a third failure the bead goes back to `human` with the log.
6. On success the supervisor opens the PR with the bead ID, risk tier and verify output. The worker never pushes.
7. Merge closes the bead.

A bead is agent-ready only if it has acceptance criteria, a verify command, the expected files, the fixtures it needs, and its `blocks` edges. The templates enforce this.

## Work types

- `adapter`: new source or dataset from an approved dossier.
- `adapter-fix`: runtime failure; the failing raw capture is attached as the new fixture.
- `parser-fix`: PDF layout change after an LLM fallback.
- `research-source`: produce a dossier for a source or country.
- `research-evidence`: extract claims from documents with citations.
- `feature`, `estimate`, `valuation`: only from a signed-off spec section.
- `human`: questions for a named owner; blocks dependent beads.

Small fixes (dependency bumps, flaky tests, typo-level adapter changes) skip the planner and go straight to `approved` by Misi.

## Research layer

Source discovery answers: where does the data live, in what format, how often does it update, under what licence, how do we get it (API, ArcGIS FeatureServer, Power BI embed, PDF, browser-only, phone call), and is it measured or must it be estimated. Output is a dossier from `docs/sources/_TEMPLATE.md`. Before using a browser, the worker inspects network requests for a JSON endpoint behind the page; ArcGIS maps usually have one.

Evidence extraction reads documents and writes claims with page-level citations into `claims/<cc>/`. Claims go through domain-owner review (H5).

LLM-driven browsing is for discovery and one-off extraction. Anything that must run on a schedule is codified as a deterministic Playwright script in the adapter before it enters the runtime.

Research workers run in their own sandbox with a per-bead browsing allowlist, no repository push rights, no database access and no secrets. Their output is files; the supervisor turns them into a PR.

## PDF pipeline

Runtime side: ADR 0009. Factory side: `parser-fix` beads carry the new edition, the LLM output that passed validation, and the old parser; the worker updates the parser until it reproduces the approved values.

## Models and cost

Model IDs are pinned in `factory/config.yaml`. Planner and valuation-related beads use the strongest available model; adapters and fixes use the standard tier; classification and summarisation steps use the cheapest tier. Base prompts are identical across runs so prompt caching applies. Hard caps per bead: wall-clock timeout, token budget, two CI rounds. Track cost per merged PR.

## Observability and evals

Workers export OpenTelemetry traces (Claude Code's built-in exporter) to a self-hosted collector. Factory evals per `docs/quality.md`. The supervisor writes a run record per bead: model, tokens, cost, CI rounds, outcome.

## What the factory does not do

It does not design the canonical schema, decide time conventions, approve entity matches, set score weights or valuation assumptions, or merge. Those stay with people.
