"""Operational alerts (scanner.runtime.alerts) and the `ops` location's health sensor.

Offline: webhook formats through an httpx.MockTransport, problem keys and their changes, and a sensor tick with
the database replaced by a fake. The webhook URLs are SYNTHETIC. Database test (marked db): the two queries run
against the real serving views.
"""

from __future__ import annotations

import json
import os
from collections import namedtuple
from datetime import UTC, date, datetime
from typing import Any

import httpx
import psycopg
import pytest
from dagster import DefaultSensorStatus, SkipReason, build_sensor_context

from scanner import ops_definitions
from scanner.runtime import alerts

NTFY = "https://ntfy.example.org/synthetic-topic"
SLACK = "https://hooks.slack.com/services/T000/B000/synthetic"
DISCORD = "https://discord.com/api/webhooks/000/synthetic"


def _settings(url: str | None = NTFY, **kwargs: Any) -> alerts.AlertSettings:
    return alerts.AlertSettings(alert_webhook_url=url, **kwargs)


def _client(seen: list[httpx.Request], status: int = 200) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_plain_text_with_a_title_header_for_ntfy_and_other_receivers() -> None:
    seen: list[httpx.Request] = []
    assert alerts.post(_settings(), "Run failed", "line 1\nline 2 ø", client=_client(seen))
    assert seen[0].headers["Title"] == "Run failed"
    assert seen[0].content.decode() == "line 1\nline 2 ø"


def test_slack_gets_json_by_url_or_by_setting() -> None:
    for settings in (_settings(SLACK), _settings(NTFY, alert_webhook_format="slack")):
        seen: list[httpx.Request] = []
        alerts.post(settings, "Health", "text", client=_client(seen))
        assert seen[0].headers["Content-Type"] == "application/json"
        assert json.loads(seen[0].content) == {"text": "*Health*\ntext"}


def test_discord_gets_json_content_by_url_or_by_setting() -> None:
    for settings in (_settings(DISCORD), _settings(NTFY, alert_webhook_format="discord")):
        seen: list[httpx.Request] = []
        alerts.post(settings, "Health", "text", client=_client(seen, status=204))
        assert seen[0].headers["Content-Type"] == "application/json"
        assert json.loads(seen[0].content) == {"content": "**Health**\ntext", "allowed_mentions": {"parse": []}}


def test_discord_content_is_cut_to_the_limit() -> None:
    _, body = alerts.webhook_request(DISCORD, "auto", "Health", "x" * 5000)
    content = json.loads(body)["content"]
    assert len(content) == alerts.DISCORD_LIMIT and content.endswith("…")


def test_nothing_is_sent_without_a_webhook() -> None:
    seen: list[httpx.Request] = []
    assert not alerts.post(_settings(None), "t", "x", client=_client(seen))
    assert not alerts.post(_settings(""), "t", "x", client=_client(seen))
    assert seen == []


def test_an_http_error_raises_without_the_url() -> None:
    with pytest.raises(RuntimeError) as exc:
        alerts.post(_settings(), "t", "x", client=_client([], status=500))
    assert "500" in str(exc.value) and "synthetic" not in str(exc.value)


class FakeConn:
    """Answers the stale query, then the gaps query, like a psycopg connection."""

    def __init__(self, stale: list[tuple[Any, ...]], gaps: list[tuple[Any, ...]]):
        self.answers = {alerts.STALE_SQL: stale, alerts.GAPS_SQL: gaps}

    def execute(self, query: Any, params: Any = None) -> Any:
        rows = self.answers[query]
        return type("Cursor", (), {"fetchall": lambda self: rows})()


def test_stale_sources_and_recent_gaps_become_keyed_problems() -> None:
    conn = FakeConn(
        stale=[("eds", "ImbalancePrice", 3.5, 2.0, "failed"), ("entsoe", "EnergyPrices", None, None, None)],
        gaps=[("eds", "Forecasts_Hour", ["2026-09-01", "2026-10-16"], ["2026-10-17"])],
    )
    problems = alerts.source_problems(conn, date(2026, 10, 18), gap_days=7)
    assert [p.key for p in problems] == [
        "stale:eds.ImbalancePrice",
        "stale:entsoe.EnergyPrices",
        "gap:eds.Forecasts_Hour:2026-10-16",
        "gap:eds.Forecasts_Hour:2026-10-17",
    ]
    assert "3.5 h ago (SLA 2 h), last attempt failed" in problems[0].text
    assert "never (no SLA)" in problems[1].text
    assert (problems[3].group, problems[3].text) == ("eds.Forecasts_Hour incomplete", "2026-10-17")


