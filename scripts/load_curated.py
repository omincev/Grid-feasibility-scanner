"""Load curated files from git into the ref and core tables (WP0.4, ADR 0006).

Thin wrapper around `python -m scanner.cli load-curated [--dry-run]`. The loader and its contract
(folders, validation, idempotence, removals, restricted path) are in scanner.core.curated.
"""

from __future__ import annotations

import sys

from scanner.cli import main

if __name__ == "__main__":
    sys.exit(main(["load-curated", *sys.argv[1:]]))
