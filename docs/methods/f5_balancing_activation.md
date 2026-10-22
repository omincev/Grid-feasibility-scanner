---
id: f5_balancing_activation
version: 1
title: F5 Balancing activation
status: provisional
owner: [oleg, michael]
inputs: [balancing.dominating_direction, imbalance_price.nordic_single, day_ahead_price]
parameters: []
outputs: [f5_activation_share_up, f5_activation_share_down, f5_imbalance_spot_spread]
known_biases:
  - The imbalance price logic changed on 2025-03-19 and the dominating-direction basis on 2026-03-04; the whole-window values mix regimes (sub-window values are written too).
  - The spread is a mean absolute difference, so up and down deviations count alike.
  - Source is Energi Data Service ImbalancePrice, so only the Danish zones have F5.
---

Specification: `docs/features.md`, "F5 Balancing activation". Code: `scanner.features.zone` (F5 functions), run by `scanner.features.run`.

**What it measures.** How often and how strongly the balancing market moves away from the day-ahead market.

**Formulas.**

- `f5_activation_share_up` and `f5_activation_share_down`: the share of 15-minute intervals whose dominating direction is 1 (up) or −1 (down), among intervals with a published direction.
- `f5_imbalance_spot_spread`: the mean absolute difference between the imbalance price and the spot price, over intervals where both are published. This is the statistic the scores read.

Each is written per local day (`daily_share`, `daily_mean`), for the window and for the sub-windows split at 2025-03-19 and 2026-03-04 (market rules `imbalance.price_logic` and `imbalance.dominating_direction_basis`).
