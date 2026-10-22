# Bead draft (label: human): capacity-map field semantics

To: Energinet, `netudvikling@energinet.dk` (the contact the StoryMap gives for data questions; per `docs/sources/dk/dso-capacity-maps.md`). Context: Kapacitetskortet_V2 (`https://storymaps.arcgis.com/stories/eb5b387e376f49b8996d5e7c47fbdd37`), snapshot 2026-10-18. Evidence for each question is in the two dossiers under "Verification of review claims". Until answered, the interim handling below applies; none of it changes scoring code.

## 1. TSO layer: what does a published 0 for consumption mean?

All 298 rows of `TSO_stations_capacity_points` (EDS `GridCapacityMapTSOProductionandConsumption`, publication 2026-02-01) have consumption capacity 0 MW, all 16 time-band percentages 0 and `hrs_zero_cons` 0 hours. The last is inconsistent with "0 MW all year" (8760 hours).

Options:
- A. No free consumption capacity at any station (and `hrs_zero_cons` is wrongly 0).
- B. Not yet calculated / not published; 0 is a placeholder.
- C. Something else (for example the maps 7/8 values are held back until the Q4 2026 update).

Interim (inferred): B, i.e. treat as null/not assessed in anything user-facing; the F7 station rows for TSO consumption (currently 0 MW, rank 1) should not be presented as "no capacity". Flag to the owner of F7. Implemented 2026-10-18 in F7 version 2 (`scanner.features.substation.NOT_ASSESSED`; `serving.substation_map.consumption_not_assessed`); the facts stay as published.

## 2. DSO limited-access ranges: what does lower > upper mean, in particular upper = 0?

70 DSO stations (Cerius 34 and Radius 28 of them) have `available_capacity_solar_MW_lower_bound` > 0 with `..._upper_bound` = 0; the same 67 for wind; 7 further stations have limited-access consumption with lower > upper (upper 20 or 30). The documentation only calls them "lower / upper bound" under "200 hours of interruptibility".

Options:
- A. Upper = 0 means "no upper limit / not limited by this calculation" (sentinel).
- B. Upper = 0 means "not calculated"; the lower bound is the only value.
- C. Data error: the bounds are swapped or the upper bound was not filled.
- D. Upper = 0 is real (limited access gives no more than 0) and the lower bound is wrong.

Interim (inferred): store both as published, keep the quirk flag, never use the pair as a range, never read upper = 0 as 0 MW or as unlimited. For the 7 consumption cases the pattern differs (upper 20 or 30); ask separately. Checked 2026-10-18: no F7 statistic or score reads a limited-access field (`tests/test_f7.py`).

## 3. Is there a flexible-connection indicator?

BEK 1468 §29 requires the map to show whether flexible connection agreements are possible. The layers have no such field (see `dso-capacity-maps.md`, "How categories are encoded"). Options: A. a field exists elsewhere (give the name); B. non-zero limited-access range means yes; C. it is not published. Interim: `capacity.flexible_connection_possible` stays null.

## 4. DSO-to-TSO station link

`TSO_station_abbreviation` on DSO rows uses bare codes (`VID`), TSO layers use suffixed codes (`VID_150`, `ABA_150--$`). Is there an authoritative mapping, and which suffix applies? Without it F7 cannot apply the transmission value to a DSO station. Interim: no join; F7 uses the lower of the with- and without-TSO-transformer values.

## 5. Stale DSO rows and update dates

26 DSO rows carry older `publication_date` values and duplicate a current station. Are older rows left over from a republish (our reading) or a station genuinely last assessed on that date? And when is the next DSO publication (3 months after 2026-09-01, i.e. about 2026-12-01) and the next TSO publication (announced "primo 4 kvartal 2026")? Interim: latest row per station wins; older rows are earlier fact versions.

## 6. Redistribution

May the values be shown in a commercial product, and what attribution is required? Items have no licence text; EDS terms apply. Interim: show the StoryMap caveat and the source name.
