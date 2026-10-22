"""Shared fixtures. DB tests (marked db) must pass on an empty migrated database and on one holding real data:
they write only test-only IDs (sources test_*, zones such as dk.zone.test_* via zones_root), read back only
those, and roll back or delete what they wrote, reference rows included."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from scanner import REPO_ROOT
from scanner.core.registry import load_assumptions
from scanner.runtime.context import RepoState, repo_state

FIXTURES = REPO_ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _fresh_entsoe_response_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backfill response reuse is process-wide state; no test may see another test's responses."""
    from scanner.adapters.entsoe import adapter as entsoe_adapter

    monkeypatch.setattr(entsoe_adapter, "_reusable_responses", {})


@pytest.fixture(scope="session")
def root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def state() -> RepoState:
    return repo_state()


@pytest.fixture()
def analytics_url() -> Iterator[str]:
    """A connection URL for a temporary login role in scanner_analytics (migration 20261018130000), the way
    ANALYTICS_DATABASE_URL points the analytics location at one. The role is dropped afterwards."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    name, password = f"test_analytics_{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("create role {} login password {} in role scanner_analytics").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
    yield make_conninfo(**{**conninfo_to_dict(url), "user": name, "password": password})
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("drop role {}").format(sql.Identifier(name)))


def zones_root(base: Path, zones: Sequence[str]) -> Path:
    """A repository root for DB tests of feature, score and valuation runs: Denmark as the only country,
    with the given test-only zones (no reference zones) and the real Danish market rules. Runs over it read
    only the test zones, so data the pipeline wrote for real zones never enters the test's results."""
    countries = base / "registry" / "countries"
    countries.mkdir(parents=True)
    country = {
        "code": "dk",
        "name": "Denmark (test zones)",
        "status": "active",
        "timezone": "Europe/Copenhagen",
        "currency": "DKK",
        "reporting_currency": "EUR",
        "tso": None,
        "geography_levels": ["country", "zone", "municipality"],
        "zones": [{"id": z, "short": z.split(".")[-1], "name": z, "eic": None, "eic_verified": False} for z in zones],
        "precedence": {},
        "reference_zones": [],
    }
    (countries / "dk.yaml").write_text(yaml.safe_dump(country), newline="\n")
    (base / "market_rules").mkdir()
    shutil.copyfile(REPO_ROOT / "market_rules" / "dk.yaml", base / "market_rules" / "dk.yaml")
    return base


def score_method_parameters() -> dict[str, dict[str, Any]]:
    """The repository's score method (assumptions/score_method_v0.yaml, the spec_v0 behaviour) with its
    proposed values as approved values, for the parameters of a test set in ref.assumption_set."""
    s = load_assumptions(REPO_ROOT)["score_method_v0"]
    return {k: {"value": p.proposed} for k, p in s.parameters.items()}


def insert_scenario(
    conn: psycopg.Connection[Any],
    scenario_id: str,
    sets: Mapping[str, str],
    *,
    status: str = "exploratory",
    method_switches: Mapping[str, Any] | None = None,
    approved: bool = False,
) -> None:
    """A scenario row as load-curated writes it (for DB tests; IDs start with test_)."""
    conn.execute(
        "insert into ref.scenario (scenario_id, title, status, description, owners, approved_by, approved_on, sets, "
        "method_switches, overrides, source_path, content_hash) "
        "values (%s, %s, %s, 'test', '{owner}', %s, %s, %s, %s, '{}', 'test', %s)",
        (
            scenario_id,
            scenario_id,
            status,
            "owner" if approved else None,
            "2026-10-18" if approved else None,
            Jsonb(dict(sets)),
            Jsonb(dict(method_switches or {})),
            f"hash-{scenario_id}",
        ),
    )
