"""Loaded repository state shared by runtime functions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from scanner import REPO_ROOT
from scanner.core.codebook import CodebookEntry, CodebookFile, load_entries
from scanner.core.manifest import Manifest, load_manifests
from scanner.core.registry import Country, load_countries


@dataclass(frozen=True)
class RepoState:
    root: Path
    countries: dict[str, Country]
    manifests: list[Manifest]
    entries: dict[str, tuple[CodebookFile, CodebookEntry]]

    def entries_for(self, manifest: Manifest) -> list[CodebookEntry]:
        return [self.entries[e][1] for e in manifest.entries]


@lru_cache(maxsize=1)
def repo_state(root: Path = REPO_ROOT) -> RepoState:
    return RepoState(
        root=root,
        countries=load_countries(root),
        manifests=load_manifests(root),
        entries=load_entries(root),
    )
