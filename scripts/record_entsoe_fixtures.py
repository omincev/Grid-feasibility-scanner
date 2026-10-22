"""Record ENTSO-E Transparency Platform responses as test fixtures (WP3.1). Run by the orchestrator, never
by a coding agent. Only public data is requested.

The security token is read only through scanner.settings (ENTSOE_API_TOKEN or ENTSOE_API_KEY, from the
environment or .env), sent only in the SECURITY_TOKEN header, and never printed. Before anything is
written, its content and sidecar are checked for the token; a match writes nothing for that case.

  uv run python scripts/record_entsoe_fixtures.py --out tests/fixtures/entsoe

Writes <out>/<case>.xml and <out>/<case>.xml.meta.json (redacted URL, parameters, status, SHA-256,
fetch time). Cases whose file already exists are skipped, so a re-run resumes. Requests run one at a time
with a pause (the API allows 400 requests per minute per token). Exit code: 0 all recorded or skipped,
1 a case failed, 2 no token configured.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx

from scanner.adapters.base import ParseError
from scanner.adapters.entsoe import (
    AcknowledgementError,
    EntsoeHttpError,
    TokenLeakError,
    assert_no_token,
    decode,
    get_document,
)
from scanner.settings import get_settings

DK1 = "10YDK-1--------W"
DE_LU = "10Y1001A1001A82H"
DE_TENNET_CTA = "10YDE-EON------1"
NL = "10YNL----------L"


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    params: dict[str, str]


def _prices(eic: str, start: str, end: str, **extra: str) -> dict[str, str]:
    """12.1.D day-ahead prices. Parameter spelling as in ENTSO-E's Postman collection."""
    return {
        "documentType": "A44",
        "in_Domain": eic,
        "out_Domain": eic,
        "contract_MarketAgreement.type": "A01",
        "periodStart": start,
        "periodEnd": end,
        **extra,
    }


# periodStart/periodEnd are UTC (yyyyMMddHHmm). The normal day is 2026-10-14, Europe/Copenhagen (CEST).
DAY = ("202610132200", "202610142200")

CASES = [
    Case("day_ahead_prices__dk1__2026-10-14", "A44 DK1, normal day", _prices(DK1, *DAY)),
    Case(
        "day_ahead_prices__dk1__2026-10-14__curve_a01",
        "A44 DK1, normal day, curveType=A01 requested; must decode to the same values as the default (A03)",
        _prices(DK1, *DAY, curveType="A01"),
    ),
    Case(
        "day_ahead_prices__dk1__2025-10-26_dst_end",
        "A44 DK1, DST end: 25-hour local day, expect 100 quarter-hours",
        _prices(DK1, "202510252200", "202510262300"),
    ),
    Case(
        "day_ahead_prices__dk1__2026-03-29_dst_start",
        "A44 DK1, DST start: 23-hour local day, expect 92 quarter-hours",
        _prices(DK1, "202603282300", "202603292200"),
    ),
    Case(
        "day_ahead_prices__dk1__2025-09-30_2025-10-01_resolution_switch",
        "A44 DK1, two local days across the SDAC 15-minute go-live: expect PT60M then PT15M",
        _prices(DK1, "202509292200", "202510012200"),
    ),
    Case("day_ahead_prices__nl__2026-10-14", "A44 NL, normal day", _prices(NL, *DAY)),
    Case("day_ahead_prices__de_lu__2026-10-14", "A44 DE-LU, normal day", _prices(DE_LU, *DAY)),
    Case(
        "actual_total_load__dk1__2026-10-14",
        "A65/A16 actual total load DK1, normal day",
        {
            "documentType": "A65",
            "processType": "A16",
            "outBiddingZone_Domain": DK1,
            "periodStart": DAY[0],
            "periodEnd": DAY[1],
        },
    ),
    Case(
        "actual_generation_per_type__dk1__2026-10-14",
        "A75/A16 actual generation per production type DK1, normal day",
        {"documentType": "A75", "processType": "A16", "in_Domain": DK1, "periodStart": DAY[0], "periodEnd": DAY[1]},
    ),
    Case(
        "physical_flows__dk1_to_de_lu__2026-10-14",
        "A11 physical flow DK1 -> DE-LU (bidding zone codes), normal day",
        {"documentType": "A11", "out_Domain": DK1, "in_Domain": DE_LU, "periodStart": DAY[0], "periodEnd": DAY[1]},
    ),
    Case(
        "physical_flows__dk1_to_de_tennet__2026-10-14",
        "A11 physical flow DK1 -> DE(TenneT GER) control area, normal day; fallback if the BZN query is empty",
        {
            "documentType": "A11",
            "out_Domain": DK1,
            "in_Domain": DE_TENNET_CTA,
            "periodStart": DAY[0],
            "periodEnd": DAY[1],
        },
    ),
    Case(
        "offered_capacity_da_implicit__dk1_to_de_lu__2026-10-14",
        "A31 offered transfer capacity, implicit day-ahead, DK1 -> DE-LU, normal day",
        {
            "documentType": "A31",
            "auction.Type": "A01",
            "contract_MarketAgreement.Type": "A01",
            "out_Domain": DK1,
            "in_Domain": DE_LU,
            "periodStart": DAY[0],
            "periodEnd": DAY[1],
        },
    ),
    Case(
        "forecasted_capacity_da__dk1_to_de_lu__2026-10-14",
        "A61 forecasted (estimated net) transfer capacity, day-ahead, DK1 -> DE-LU, normal day",
        {
            "documentType": "A61",
            "contract_MarketAgreement.Type": "A01",
            "out_Domain": DK1,
            "in_Domain": DE_LU,
            "periodStart": DAY[0],
            "periodEnd": DAY[1],
        },
    ),
    Case(
        "no_data__day_ahead_prices__dk1__2027-01-15",
        "A44 DK1 for a day not yet auctioned: expect an Acknowledgement, reason 999 'No matching data found'",
        _prices(DK1, "202701142300", "202701152300"),
    ),
]


