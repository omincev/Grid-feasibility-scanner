# Energi Data Service: terms of use and licence

Checked: 2026-10-18 from `https://www.energidataservice.dk/terms-and-conditions` (English and Danish text on one page). Status: confirmed by reading the page; not legal advice.

- **Licence:** data published on Energi Data Service is under Creative Commons CC BY 4.0. Copying, changing and distributing is free, including commercially.
- **Attribution:** give appropriate credit, indicate if data was changed, and do not suggest Energinet endorses the use. Suggested credit: `Source: Energinet (www.energidataservice.dk)`. The UI must carry this wherever EDS-derived values are shown, with a "derived/modified" note for computed features.
- **Liability:** data is provided "as is"; no liability for errors or omissions and no guarantee of continued availability.
- **API usage logging:** Energinet stores the IP address of each API call for 3 months, for usage statistics. No API key or registration is mentioned, and no explicit rate limit appears on this page (the API guide may state limits; check it before bulk backfills).
- **Change monitoring:** the page is captured weekly by snapshot `dk.eds.terms` (`registry/snapshots.yaml`).

Open points for the owner: whether any individual dataset carries different conditions than the portal-wide terms (none seen); the CC BY 4.0 legal code is at `https://creativecommons.org/licenses/by/4.0/`.
