"""Raw landing zone (docs/architecture.md). Everything fetched is stored untouched, with a sidecar.

References are relative to the store root, `raw://<source>/<dataset>/<yyyy>/<mm>/<dd>/<run_id>-<seq>.<ext>`,
and resolve through RAW_STORE_URL when read (the object lives at `<RAW_STORE_URL>/raw/<relative path>`), so
the store can move to another directory, container mount or bucket without rewriting the database.
References written before 2026-10-18 were absolute; `relative_uri` maps them (CLI `raw-uris migrate`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import fsspec

SCHEME = "raw://"
# An absolute reference in the store layout, on a filesystem fsspec stores captures on:
# <scheme>://<root>/raw/<source>/<dataset>/<yyyy>/<mm>/<dd>/<name>.
_ABSOLUTE = re.compile(r"^(?:file|memory|s3|s3a|gs|gcs|az|abfs)://.*/raw/(?P<rel>[^/]+/[^/]+/\d{4}/\d{2}/\d{2}/[^/]+)$")


@dataclass(frozen=True)
class RawRef:
    uri: str
    sha256: str
    size: int


def relative_uri(uri: str) -> str | None:
    """The `raw://` form of a reference: itself if relative already, the store-relative part of an absolute
    reference in the store layout (any root, e.g. file://C:/.../.data/raw/raw/... or file:///data/raw/raw/...),
    or None for anything else."""
    if uri.startswith(SCHEME):
        return uri
    m = _ABSOLUTE.match(uri)
    return SCHEME + m["rel"] if m else None


class RawStore:
    """Write-once store keyed raw/<source>/<dataset>/<yyyy>/<mm>/<dd>/<run_id>-<seq>.<ext>.

    `base_url` is any fsspec URL: file://./.data/raw in development, s3://bucket/prefix in production.

    Identical content is stored once per source and dataset. Backfill reuse hands several runs the same
    response (scanner.adapters.entsoe, energidataservice), and unchanged pages are captured again on every
    schedule tick. The first capture holds the bytes; a later capture with the same SHA-256 writes only its
    own sidecar, whose `content_uri` names the capture holding them, found through the index object
    `<base>/sha256/<source>/<dataset>/<sha256>`. Each capture keeps its own reference and sidecar, and
    `get` follows `content_uri`, so every reference reads back its bytes; read captures through `get`, not
    by opening the path. Captures written before 2026-10-18 each hold their own copy.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.fs, self.base_path = fsspec.core.url_to_fs(self.base_url)

    def _path(self, rel: str) -> str:
        return f"{self.base_path}/raw/{rel}"

    def _index(self, source: str, dataset: str, sha: str) -> str:
        return f"{self.base_path}/sha256/{source}/{dataset}/{sha}"

    def resolve(self, uri: str) -> str:
        """The fsspec path of a reference: a `raw://` reference under this store's root; an absolute one as
        it is (read with the filesystem its scheme names)."""
        if uri.startswith(SCHEME):
            return self._path(uri.removeprefix(SCHEME))
        return uri

    def _write(self, path: str, content: bytes) -> None:
        self.fs.makedirs(path.rsplit("/", 1)[0], exist_ok=True)
        with self.fs.open(path, "wb") as fh:
            fh.write(content)

    def _stored_copy(self, source: str, dataset: str, sha: str) -> str | None:
        """The reference of an earlier capture of this source and dataset holding these bytes, if any."""
        index = self._index(source, dataset, sha)
        if not self.fs.exists(index):
            return None
        with self.fs.open(index, "rb") as fh:
            first = str(fh.read(), "utf-8")
        return first if self.fs.exists(self.resolve(first)) else None

    def put(
        self,
        *,
        source: str,
        dataset: str,
        run_id: str,
        seq: int,
        content: bytes,
        ext: str,
        fetched_at: datetime,
        meta: dict[str, Any],
    ) -> RawRef:
        sha = hashlib.sha256(content).hexdigest()
        d = fetched_at.astimezone(UTC)
        rel = f"{source}/{dataset}/{d:%Y}/{d:%m}/{d:%d}/{run_id}-{seq}.{ext}"
        uri, path = SCHEME + rel, self._path(rel)
        if self.fs.exists(path) or self.fs.exists(path + ".meta.json"):
            raise FileExistsError(f"raw object already exists: {path}")
        sidecar = {**meta, "sha256": sha, "size": len(content), "fetched_at": fetched_at.isoformat()}
        first = self._stored_copy(source, dataset, sha)
        if first is None:
            self._write(path, content)
            self._write(self._index(source, dataset, sha), uri.encode())
        else:
            sidecar["content_uri"] = first
        self._write(path + ".meta.json", json.dumps(sidecar, indent=2, sort_keys=True, default=str).encode())
        return RawRef(uri=uri, sha256=sha, size=len(content))

    def sidecar(self, uri: str) -> dict[str, Any]:
        fs, path = self._fs_path(uri)
        with fs.open(path + ".meta.json", "rb") as fh:
            data: dict[str, Any] = json.loads(fh.read())
        return data

    def _fs_path(self, uri: str) -> tuple[Any, str]:
        if uri.startswith(SCHEME):
            return self.fs, self.resolve(uri)
        fs, path = fsspec.core.url_to_fs(uri)
        return fs, str(path)

    def get(self, uri: str) -> bytes:
        """The bytes of a capture: a `raw://` reference (resolved under this store) or an absolute one."""
        fs, path = self._fs_path(uri)
        if not fs.exists(path):
            content_uri = self.sidecar(uri).get("content_uri")
            if not content_uri:
                raise FileNotFoundError(f"raw object not found: {uri}")
            fs, path = self._fs_path(content_uri)
        with fs.open(path, "rb") as fh:
            data: bytes = fh.read()
        return data
