"""Apply dbmate-format migrations without dbmate (CI and local fallback). Logic: scanner.runtime.migrate.

Usage: uv run python scripts/migrate_local.py <database_url> [--skip-spatial]
"""

from __future__ import annotations

import argparse
import sys

from scanner.runtime.migrate import MigrationError, migrate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("database_url")
    ap.add_argument("--skip-spatial", action="store_true")
    args = ap.parse_args()
    try:
        migrate(args.database_url, skip_spatial=args.skip_spatial)
    except MigrationError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
