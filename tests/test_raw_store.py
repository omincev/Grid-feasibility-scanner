"""Raw store: store-relative references and content stored once (scanner.core.raw_store)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scanner.core.raw_store import RawStore, relative_uri

T = datetime(2026, 10, 18, 12, tzinfo=UTC)


def put(store: RawStore, run_id: str, content: bytes, **meta: str) -> str:
    ref = store.put(
        source="src", dataset="ds", run_id=run_id, seq=0, content=content, ext="json", fetched_at=T, meta=dict(meta)
    )
    return ref.uri


def files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_references_are_relative_and_resolve_after_the_store_moves(tmp_path: Path) -> None:
    store = RawStore((tmp_path / "a").as_uri())
    uri = put(store, "r1", b'{"x": 1}', url="https://example.test")
    assert uri == "raw://src/ds/2026/10/18/r1-0.json"
    assert (tmp_path / "a" / "raw" / "src" / "ds" / "2026" / "10" / "18" / "r1-0.json").read_bytes() == b'{"x": 1}'
    shutil.move(tmp_path / "a", tmp_path / "b")
    moved = RawStore(f"file://{(tmp_path / 'b').as_posix()}")
    assert moved.get(uri) == b'{"x": 1}'
    assert moved.sidecar(uri)["url"] == "https://example.test"


def test_absolute_references_are_still_readable(tmp_path: Path) -> None:
    store = RawStore((tmp_path / "a").as_uri())
    uri = put(store, "r1", b"abc")
    absolute = (tmp_path / "a" / "raw" / "src" / "ds" / "2026" / "10" / "18" / "r1-0.json").as_uri()
    assert store.get(absolute) == b"abc" and relative_uri(absolute) == uri


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (
            "file://C:/Users/x/grid-scanner/.data/raw/raw/energidataservice/DayAheadPrices/2026/10/18/ab-0.json",
            "raw://energidataservice/DayAheadPrices/2026/10/18/ab-0.json",
        ),
        (
            "file:///data/raw/raw/snapshots/dk.eds.meta.DayAheadPrices/2026/10/18/ab-0.json",
            "raw://snapshots/dk.eds.meta.DayAheadPrices/2026/10/18/ab-0.json",
        ),
        (
            "s3://bucket/prefix/raw/entsoe/EnergyPrices/2026/01/02/ab-3.xml",
            "raw://entsoe/EnergyPrices/2026/01/02/ab-3.xml",
        ),
        ("raw://entsoe/EnergyPrices/2026/01/02/ab-3.xml", "raw://entsoe/EnergyPrices/2026/01/02/ab-3.xml"),
        ("file://raw/test/report.pdf", None),  # not in the store layout
        ("s3://.../raw/...", None),
        ("https://example.test/raw/a/b/2026/10/18/x.json", None),  # a web address, not a capture
    ],
)
def test_relative_uri(uri: str, expected: str | None) -> None:
    assert relative_uri(uri) == expected


def test_identical_content_is_stored_once_with_a_sidecar_per_capture(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = RawStore(root.as_uri())
    first = put(store, "r1", b"same", url="u1")
    second = put(RawStore(root.as_uri()), "r2", b"same", url="u2")  # another process, same store
    other = put(store, "r3", b"different")
    assert files(root) == [
        "raw/src/ds/2026/10/18/r1-0.json",
        "raw/src/ds/2026/10/18/r1-0.json.meta.json",
        "raw/src/ds/2026/10/18/r2-0.json.meta.json",
        "raw/src/ds/2026/10/18/r3-0.json",
        "raw/src/ds/2026/10/18/r3-0.json.meta.json",
        *sorted(f"sha256/src/ds/{s}" for s in (store.sidecar(first)["sha256"], store.sidecar(other)["sha256"])),
    ]
    assert store.get(first) == store.get(second) == b"same" and store.get(other) == b"different"
    meta = store.sidecar(second)
    assert meta["url"] == "u2" and meta["content_uri"] == first and meta["size"] == 4
    assert "content_uri" not in store.sidecar(first)


def test_content_is_stored_again_when_the_first_copy_is_gone(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = RawStore(root.as_uri())
    first = put(store, "r1", b"same")
    (root / "raw" / "src" / "ds" / "2026" / "10" / "18" / "r1-0.json").unlink()
    second = put(store, "r2", b"same")
    assert "content_uri" not in store.sidecar(second) and store.get(second) == b"same"
    with pytest.raises(FileNotFoundError):
        store.get(first)


def test_captures_are_write_once(tmp_path: Path) -> None:
    store = RawStore(tmp_path.as_uri())
    put(store, "r1", b"a")
    with pytest.raises(FileExistsError):
        put(store, "r1", b"a")


def test_sidecar_is_json_with_hash_and_size(tmp_path: Path) -> None:
    store = RawStore(tmp_path.as_uri())
    uri = put(store, "r1", b"abc", url="u")
    meta = json.loads((tmp_path / "raw" / "src" / "ds" / "2026" / "10" / "18" / "r1-0.json.meta.json").read_text())
    assert meta == store.sidecar(uri)
    assert meta["sha256"] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" and meta["size"] == 3
