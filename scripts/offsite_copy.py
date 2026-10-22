"""Copy one backup to an off-site fsspec location (infra/backup.sh; infra/README.md, "Backups on the VPS").

Usage: python scripts/offsite_copy.py <dump file> [url]

The URL (default BACKUP_OFFSITE_URL) is a directory, for example s3://my-backups/grid-scanner. Storage options
come only from BACKUP_OFFSITE_OPTIONS, a JSON object (for an S3-compatible store such as Cloudflare R2:
{"endpoint_url": "https://<account>.r2.cloudflarestorage.com", "key": "...", "secret": "..."}). It runs in
the compose `backup` service, which has none of the raw store's settings (FSSPEC_S3_*, AWS_*), so a backup
never lands in the raw-store bucket by accident. The copy is checked by size. rclone targets
(`rclone:<remote>:<path>`) are handled by backup.sh on the host instead.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import fsspec


def copy(path: Path, url: str, options: dict[str, Any]) -> str:
    """Upload `path` into the directory `url`; returns the target path. Raises when the sizes differ."""
    fs, base = fsspec.core.url_to_fs(url, **options)
    target = f"{str(base).rstrip('/')}/{path.name}"
    fs.put_file(str(path), target)
    size = fs.size(target)
    if size != path.stat().st_size:
        raise RuntimeError(f"{target}: {size} bytes off-site, {path.stat().st_size} locally")
    return target


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 2
    url = argv[1] if len(argv) > 1 else os.environ.get("BACKUP_OFFSITE_URL", "")
    if not url or url.startswith("rclone:"):
        print("no fsspec BACKUP_OFFSITE_URL given", file=sys.stderr)
        return 2
    options = json.loads(os.environ.get("BACKUP_OFFSITE_OPTIONS") or "{}")
    target = copy(Path(argv[0]), url, options)
    print(f"copied {argv[0]} to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
