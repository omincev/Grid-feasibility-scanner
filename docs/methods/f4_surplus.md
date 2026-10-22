---
id: f4_surplus
version: 1
title: F4 Renewable surplus index
status: provisional
owner: [oleg, michael]
inputs:
  - generation_actual.solar
  - generation_actual.wind_onshore
  - generation_actual.wind_offshore
  - consumption_actual
parameters: []
outputs: [f4_surplus_index]
known_biases:
  - Ignores exchange with neighbouring zones; a surplus that is exported still counts as surplus.
  - Intervals with consumption at or below zero are left out (the ratio is undefined there).
---

Specification: `docs/features.md`, "F4 Renewable surplus index". Code: `scanner.features.zone.f4_surplus_index`, run by `scanner.features.run`.

**What it measures.** How much wind and solar production exceeds (or falls short of) consumption in a zone.

**Formula.** Per interval, (wind onshore + wind offshore + solar − consumption) ÷ consumption, at the coarsest resolution of the inputs. Over the window: the mean (weighted by interval length; the statistic the scores read) and the share of intervals above 0. Also per local day (`daily_mean`, `daily_share_above_0`) and per regime sub-window. Intervals missing any input are not evaluated; nothing is filled.
