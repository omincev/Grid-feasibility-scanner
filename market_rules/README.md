# Market rules

Market design as dated data (ADR 0011). One file per country. Schema: `scanner.core.registry.MarketRulesFile`. Loaded into `ref.market_rule`.

Each rule: `key` (dotted, from the vocabulary below), `zone` (entity ID, or null for the whole country), `value` (any YAML value), `valid_from` and `valid_to` (dates, half-open, null for open-ended), `precision` (`day` or `month`, for rules whose exact date is not known), `source` (URL or document), `verified` (true only when checked against a primary source), `note`.

Code reads rules for a zone and date and branches on their values; it never branches on country codes.

Key vocabulary (extend through PRs signed off by the country owner):

- `day_ahead.resolution`, `intraday.resolution`
- `imbalance.settlement_period`, `imbalance.pricing_method`, `imbalance.price_logic`, `imbalance.dominating_direction_basis`
- `balancing.products` (list of FCR, FFR, aFRR, mFRR...), `balancing.mfrr_energy_activation_market`
- `capacity_mechanism` (for example MACSE)
- `connection.transmission_new_agreements`, `connection.allocation_regime`, `connection.flexible_agreements`, `connection.shared_connection_point_storage`
- `dso.capacity_map_obligation`
- `support_scheme.negative_price_rule`
