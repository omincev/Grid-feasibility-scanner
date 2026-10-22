"""Marker for assets whose computation is a planned work package."""

from __future__ import annotations


class NotBuiltYet(NotImplementedError):
    def __init__(self, wp: str, spec: str):
        super().__init__(f"{wp} not built yet; implement per {spec}")
