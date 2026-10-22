"""Country-agnostic acceptance test (ADR 0011, WP3.2). Permanent in CI."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import yaml
from conftest import FIXTURES, insert_scenario, score_method_parameters, zones_root
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from scanner import REPO_ROOT
from scanner.adapters.base import FetchResult
from scanner.adapters.entsoe import EntsoeAdapter
from scanner.core import db
from scanner.core.manifest import load_manifests
from scanner.core.registry import load_countries
from scanner.core.time import local_day_bounds
from scanner.features.run import run_features
from scanner.runtime.context import RepoState
from scanner.scores.run import run_congestion_scores

NL_EIC = "10YNL----------L"


def test_nl_is_configuration_only(root: Path) -> None:
    nl = load_countries(root)["nl"]
    assert nl.status == "active"
    assert all(z.eic for z in nl.zones), "base-tier countries need EIC codes for ENTSO-E"
    assert set(nl.precedence.get("day_ahead_price", [])) == {"entsoe"}


def test_nl_entsoe_series_resolve_from_its_country_file(state: RepoState) -> None:
    """Every ENTSO-E manifest covers NL through nl.yaml alone, and a recorded NL response becomes an
    nl.zone.nl series of a class F1 reads, with no NL-specific code."""
    base = [m for m in state.manifests if m.source == "entsoe"]
    assert base and all(m.zones.get(NL_EIC) == "nl.zone.nl" for m in base)
    prices = next(m for m in base if m.dataset == "EnergyPrices")
    content = (FIXTURES / "entsoe" / "day_ahead_prices__nl__2026-10-14.xml").read_bytes()
    result = FetchResult(
        url="fixture",
        params={"periodStart": "202610132200", "periodEnd": "202610142200"},
        status=200,
        content_type="text/xml",
        content=content,
        fetched_at=datetime(2026, 10, 18, tzinfo=UTC),
    )
    adapter = EntsoeAdapter(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(599))), SecretStr("x"))
    batch = adapter.parse(
        prices,
        state.countries["eu"],
        state.entries_for(prices),
        result,
        datetime(2026, 10, 13, 22, tzinfo=UTC),
        datetime(2026, 10, 14, 22, tzinfo=UTC),
    )
    assert {(i.entity_id, i.entry.equivalence_class) for i in batch.series.values()} == {
        ("nl.zone.nl", "day_ahead_price")
    }
    assert len(batch.rows) == 96


def test_a_new_country_file_is_the_whole_change(root: Path, tmp_path: Path) -> None:
    """Adding registry/countries/<cc>.yaml puts its zones into the base-tier manifests (copied as is)."""
    shutil.copytree(root / "registry" / "countries", tmp_path / "registry" / "countries")
    manifests = Path("src", "scanner", "adapters", "entsoe", "manifests")
    shutil.copytree(root / manifests, tmp_path / manifests)
    (tmp_path / "registry" / "countries" / "be.yaml").write_text(
        "code: be\nname: Belgium\nstatus: active\ntimezone: Europe/Brussels\ncurrency: EUR\n"
        "reporting_currency: EUR\ntso: Elia\ngeography_levels: [country, zone]\n"
        "zones:\n  - {id: be.zone.be, short: BE, name: Belgium, eic: 10YBE----------2, eic_verified: true}\n"
        "precedence: {day_ahead_price: [entsoe]}\nreference_zones: []\n",
        encoding="utf-8",
    )
    loaded = load_manifests(tmp_path)
    assert loaded and all(m.zones.get("10YBE----------2") == "be.zone.be" for m in loaded)


CPH = "Europe/Copenhagen"
# Test-only zone IDs: the countries' real zones may already hold pipeline data in the database.
NL_ZONE, DE_ZONE, HU_ZONE, NO2_ZONE = "nl.zone.t_nl", "de.zone.t_de_lu", "hu.zone.t_hu", "no.zone.t_no2"
PRICE = "eu.entsoe.EnergyPrices.price_amount"
TAG = "test_agnostic"
WEIGHTS_SET, METHOD_SET = "test_agnostic_weights", "test_agnostic_method"
SCENARIO = "test_agnostic_scenario"
WEIGHTS = {
    "f7_substation_saturation": 20,
    "f1_price_spread": 15,
    "f2_curtailment": 15,
    "f4_surplus": 15,
    "f5_balancing_activation": 15,
    "f3_interconnector_saturation": 10,
    "f6_renewable_pipeline": 10,
}
SEP30 = local_day_bounds(date(2025, 9, 30), CPH)[0]
OCT1 = local_day_bounds(date(2025, 10, 1), CPH)[0]
H, Q = timedelta(hours=1), timedelta(minutes=15)


def _test_zones_root(base: Path) -> Path:
    """A repository root for the run: Denmark with a test zone that has no data (the run country, so
    the Danish market rules and local days apply as in the real run) beside the repository's EU, NL, DE,
    HU and NO country files with every zone renamed to its test-only ID (`<cc>.zone.t_<name>`). Runs over it
    read only the test series, whatever the database already holds; status and precedence are the real files'."""
    root = zones_root(base, ["dk.zone.t_dk"])
    for cc in ("eu", "nl", "de", "hu", "no"):
        data = yaml.safe_load((REPO_ROOT / "registry" / "countries" / f"{cc}.yaml").read_text(encoding="utf-8"))
        for z in data["zones"]:
            z["id"] = z["id"].replace(".zone.", ".zone.t_")
        (root / "registry" / "countries" / f"{cc}.yaml").write_text(
            yaml.safe_dump(data), encoding="utf-8", newline="\n"
        )
    return root


