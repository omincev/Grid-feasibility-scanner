"""Method cards (docs/methods/<method_id>.md) and the method references in code.

A card documents one calculation for field experts: YAML front matter between two `---` lines
(MethodCard) and a body in plain words. Code that implements a method declares it once with
MethodRef.declare (ID, version and the `<role>.<parameter>` names it resolves through the scenario's
assumption sets); every output row records the method ID and version. Declaring registers the method in
REFERENCED, so scanner.core.contracts.check_methods can fail when code references a method without a card,
with another version or other parameters, and when a card's parameters do not resolve in every scenario.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from scanner.core.registry import PARAMETER_REF, SLUG_ID

METHODS_DIR = ("docs", "methods")
_FRONT_MATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)


class MethodCard(BaseModel):
    """The front matter of a method card, plus its markdown body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=SLUG_ID)
    version: int = Field(ge=1)
    title: str
    status: Literal["provisional", "approved"]
    owner: list[str] = Field(min_length=1)
    inputs: list[str]  # codebook equivalence classes or upstream outputs (feature keys, tables)
    parameters: list[str]  # <role>.<parameter>, resolved through the scenario's assumption sets
    outputs: list[str]
    known_biases: list[str]
    body: str

    @field_validator("parameters")
    @classmethod
    def _parameter_refs(cls, refs: list[str]) -> list[str]:
        for r in refs:
            if not re.match(PARAMETER_REF, r):
                raise ValueError(f"parameter {r!r} must have the form <role>.<parameter>")
        return refs


def parse_card(text: str) -> MethodCard:
    """A card from the text of its file; ValueError without front matter."""
    m = _FRONT_MATTER.match(text)
    if m is None:
        raise ValueError("a method card starts with YAML front matter between two '---' lines")
    front = yaml.safe_load(m[1]) or {}
    if not isinstance(front, dict) or "body" in front:
        raise ValueError("front matter must be a mapping without a 'body' key")
    return MethodCard(**front, body=m[2].strip() + "\n")


def card_paths(root: Path) -> list[Path]:
    """docs/methods/*.md except README.md."""
    return [p for p in sorted(root.joinpath(*METHODS_DIR).glob("*.md")) if p.name != "README.md"]


def load_method_cards(root: Path) -> dict[str, MethodCard]:
    out: dict[str, MethodCard] = {}
    for path in card_paths(root):
        card = parse_card(path.read_text(encoding="utf-8"))
        if path.stem != card.id:
            raise ValueError(f"{path}: file name must equal method id {card.id}")
        out[card.id] = card
    return out


REFERENCED: dict[str, MethodRef] = {}


@dataclass(frozen=True)
class MethodRef:
    """A method as implemented in code. Code declares the methods it implements with MethodRef.declare."""

    id: str
    version: int
    parameters: tuple[str, ...] = ()

    @classmethod
    def declare(cls, id: str, version: int, parameters: tuple[str, ...] = ()) -> MethodRef:
        """A MethodRef, registered in REFERENCED for the contract check."""
        ref = cls(id, version, parameters)
        if REFERENCED.setdefault(id, ref) != ref:
            raise ValueError(f"method {id} is declared twice with different versions or parameters")
        return ref

    def names(self, role: str) -> tuple[str, ...]:
        """The parameter names this method resolves from the set of one role."""
        return tuple(p.split(".", 1)[1] for p in self.parameters if p.split(".", 1)[0] == role)

    def record(self) -> dict[str, Any]:
        return {"method_id": self.id, "version": self.version}
