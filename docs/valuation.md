# Valuation engine

The valuation engine answers the investor's question for an asset at a place: what does it earn, and what are IRR, NPV, payback and the asset-specific metrics, as ranges. It reads canonical series, facts, estimates, market rules and versioned assumption sets. It never reads raw data or adapter internals. Logic changes need sign-off from Oleg and Misi (H7).

Every result row in `val.result` records the asset template, assumption set ID and hash, price window, method and version, and run ID, so any number can be reproduced and compared after assumptions change. Each valuation runs under a scenario (`assumptions/scenarios/`), which names the template through its `bess_template` role; `val.run` and `val.result` record the scenario, the hashes of the scenario and its sets, and the method versions. Methods are described in `docs/methods/` (BESS v0: `bess_v0_daily_lp_day_ahead.md`).

## Asset templates

Templates are parameter sets in `assumptions/`, never constants in code.

- BESS: power (MW), energy (MWh), round-trip efficiency, min and max state of charge, cycle limit per day, degradation per cycle and per year, capex per MW and per MWh, fixed and variable opex, augmentation schedule, lifetime, grid fees per country (from market rules).
- DSF: flexible MW, flexibility windows (time of day, duration, notice), activation limits per day and year, rebound behaviour, client revenue share, aggregator costs.
- Modular DC: IT load MW, PUE, utilisation, power-availability requirement, heat recovery share, capex, opex, required connection type (firm or flexible).

## BESS v0 (MVP)

One battery of the size in `assumptions/bess_mvp.yaml` per zone. For each local day, solve a linear programme over that day's day-ahead prices at native resolution:

- variables: charge `c_t`, discharge `d_t`, state of charge `s_t`
- maximise `sum_t (p_t * d_t - p_t * c_t) * dt`
- subject to `s_t = s_{t-1} + (eta_c * c_t - d_t / eta_d) * dt`, `0 <= c_t, d_t <= P`, `soc_min * E <= s_t <= soc_max * E`, `s_0 = s_T = s_init`, and total discharged energy at most `cycles_per_day * E`

Solve with `scipy.optimize.linprog(method="highs")`. Sum daily revenues into rolling 365-day windows over the available history. Report low = minimum and high = maximum of those window sums, divided by power, as EUR per MW per year. If less than 365 days are available, report the annualised mean with the label "less than one year of data". Days with a missing, null or rejected interval (quarantined values count unless a person rejected their run, `docs/quality.md` layer 2) are skipped and counted, never filled, and a window counts only if all 365 of its days were valued; if skipped days leave no complete window although 365 or more days were valued, report the same annualised mean with the label "no complete 365-day window" (pending H7: whether windows of 365 valued days should count instead).

Known biases, shown on the zone card: perfect foresight (overstates), no balancing revenue (understates), no degradation cost (overstates), no saturation (overstates as more storage enters).

## Full BESS model (Phase 6)

Revenue stack: day-ahead and intraday arbitrage, balancing energy (aFRR, mFRR activations), reserve capacity (FCR, aFRR, mFRR capacity), capacity mechanisms (MACSE in Italy), tolling offers. Co-optimised in one LP or MILP per day with the reserve products and rules from `ref.market_rule` for the zone and date.

The foresight penalty (WP6.3): dispatch decisions use forecasts available at decision time (as-of queries on forecast series), settlement uses realised prices. Report perfect-foresight and forecast-based results side by side.

Saturation scenarios (WP6.4): price spreads and balancing prices adjusted for added storage capacity in the zone. Their parameters will be assumption sets with a role of their own, named by the analytics scenarios in `assumptions/scenarios/`, and link to the market evaluation framework's saturation position.

## DSF model (Phase 6)

Revenue per MW of flexible load per year from balancing activations within the client's flexibility windows, plus avoided peak tariffs, minus rebound costs and the aggregator share. Output: revenue per MW per year, client payback. Needs Layer 2 data for real sites; on public data it runs on template windows and is labelled "template site".

## Modular DC model (Phase 6, scope per open decision D3)

The spread hardly matters. The model computes all-in electricity cost per MWh (energy, grid tariffs, taxes, from market rules and tariff facts), hours of surplus or low prices, load headroom at the substation, flexible-connection options, PPA price ranges (D5), heat-sale value and fibre distance. Output: cost per MWh delivered, time to power (estimate with range), IRR on the IT load.

## Contract instruments

PPAs (pay-as-produced or baseload, physical or virtual), tolling agreements, capacity contracts, flexible connection agreements and heat-sale contracts are modelled as cash-flow transforms on top of the dispatch result, with their parameters in assumption sets. Private price references carry sensitivity `licensed` or `nda` and make every result that uses them inherit that class.

## Financials

`scanner.valuation.financials` implements NPV, IRR, payback and LCOS on yearly cash flows. Ranges come from running the cash-flow model at the low and high revenue cases and across the assumption ranges in the set; never a single number.