@pytest.fixture()
def score_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, list[str]]]:
    """The test database URL (minimal reference rows inserted) and a list of run IDs to delete afterwards."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("GIT_SHA", "testsha")
    run_ids: list[str] = []
    with psycopg.connect(url) as conn:
        _cleanup(conn, run_ids)
        _reference(conn)
    yield url, run_ids
    with psycopg.connect(url) as conn:
        _cleanup(conn, run_ids)


def _cleanup(conn: psycopg.Connection[Any], run_ids: list[str]) -> None:
    runs = "select run_id from ops.compute_run where run_id = any(%(ids)s) or params->>'weights_set' = %(set)s"
    p = {"ids": [uuid.UUID(r) for r in run_ids], "set": WEIGHTS_SET}
    conn.execute(f"delete from feat.score where run_id in ({runs})", p)
    conn.execute(f"delete from feat.feature_value where run_id in ({runs})", p)
    conn.execute(f"delete from ops.compute_run where run_id in ({runs})", p)
    conn.execute("delete from ref.scenario where scenario_id = %s", (SCENARIO,))
    conn.execute("delete from ref.assumption_set where set_id = any(%s)", ([WEIGHTS_SET, METHOD_SET],))
    series = "select series_id from core.series where series_key like %s"
    conn.execute(f"delete from core.observation where series_id in ({series})", (f"{TAG}.%",))
    conn.execute("delete from core.series where series_key like %s", (f"{TAG}.%",))
    conn.execute("delete from ref.entity where entity_id = any(%s)", ([NL_ZONE, DE_ZONE, HU_ZONE, NO2_ZONE],))
    conn.commit()


def _reference(conn: psycopg.Connection[Any]) -> None:
    """What `load-curated` would load from the repository for these zones, the ENTSO-E price entry and a
    weights and score-method set with a scenario naming them; nothing NL-specific beyond nl.yaml's own values."""
    countries = load_countries(REPO_ROOT)
    for cc in ("eu", "nl", "de", "hu", "no"):
        c = countries[cc]
        conn.execute(
            "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
            "values (%s, %s, %s, %s, %s, '{}', 't', 't') on conflict do nothing",
            (cc, c.name, c.status, c.timezone, c.currency),
        )
    for zone in (NL_ZONE, DE_ZONE, HU_ZONE, NO2_ZONE):
        conn.execute(
            "insert into ref.entity (entity_id, kind, country_code, name) values (%s, 'zone', %s, %s) "
            "on conflict do nothing",
            (zone, zone.split(".")[0], zone),
        )
    conn.execute("insert into ref.equivalence_class values ('day_ahead_price', 'x', 'x') on conflict do nothing")
    conn.execute(
        "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, equivalence_class, kind, "
        "unit, resample, definition, status, source_path, content_hash) values (%s, 'entsoe', 'EnergyPrices', "
        "'eu', 'price_amount', 'day_ahead_price', 'actual', 'EUR/MWh', 'mean', '{}', 'draft', 't', 't') "
        "on conflict do nothing",
        (PRICE,),
    )
    for set_id, params in ((WEIGHTS_SET, {"weights": {"value": WEIGHTS}}), (METHOD_SET, score_method_parameters())):
        conn.execute(
            "insert into ref.assumption_set (set_id, kind, status, owners, parameters, source_path, content_hash) "
            "values (%s, 'test', 'proposed', '{owner}', %s, 'test', 'hash')",
            (set_id, Jsonb(params)),
        )
    insert_scenario(conn, SCENARIO, {"score_weights": WEIGHTS_SET, "score_method": METHOD_SET})
    conn.commit()


def _prices(conn: psycopg.Connection[Any], zone: str, start: datetime, step: timedelta, values: list[float]) -> None:
    """One ENTSO-E price series per zone and resolution, as the adapter stores them (ADR 0002)."""
    key = f"{TAG}.{zone}.price_amount.pt{int(step.total_seconds() // 60)}m"
    sid = db.ensure_series(conn, db.SeriesSpec(key, PRICE, zone, "EUR/MWh", step, "actual", "public", None))
    rows = [db.ObservationRow(key, start + i * step, start + (i + 1) * step, None, v) for i, v in enumerate(values)]
    db.write_observations(conn, rows, {key: sid}, uuid.uuid4())
    conn.commit()