def summarise(content: bytes) -> str:
    """One line describing what came back, for the operator's console."""
    try:
        records = decode(content)
    except AcknowledgementError as exc:
        return f"acknowledgement ({'no matching data' if exc.no_matching_data else 'rejected'}): {exc}"
    except ParseError as exc:
        return f"does not decode: {exc}"
    shapes = Counter((r.resolution, r.curve_type) for r in records)
    series = len({r.time_series for r in records})
    return f"{len(records)} records in {series} time series; (resolution, curve): {dict(shapes)}"


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="directory to write fixtures into")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between requests (default 2)")
    ap.add_argument("--case", action="append", choices=[c.name for c in CASES], help="record only these cases")
    args = ap.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")  # API texts must not crash a cp1252 console

    settings = get_settings()
    token = settings.entsoe_api_token
    if token is None or not token.get_secret_value():
        print("ENTSOE_API_TOKEN (or ENTSOE_API_KEY) is not set; nothing recorded", file=sys.stderr)
        return 2
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    cases = [c for c in CASES if not args.case or c.name in args.case]

    failed = 0
    sent = 0
    with httpx.Client(
        timeout=settings.http_timeout_s, headers={"User-Agent": settings.user_agent}, transport=transport
    ) as client:
        for case in cases:
            if any(out.glob(f"{case.name}.*")):
                print(f"{case.name}: exists, skipped")
                continue
            if sent and args.pause > 0:
                time.sleep(args.pause)
            sent += 1
            try:
                result = get_document(client, case.params, token)
            except (EntsoeHttpError, TokenLeakError) as exc:
                failed += 1
                print(f"{case.name}: FAILED: {exc}")
                continue
            if result.status == 429:
                print(f"{case.name}: HTTP 429 after retries; stopping (the ban lasts about 10 minutes)")
                return 1
            if result.status in (401, 403):
                print(
                    f"{case.name}: HTTP {result.status}. The token was rejected, or the API no longer accepts the "
                    "SECURITY_TOKEN header on GET. Stopping; nothing written for this case."
                )
                return 1
            path = out / f"{case.name}.{result.ext}"
            meta = {
                "case": case.name,
                "description": case.description,
                "url": result.url,
                "params": result.params,
                "auth": "SECURITY_TOKEN request header (redacted; never stored)",
                "status": result.status,
                "content_type": result.content_type,
                "headers": result.headers,
                "fetched_at": result.fetched_at.isoformat(),
                "sha256": hashlib.sha256(result.content).hexdigest(),
                "size": len(result.content),
                "recorded_by": "scripts/record_entsoe_fixtures.py",
            }
            meta_text = json.dumps(meta, indent=2, sort_keys=True) + "\n"
            try:
                assert_no_token(token, result.content, meta_text, path.name)
            except TokenLeakError as exc:
                failed += 1
                print(f"{case.name}: FAILED: {exc}")
                continue
            path.write_bytes(result.content)
            Path(f"{path}.meta.json").write_text(meta_text, encoding="utf-8", newline="\n")
            if not 200 <= result.status < 300:
                failed += 1
            print(f"{case.name}: HTTP {result.status}, {len(result.content)} bytes; {summarise(result.content)}")
    print(f"done: {len(cases)} cases, {sent} requests, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