def test_a_message_lists_a_datasets_days_on_one_line() -> None:
    new = [
        alerts.Problem("stale:a.b", "a.b is stale"),
        alerts.Problem("gap:c.d:2026-10-16", "2026-10-16", "c.d missing"),
        alerts.Problem("gap:c.d:2026-10-17", "2026-10-17", "c.d missing"),
    ]
    resolved = ["gap:e.f:2026-10-10", "gap:e.f:2026-10-11", "stale:g.h"]
    assert alerts.changes_text(new, resolved, 3) == (
        "New (3):\n- a.b is stale\n- c.d missing: 2026-10-16, 2026-10-17\n"
        "Resolved (3):\n- gap:e.f: 2026-10-10, 2026-10-11\n- stale:g.h\nOpen problems now: 3"
    )


def test_disk_problem_at_the_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda path: usage(100e9, 85e9, 15e9))
    problem = alerts.disk_problem("/", 85)
    assert problem is not None and problem.key == "disk:/" and "85% full (15.0 GB free" in problem.text
    assert alerts.disk_problem("/", 90) is None


def test_changes_report_new_problems_and_resolved_keys() -> None:
    now = [alerts.Problem("stale:a.b", "a.b is stale"), alerts.Problem("disk:/", "disk 90% full")]
    new, resolved = alerts.changes(["stale:a.b", "gap:c.d:2026-10-17"], now)
    assert [p.key for p in new] == ["disk:/"] and resolved == ["gap:c.d:2026-10-17"]
    assert alerts.changes_text(new, resolved, 2) == (
        "New (1):\n- disk 90% full\nResolved (1):\n- gap:c.d: 2026-10-17\nOpen problems now: 2"
    )


def test_run_failure_text_links_the_run() -> None:
    text = alerts.run_failure_text(
        job="job_compute", run_id="r1", location="analytics", partition=None, message="boom", ui_url="http://x:3000/"
    )
    assert text == "Job job_compute in location analytics failed\nboom\nhttp://x:3000/runs/r1"


def test_sensors_stay_stopped_without_a_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert ops_definitions.default_status() == DefaultSensorStatus.STOPPED


def test_health_sensor_posts_changes_once_and_keeps_the_open_keys_as_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_WEBHOOK_URL", NTFY)
    monkeypatch.setattr(psycopg, "connect", lambda url: _Connected())
    found = [alerts.Problem("stale:a.b", "a.b is stale")]
    monkeypatch.setattr(alerts, "source_problems", lambda conn, today, days: list(found))
    monkeypatch.setattr(alerts, "disk_problem", lambda path, limit: None)
    sent: list[str] = []
    monkeypatch.setattr(alerts, "post", lambda settings, title, text: sent.append(text) or True)

    context = build_sensor_context(cursor=None)
    result = ops_definitions.health_alert(context)
    assert isinstance(result, SkipReason) and sent == ["New (1):\n- a.b is stale\nOpen problems now: 1"]
    assert json.loads(context.cursor or "") == ["stale:a.b"]

    ops_definitions.health_alert(context)  # unchanged: nothing sent
    found.clear()
    ops_definitions.health_alert(context)
    assert len(sent) == 2 and sent[1].startswith("Resolved (1):\n- stale:a.b")
    assert json.loads(context.cursor or "") == []


class _Connected:
    read_only = False

    def __enter__(self) -> _Connected:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.db
def test_alert_queries_run_on_the_serving_views() -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    with psycopg.connect(url) as conn:
        conn.read_only = True
        problems = alerts.source_problems(conn, datetime.now(UTC).date(), gap_days=7)
    assert all(p.key.startswith(("stale:", "gap:")) for p in problems)
