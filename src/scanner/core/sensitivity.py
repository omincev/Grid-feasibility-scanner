"""Sensitivity classes (ADR 0008). Derived data takes the most restrictive class of its inputs."""

from __future__ import annotations

from typing import Literal

Sensitivity = Literal["public", "licensed", "nda", "customer", "personal"]
ORDER: tuple[Sensitivity, ...] = ("public", "licensed", "nda", "customer", "personal")


def rank(s: Sensitivity) -> int:
    return ORDER.index(s)


def combine(*classes: Sensitivity) -> Sensitivity:
    """Most restrictive class among the inputs. No inputs means public."""
    if not classes:
        return "public"
    return max(classes, key=rank)


def allowed_for_agents(s: Sensitivity) -> bool:
    """Only public data may enter a factory or research agent's context."""
    return s == "public"
