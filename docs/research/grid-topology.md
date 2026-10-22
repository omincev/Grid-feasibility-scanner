# Research brief: grid topology and node-level capacity

Status: open. Owner: Misi, with Martino (Italy) and Michael (Hungary). Feeds WP5.2, F7, F8, F9 and the grid-friendliness score.

## Why it matters

The grid-friendliness question in the build plan ("does the asset relieve the local constraint or make it worse") needs to know which nodes sit behind the same constraint and in which direction each node is stressed. Denmark publishes capacity maps in both directions for 25 to 99 kV stations; Italy and Hungary publish less, and nobody publishes a full network model openly.

## Questions

1. What does each TSO and DSO in DK, IT and HU publish today on available connection capacity, by voltage level, direction and update frequency? Format and licence?
2. What does EU law now oblige them to publish? The 2024 electricity market design reform (Directive (EU) 2024/1711) amended the Electricity Directive with publication duties for available connection capacity. Confirm the exact articles, the required granularity and update frequency, and whether Italy and Hungary have transposed them. Denmark's transposition includes BEK 1468/2025 §29 (quarterly DSO capacity maps).
3. What open network models exist? Start with PyPSA-Eur (OSM-based transmission network with substations and lines) and the ENTSO-E grid map. How accurate are they at 132/150 kV and below in our three countries?
4. What can be obtained from operators under NDA or through a research agreement, and what would we have to offer?
5. Can topology be estimated well enough from public proxies (OSM lines, substation locations, capacity map areas, price separation, curtailment patterns) to rank nodes, and how would we validate it?
6. Which national documents (network development plans, Terna's Econnextion, MAVIR plans) give constraint information at node or area level?

## Output

A dossier per country in `docs/sources/<cc>/grid-topology.md`, claims for any figures extracted, a recommendation on the estimation approach for each country, and a list of operator contacts worth approaching. Each country section signed off by its domain owner.
