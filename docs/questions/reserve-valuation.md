# Reserve-market valuation: questions and answers

Owner: Oleg. Asked 2026-10-21; answers received the same day. Each answer is recorded as given, with the reading we would build from and, where the answer is unclear or missing, a sharper follow-up. Nothing here is approved until it is an assumption set (`assumptions/`) with `approved_by`; until then the valuation stays day-ahead only (`docs/methods/bess_v0_daily_lp_day_ahead.md`).

Data available: `docs/sources/dk/eds-reserves-fcr.md`, `eds-reserves-afrr.md`, `eds-reserves-mfrr.md`, plus aFRR and mFRR activation prices in ImbalancePrice.

## A. Products

| # | Question | Answer | Reading | Follow-up |
|---|---|---|---|---|
| A1 | Which products should a Danish battery be valued for? | FCR-D, FCR-N, aFRR, mFRR; also intraday | FCR-N, FCR-D up and down, aFRR (capacity and energy), mFRR (capacity and energy). FFR and DK1 FCR left out. Intraday is a separate market we do not have data for (Nord Pool) | Is leaving out FFR (DK2) and FCR (DK1) intended? |
| A2 | Same products in DK1 and DK2? | No | Unclear: "no difference" or "no, they differ" | FCR-N and FCR-D exist only in DK2 (Nordic area); DK1 has FCR (continental). Is a DK1 battery then valued for aFRR and mFRR only, or for DK1 FCR as well? |
| A3 | DK1: Danish or cross-border FCR price? | Depending where we trade | Both are possible | If DK1 FCR stays in: which one should the model use by default? |
| A4 | FCR-N/D DK2: which auction's price? | D-1 early | Use the `d1_early` series | none |

## B. Technical limits

| # | Question | Answer | Reading | Follow-up |
|---|---|---|---|---|
| B5 | Minimum duration per product; effect on a 2 h battery | Minimum 30 s on hold; 40 % of capacity dedicated to frequency services | At most 40 % of power (and its energy) goes to FCR-N/D, aFRR, mFRR; 60 % stays for arbitrage | Is 40 % a fixed share or a maximum the model may choose below? Is 30 s the hold time for all products? (FCR-D and aFRR usually require longer energy reserves; the product rules are worth checking against Energinet's prequalification terms) |
| B6 | Symmetric or asymmetric bids; minimum bid | Preferably upward regulation, based on state of charge and the opportunity value of stored energy; at least 15 % of capacity to frequency services | Up-regulation first; frequency share between 15 % and 40 % | Confirm the 15 % minimum and 40 % maximum reading; minimum bid size in MW? |
| B7 | How to model activation | Use the ingested activation prices and volumes; up-regulation lowers state of charge, down-regulation raises it | Activation from the actual aFRR and mFRR activations per 15 minutes, scaled to the battery's share | How to scale a zone's activated volume to one battery: pro rata to the battery's share of procured capacity? |
| B8 | Availability, penalties | 95 % availability; penalty when non-delivery exceeds 10 % of promised time: price × energy not delivered | Revenue × 0.95; penalty modelled as zero while availability is 95 % (under the 10 % threshold) | none |

## C. Combining reserves and arbitrage (not answered)

| # | Question | Follow-up, sharper |
|---|---|---|
| C9 | Stacking | Within one hour, may the 60 % not reserved be used for day-ahead arbitrage while the 40 % is held for reserves (our default), or is each hour either reserves or arbitrage? |
| C10 | Foresight | Should the daily optimisation know the day's reserve prices in advance (as it knows day-ahead prices now, flagged "perfect foresight"), or use a rule: reserve share fixed, arbitrage on the rest? |
| C11 | Cycles | Does energy moved by activations count towards the approved one cycle a day, or only arbitrage cycles? |

## D. Prices over time

| # | Question | Answer | Reading | Follow-up |
|---|---|---|---|---|
| D12 | Saturation | Forecast path, −5 % a year | Reserve capacity prices decline 5 % a year from the last observed level | Applies to capacity prices only, or to activation prices too? Starting level: last 12 months' mean? |
| D13 | Averaging period | Every 24 hours | Likely read as the optimisation step (daily), not the history window | Which history should set the starting level: the last 12 months, or since 2024? |
| D14 | Separate uncertainty label | Yes | A "reserve revenue: forecast path, not observed" label on each reserve line | none |

## E. Money

| # | Question | Answer | Reading | Follow-up |
|---|---|---|---|---|
| E15 | Fees, reserve-specific tariffs | No fees; consider avoided carbon taxes | No deductions. Avoided carbon cost is a societal value, not cash to the battery: shown as a separate, clearly labelled line, not in the € per MW revenue | Which carbon price and which avoided-emission factor (t CO₂ per MWh of reserve displacing fossil)? |
| E16 | One range or split lines | Split | Revenue lines: day-ahead arbitrage, FCR-N, FCR-D up, FCR-D down, aFRR capacity, aFRR energy, mFRR capacity, mFRR energy; each with its own range and labels | none |
