# ADR 0014: Domain owners sign off through CODEOWNERS

Status: accepted, 2026-10-17.

## Context

Codebook entries, market rules, entity matches, extracted claims, assumption sets and valuation logic encode domain judgement. The people with that judgement differ by country and topic, and review capacity is the scarcest resource in the system.

## Decision

All curated material lives as files in git (ADR 0006), and `.github/CODEOWNERS` maps paths to owners. Branch protection on `main` requires approval from the owners of every touched path. Review is tiered by risk: adding an ENTSO-E zone to a country file needs one owner glance; a change under `src/scanner/valuation/` or `assumptions/` needs the valuation owner and Misi. The PR template asks the author to state the risk tier.

Claims and LLM PDF output that arrive as database rows in `pending_review` are approved through the same path: the supervisor writes them to `claims/` as a PR.

## Consequences

Every judgement has a named approver and a date in git history. Non-technical owners review on GitHub; if that proves a barrier, a review screen in the front end can write approvals back as commits.
