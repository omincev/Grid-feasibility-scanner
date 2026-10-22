"""Source adapters. One package per source; manifests in <source>/manifests/*.yaml."""

from __future__ import annotations

import importlib

from scanner.adapters.base import Adapter


def load_adapter(path: str) -> Adapter:
    """Instantiate an adapter from a manifest's 'module.path:ClassName'."""
    module_name, class_name = path.split(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    adapter: Adapter = cls()
    return adapter
