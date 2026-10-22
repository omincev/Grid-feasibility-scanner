# Source dossier: Energinet connection-queue map

Status: draft. Country: dk. Owner: Misi. Last verified: 2026-10-18 (Playwright inspection of the live page).

## What it is

An interactive regional map of the queue for connection to Energinet's transmission grid, published in June 2026. It shows applied capacity per region for consumption and production, split by project phase (screening, modning, etablering). At launch the queue held about 35 GW of consumption and 24 GW of production. Data updates on the first day of each month. Version 1.0 has no project-level detail; Energinet has announced a more detailed version 2.0.

Page: `https://energinet.dk/el/nyt-anlaeg-i-elnettet/overblik-over-nettilslutningsprojekter/`

Feeds F8 (queue pressure) and the "why now" narrative.

## Access

Researched 2026-10-18 with Playwright. The page embeds three Power BI "publish to web" reports (iframes on `app.powerbi.com/view?r=<base64>`, one per `pageName`). The `r` parameter is base64 JSON with the resource key (`k`) and tenant (`t`). No login is needed.

Data endpoint (no credentials beyond the public resource key):

```
POST https://wabi-north-europe-g-primary-api.analysis.windows.net/public/reports/querydata?synchronous=true
Header: X-PowerBI-ResourceKey: <k from the embed URL>
Body:   Power BI semantic query, modelId 4546001
```

Model (Danish names): tables `Dim Placering` (column `Region`), `Dim Type` (`Type`: Forbrug = consumption, Produktion = production), `Dim Fase` (`Fasenavn`: Screening, Modning, Etablering; `Fasenummer`), `Målinger` (measures `Kapacitet i alt MW`, `Kapacitet forbrug MW`, `Kapacitet produktion MW`, `Antal projekter i alt med failsafe`, `Senest opdateret`). One grouped query on Region x Type x Fasenavn with `Kapacitet i alt MW` returns the whole map (5 regions x 2 types x 3 phases). The response uses Power BI's compressed DSR format (`R` repeat bitmask, `Ø` null bitmask, string columns index into `ValueDicts`).

Fields: region (Region Hovedstaden, Midtjylland, Nordjylland, Sjælland, Syddanmark), direction (Forbrug/Produktion), phase, capacity in MW, project count (returned as the string `'Ikke oplyst'` in the grouped query, so not usable per cell; only totals showed counts on the page). Empty `Type`/phase rows are Power BI padding, not data.

Snapshot of 2026-10-18 (page says "Senest opdateret: 01-10-2026"): consumption 36,494.54 MW, production 24,287.69 MW. By phase, consumption 25,447.5 / 7,235 / 3,812.04 MW and production 8,625.4 / 4,817.65 / 10,844.64 MW (Screening / Modning / Etablering). These match the sum of the per-region cells.

Fixture: `tests/fixtures/energinet/connection_queue__capacity_by_region_phase.json` (raw response) with `.meta.json`. The resource key is deliberately not stored; get it from the live embed URL.

Cadence: monthly, on the first day of the month (confirmed by the "Senest opdateret" text).

Snapshot status: `registry/snapshots.yaml` entry `dk.energinet.connection_queue` is a `kind: json`, `method: POST` capture of the querydata endpoint with the grouped query (Region x Type x Fasenavn, measures `Kapacitet i alt MW` and `Senest opdateret`) as its body. The resource key is read from the page's embed URL on every capture (`powerbi_embed_page`; the page is static HTML, no browser needed), so it is never stored. Volatile keys (`jobIds`, `jobId`, `timestamp`, `rootActivityId`) are left out of the change hash. `on_change: parse:connection_queue` turns each capture into `core.fact` rows (codebook `energinet.connection_queue`): one `tso_area` entity per region, `dk.tso_area.<slug(region)>`, parent the zone that contains the region (Nordjylland, Midtjylland, Syddanmark in DK1; Sjælland, Hovedstaden in DK2), `valid_from` = the "Senest opdateret" date at 00:00 UTC; only changed values are written. A second entry, `dk.energinet.connection_queue.page`, watches the page itself for a new report version. The Power BI endpoint is unofficial; the embed may change without notice.

## Decisions

The normalisation decisions of the `parse:connection_queue` parser (`scanner.runtime.snapshot_parsers`), in one place (the snapshot asset shows this section in Dagster):

- **Facts per region, direction and phase.** One `core.fact` per cell of the grouped query (5 regions x consumption/production x screening/maturation/establishment), in MW as published, on one `tso_area` entity per region (`dk.tso_area.<slug(region)>`), parent the zone that contains the region.
- **Valid from the report date.** `valid_from` is the "Senest opdateret" date at 00:00 UTC; only values that changed are written.
- **Not ingested.** Project counts (the grouped query returns the string `'Ikke oplyst'` per cell) and the update date as a value; empty `Type` or phase rows are Power BI padding, not data.
- **Change detection.** Volatile response keys (`jobIds`, `jobId`, `timestamp`, `rootActivityId`) are left out of the change hash; the public resource key is read from the embed page on every capture and never stored.

## Licence and terms

Not stated on the page or in the report. To confirm with Energinet before redistributing the figures; treat as attribution-required ("Source: Energinet") until then.

## Sensitivity

`public`.
