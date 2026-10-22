---
id: f7_substation_saturation
version: 2
title: F7 Substation saturation
status: provisional
owner: [oleg, michael]
inputs: [capacity.free_mw.consumption, capacity.free_mw.production]
parameters: []
outputs: [f7_substation_saturation]
known_biases:
  - A current snapshot of the capacity map, not a history; the values carry the analysis window as period but describe the state known at run time.
  - For DSO stations the transmission limit is not applied (it needs an approved link from the DSO station to its Energinet station), so the true free capacity can be lower.
  - A published 0 MW means below the publisher's first step (under 10 MW for consumption, under 20 MW for production), not exactly zero.
  - Station values cannot be added up (the source says so); the area sums and MW means are indicative only.
  - Station-count shares weigh a large and a small station the same; the MW means cannot be divided by station capacity, which the map does not publish.
  - TSO consumption is treated as not assessed (an inferred reading, pending Energinet), so transmission stations count for production only and every direction-aware statistic rests on DSO stations alone.
  - The _incl_tso statistics count a 150/132 kV station next to the 60/10 kV stations it feeds; their MW means are dominated by the larger transmission values.
---

Specification: `docs/features.md`, "F7 Substation saturation". Code: `scanner.features.substation`, run by `scanner.features.run`. Source: the national capacity map (`docs/sources/dk/dso-capacity-maps.md`, `docs/sources/dk/energinet-transmission-capacity.md`), loaded as facts by `scanner.runtime.capacity_map`. Computed once per feature run, for every scenario: scenarios only choose which statistic a score reads.

**What it measures.** Whether the grid stations in an area still have free capacity for new production or new consumption, and whether a station has room in one direction where the other is full.

**Per station.** The free MW the map publishes, per direction, taken as the lowest of the published values that apply: for DSO stations the lower of the full-access value without and with the TSO transformer; for TSO stations the existing-grid value. Null when either value is missing (never 0). Rank within the zone among stations of the same level (`rank_in_zone_*`, 1 = least free capacity, ties share the lowest rank). The source publishes free MW only, so no percentage and no free/limited/none category is derived.

**TSO consumption is not assessed (inferred).** All 298 TSO rows publish 0 MW consumption capacity, all 16 time-band percentages 0 and 0 hours with 0 MW consumption capacity. 0 MW on average over a year would mean 8760 such hours, so the fields contradict each other, and neither the StoryMap nor the Energi Data Service metadata says what the 0 means. Our reading, not the source's: the figures were not calculated or not published. F7 therefore gives TSO stations no consumption value: no `free_mw_consumption`, no consumption rank, and no area statistic counts them as stations without free consumption capacity; the serving views show their consumption status as unknown. The facts stay in `core.fact` as published. The rule is `NOT_ASSESSED` in `scanner.features.substation`; it is lifted when Energinet answers (`docs/questions/capacity-map-semantics.md`, question 1).

**Limited-access ranges are not read.** 70 DSO stations publish a limited-access lower bound above the upper bound (for solar and wind the upper bound is then 0). The ranges stay as published and flagged (`serving.substation_map.limited_range_inverted`); no F7 statistic reads a limited-access field, so an upper bound 0 is never read as 0 MW or as unlimited.

**Per zone and municipality.** Two station populations: DSO stations (names without suffix, the BEK 1468 §29 population) and DSO plus TSO existing-grid stations (suffix `_incl_tso`; TSO stations count where their production value is published, and for consumption not at all). Per direction d (`consumption`, `production`):

| Statistic | Meaning |
|---|---|
| `stations` | stations in the area |
| `stations_assessed_<d>` | stations with a published free MW in d |
| `share_stations_no_free_<d>` | stations with 0 MW free in d ÷ `stations_assessed_<d>` (station-count share) |
| `free_mw_<d>_mean` | sum of free MW in d ÷ `stations_assessed_<d>` (the MW variant; the map publishes no station capacity, so free MW over capacity cannot be formed) |
| `free_mw_<d>_sum` | sum of free MW in d (DSO only, indicative) |
| `stations_assessed_both` | stations with published values in both directions |
| `share_stations_free_consumption_no_free_production` | stations with free consumption capacity and 0 MW free production ÷ `stations_assessed_both`: room for a load at a production-saturated station |
| `share_stations_free_production_no_free_consumption` | the reverse: room for production at a consumption-saturated station |
| `free_mw_consumption_mean_no_free_production` | free consumption MW summed over stations with 0 MW free production ÷ `stations_assessed_both` (stations with free production add 0 MW) |
| `free_mw_production_mean_no_free_consumption` | the reverse |

Each statistic except the sums also exists with the suffix `_incl_tso`. Counts are written for every area with a station; shares and means only where their count is above 0 (an area without stations has no rows). `scanner.features.substation.SAMPLE_STATISTIC` names the count behind each statistic; the site score flags municipalities whose count is below `score_method.municipality_min_stations` (`low_station_count`).

The scores read one statistic through the scenario's `score_method` set: `share_stations_no_free_production` (higher = more congested) by default, the direction-aware statistics in `f7_direction_aware` and `f7_mw_weighted` (`docs/methods/municipality_site_score.md`). Labels: "current snapshot", and the capacity map's own caveat on site cards.