@pytest.mark.db
def test_nl_produces_a_score_from_config_alone(score_db: tuple[str, list[str]], tmp_path: Path) -> None:
    """ADR 0011 / WP3.2: with ENTSO-E prices in the database, the default feature run and the congestion
    score cover NL through nl.yaml alone (status active, precedence entsoe): F1 and F2's non-positive price
    share, percentiles in the population pooled over every zone of active and reference countries, weights
    renormalised over the features NL has. Draft countries' zones stay out of populations and scores, and
    one country's unusable rows do not cost the other zones their features."""
    url, run_ids = score_db
    root = _test_zones_root(tmp_path)
    with psycopg.connect(url) as conn:
        # NL: PT60M to 2025-09-30, PT15M from 2025-10-01. Daily hourly spreads 23, 10, 10; non-positive
        # intervals: 3 hours on 30 Sep and one quarter-hour on 2 Oct.
        _prices(conn, NL_ZONE, SEP30, H, [float(i - 2) for i in range(24)])
        _prices(conn, NL_ZONE, OCT1, Q, [40.0] * 40 + [80.0] + [40.0] * 55 + [30.0] * 95 + [-10.0])
        # DE-LU publishes PT15M before 2025-10-01 too (as ENTSO-E does for NO2 and the Italian zones): no
        # market rule of DE's limits its resolution, and Denmark's does not apply to it. Spreads 0, 0, 0;
        # every interval of 2 Oct is at 0 EUR/MWh.
        _prices(conn, DE_ZONE, SEP30, Q, [50.0] * 96 + [60.0] * 96 + [0.0] * 96)
        # HU (draft country) would move every percentile if it joined the population.
        _prices(conn, HU_ZONE, SEP30, H, [0.0, 1000.0] * 12)
        _prices(conn, HU_ZONE, OCT1, Q, [-50.0] * 192)
        # NO2: overlapping PT60M and PT15M series of one source (as stored from ENTSO-E for May 2025): using
        # them would need a guess, so NO2 is left out of the run and noted.
        _prices(conn, NO2_ZONE, SEP30, H, [20.0] * 24)
        _prices(conn, NO2_ZONE, SEP30, Q, [20.0] * 96)

        features = run_features(
            ["f1_price_spread", "f2_curtailment"], database_url=url, end_day=date(2025, 10, 3), root=root
        )
        run_ids += [o.run_id for o in features]
        scores = run_congestion_scores(database_url=url, scenario=SCENARIO, allow_proposed=False, root=root)
        run_ids.append(scores.run_id)
        f1_run = features[0]
        assert f1_run.status == "partial" and any(
            n.startswith(f"input price unusable for {NO2_ZONE}") for n in f1_run.notes
        )

        written = conn.execute(
            "select distinct entity_id from feat.feature_value where run_id = any(%s)",
            ([uuid.UUID(o.run_id) for o in features],),
        ).fetchall()
        assert sorted(e for (e,) in written) == [DE_ZONE, NL_ZONE]
        rows = {
            e: (v, b)
            for e, v, b in conn.execute(
                "select entity_id, value, breakdown from feat.score where run_id = %s", (uuid.UUID(scores.run_id),)
            )
        }
        assert NL_ZONE in rows and DE_ZONE not in rows and HU_ZONE not in rows
        value, b = rows[NL_ZONE]
        assert value == pytest.approx(75.0)  # (83.3 + 66.7) / 2
        f1, f2 = b["features"]["f1_intraday_spread_hourly"], b["features"]["f2_nonpositive_price_share"]
        assert set(b["features"]) == {"f1_intraday_spread_hourly", "f2_nonpositive_price_share"}
        assert b["missing_features"] == sorted(k for k in WEIGHTS if k not in ("f1_price_spread", "f2_curtailment"))
        assert (f1["weight"], f2["weight"]) == (pytest.approx(50.0), pytest.approx(50.0))
        # F1 raw 43/3 among daily spreads {23, 10, 10, 0, 0, 0}: 5 below. F2 raw 11700/259200 among daily
        # shares {3/24, 0, 1/96, 0, 0, 1}: 4 below.
        assert f1["raw"] == pytest.approx(43 / 3) and f1["percentile"] == pytest.approx(500 / 6)
        assert f2["raw"] == pytest.approx(11700 / 259200) and f2["percentile"] == pytest.approx(400 / 6)
        for f in (f1, f2):
            assert f["population"]["entities"] == [DE_ZONE, NL_ZONE] and f["population"]["n"] == 6
