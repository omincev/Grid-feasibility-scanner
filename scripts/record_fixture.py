"""Record a real source response as a test fixture (WP1.1). Run by a human or the supervisor, never by
a coding agent (AGENTS.md rule 5). Only public data may be recorded.

Example:
  uv run python scripts/record_fixture.py energidataservice DayAheadPrices dst_end \
      --url 'https://api.energidataservice.dk/dataset/DayAheadPrices?start=2025-10-25T00:00&end=2025-10-28T00:00&filter={"PriceArea":["DK1","DK2"]}'

Writes tests/fixtures/<source>/<dataset>__<case>.json and a .meta.json sidecar with URL, time and
SHA-256. Refuses to overwrite an existing fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from scanner import REPO_ROOT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("dataset")
    ap.add_argument("case", help="short name, e.g. normal_day, dst_end, resolution_switch")
    ap.add_argument("--url", required=True)
    args = ap.parse_args()

    out_dir = REPO_ROOT / "tests" / "fixtures" / args.source
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{args.dataset}__{args.case}.json"
    if path.exists():
        print(f"refusing to overwrite {path}", file=sys.stderr)
        return 1
    if "token" in args.url.lower() or "key=" in args.url.lower():
        print("URL looks like it contains a credential; fixtures must not contain secrets", file=sys.stderr)
        return 1
    r = httpx.get(args.url, timeout=120)
    r.raise_for_status()
    path.write_bytes(r.content)
    meta = {
        "url": args.url,
        "status": r.status_code,
        "content_type": r.headers.get("content-type"),
        "fetched_at": datetime.now(UTC).isoformat(),
        "sha256": hashlib.sha256(r.content).hexdigest(),
    }
    Path(str(path) + ".meta.json").write_text(json.dumps(meta, indent=2))
    print(f"wrote {path.relative_to(REPO_ROOT)} ({len(r.content)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
