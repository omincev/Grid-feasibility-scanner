"""Codebook models and loader (ADR 0012)."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scanner.core.sensitivity import Sensitivity
from scanner.core.time import parse_duration

Resample = Literal["sum", "mean", "first", "last", "none"]
Kind = Literal["actual", "forecast", "settlement", "capacity"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IssuedAtField(_Strict):
    rule: Literal["field"]
    field: str


class IssuedAtFixedLocalTime(_Strict):
    rule: Literal["fixed_local_time"]
    local_time: time
    day_offset: int


class IssuedAtOffset(_Strict):
    rule: Literal["offset"]
    offset: str

    @model_validator(mode="after")
    def _check(self) -> IssuedAtOffset:
        parse_duration(self.offset)
        return self


IssuedAtRule = Annotated[IssuedAtField | IssuedAtFixedLocalTime | IssuedAtOffset, Field(discriminator="rule")]


class Definition(_Strict):
    source_quote: str | None
    source_url: str | None
    ours: str


class CodebookEntry(_Strict):
    id: str = Field(pattern=r"^[a-z]{2}\.[a-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+(\.[a-z0-9_]+)?$")
    field: str
    dimensions: dict[str, str] = {}
    equivalence_class: str
    kind: Kind
    unit: str
    sign_convention: str | None = None
    resample: Resample
    issued_at: IssuedAtRule | None = None
    sensitivity: Sensitivity = "public"
    definition: Definition
    notes: str | None = None

    @model_validator(mode="after")
    def _forecast_needs_issue_time(self) -> CodebookEntry:
        if self.kind == "forecast" and self.issued_at is None:
            raise ValueError(f"{self.id}: forecast entries need an issued_at rule (ADR 0003)")
        if self.kind != "forecast" and self.issued_at is not None:
            raise ValueError(f"{self.id}: only forecast entries may have an issued_at rule")
        return self


class NotIngested(_Strict):
    field: str
    reason: str


class CodebookFile(_Strict):
    source: str
    dataset: str
    country: str
    status: Literal["draft", "approved"]
    verified_by: str | None
    verified_on: date | None
    entries: list[CodebookEntry]
    not_ingested: list[NotIngested] = []

    @model_validator(mode="after")
    def _approved_needs_verifier(self) -> CodebookFile:
        if self.status == "approved" and not (self.verified_by and self.verified_on):
            raise ValueError(f"{self.source}.{self.dataset}: approved files need verified_by and verified_on")
        ids = [e.id for e in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.source}.{self.dataset}: duplicate entry ids")
        return self


class EquivalenceClass(_Strict):
    dimension: str
    description: str


def load_classes(root: Path) -> dict[str, EquivalenceClass]:
    raw: dict[str, Any] = yaml.safe_load((root / "codebook" / "classes.yaml").read_text())
    return {k: EquivalenceClass(**v) for k, v in raw["classes"].items()}


def load_codebook_files(root: Path) -> dict[Path, CodebookFile]:
    out: dict[Path, CodebookFile] = {}
    for path in sorted((root / "codebook").glob("*/*.yaml")):
        out[path] = CodebookFile(**yaml.safe_load(path.read_text()))
    return out


def load_entries(root: Path) -> dict[str, tuple[CodebookFile, CodebookEntry]]:
    """All entries by id, with their file header (source, dataset, status)."""
    entries: dict[str, tuple[CodebookFile, CodebookEntry]] = {}
    for path, f in load_codebook_files(root).items():
        for e in f.entries:
            if e.id in entries:
                raise ValueError(f"duplicate codebook id {e.id} in {path}")
            entries[e.id] = (f, e)
    return entries
