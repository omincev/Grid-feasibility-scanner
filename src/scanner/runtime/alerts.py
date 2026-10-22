"""Minimal operational alerts (infra/README.md, "Alerts"): a webhook message when a Dagster run fails, when an
active source is stale or has recent gaps, and when the disk fills up. Off unless ALERT_WEBHOOK_URL is set.

Plain functions; the sensors of the `ops` code location (scanner.ops_definitions) call them. Problems are keyed,
so a sensor reports a problem once when it appears and once when it is resolved, not on every tick:

- `stale:<source>.<dataset>`: serving.source_freshness says stale (last success older than the SLA);
- `gap:<source>.<dataset>:<day>`: serving.source_completeness lists the local day as missing or incomplete,
  for days in the last ALERT_GAP_DAYS (older gaps are backfill work, not news);
- `disk:<path>`: usage of the filesystem holding <path> is at or above DISK_ALERT_PERCENT.

The webhook gets plain text with a `Title` header (ntfy and most generic receivers) or, for Slack incoming
webhooks (hooks.slack.com, or ALERT_WEBHOOK_FORMAT=slack), JSON {"text": ...}, or, for Discord webhooks
(discord.com/api/webhooks, or ALERT_WEBHOOK_FORMAT=discord), JSON {"content": ...} cut to Discord's 2000
characters, with mentions disabled. The URL often embeds a token: it is a secret and is never logged.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, Protocol

import httpx
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AlertSettings(BaseSettings):
    """Alert settings from the environment (the server's env file; infra/server-env.template)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    alert_webhook_url: SecretStr | None = None
    alert_webhook_format: Literal["auto", "text", "slack", "discord"] = "auto"
    disk_alert_percent: float = Field(default=85.0, gt=0, le=100)
    # In the containers "/" is the overlay filesystem on the host's single disk.
    disk_alert_path: str = "/"
    alert_gap_days: int = Field(default=7, ge=1)
    # Where the run link in a failure message points: the Dagster UI through an SSH tunnel or Tailscale.
    dagster_ui_url: str = "http://localhost:3000"

    @property
    def enabled(self) -> bool:
        return self.alert_webhook_url is not None and bool(self.alert_webhook_url.get_secret_value())


@dataclass(frozen=True)
class Problem:
    key: str
    text: str
    # Problems of one group are listed on one line in a message ("<group>: <text>, <text>"): a dataset's days.
    group: str | None = None


class Connection(Protocol):
    def execute(self, query: Any, params: Any = ...) -> Any: ...


STALE_SQL = """
select source, dataset, hours_since_success, freshness_sla_hours, last_attempt_status
from serving.source_freshness
where stale and registry_status = 'active'
order by source, dataset
"""

GAPS_SQL = """
select source, dataset, missing_partitions, incomplete_partitions
from serving.source_completeness
where registry_status = 'active' and (partitions_missing > 0 or partitions_incomplete > 0)
order by source, dataset
"""


def _hours(value: float | None) -> str:
    return "never" if value is None else f"{value:.1f} h ago"


def source_problems(conn: Connection, today: date, gap_days: int) -> list[Problem]:
    """Stale active datasets, and missing or incomplete local days of active datasets since today - gap_days."""
    problems = []
    for source, dataset, since, sla, attempt in conn.execute(STALE_SQL).fetchall():
        sla_text = "no SLA" if sla is None else f"SLA {sla:g} h"
        problems.append(
            Problem(
                f"stale:{source}.{dataset}",
                f"{source}.{dataset} is stale: last success {_hours(since)} ({sla_text}), "
                f"last attempt {attempt or 'none'}",
            )
        )
    first = (today - timedelta(days=gap_days)).isoformat()
    for source, dataset, missing, incomplete in conn.execute(GAPS_SQL).fetchall():
        for state, days in (("missing", missing or []), ("incomplete", incomplete or [])):
            problems += [
                Problem(f"gap:{source}.{dataset}:{day}", day, f"{source}.{dataset} {state}")
                for day in days
                if day >= first
            ]
    return problems


def disk_problem(path: str, limit_percent: float) -> Problem | None:
    """A problem when the filesystem holding `path` is at least `limit_percent` full."""
    usage = shutil.disk_usage(path)
    percent = 100 * usage.used / usage.total
    if percent < limit_percent:
        return None
    free_gb = usage.free / 1e9
    return Problem(f"disk:{path}", f"disk {percent:.0f}% full ({free_gb:.1f} GB free; alert at {limit_percent:g}%)")


def changes(previous: Iterable[str], current: Sequence[Problem]) -> tuple[list[Problem], list[str]]:
    """Problems not reported before, and keys reported before that are gone."""
    before = set(previous)
    now = {p.key for p in current}
    return [p for p in current if p.key not in before], sorted(before - now)


def _grouped(items: Iterable[tuple[str | None, str]]) -> list[str]:
    """One line per item, except that items of one group share a line, in order of first appearance."""
    groups: dict[str, list[str]] = {}
    lines: list[str | list[str]] = []
    for group, text in items:
        if group is None:
            lines.append(text)
        elif group in groups:
            groups[group].append(text)
        else:
            groups[group] = [f"{group}: {text}"]
            lines.append(groups[group])
    return [f"- {line if isinstance(line, str) else ', '.join(line)}" for line in lines]


def changes_text(new: Sequence[Problem], resolved: Sequence[str], open_count: int) -> str:
    lines = [f"New ({len(new)}):", *_grouped((p.group, p.text) for p in new)] if new else []
    if resolved:
        gone = ((k.rsplit(":", 1)[0], k.rsplit(":", 1)[1]) if k.startswith("gap:") else (None, k) for k in resolved)
        lines += [f"Resolved ({len(resolved)}):", *_grouped(gone)]
    lines.append(f"Open problems now: {open_count}")
    return "\n".join(lines)


def run_failure_text(
    *, job: str, run_id: str, location: str | None, partition: str | None, message: str | None, ui_url: str
) -> str:
    lines = [f"Job {job} in location {location or '?'} failed" + (f", partition {partition}" if partition else "")]
    if message:
        lines.append(message.strip()[:1500])
    lines.append(f"{ui_url.rstrip('/')}/runs/{run_id}")
    return "\n".join(lines)


DISCORD_LIMIT = 2000  # characters in a Discord message's content
_DISCORD_HOSTS = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")


def webhook_request(url: str, fmt: str, title: str, text: str) -> tuple[dict[str, str], bytes]:
    """Headers and body for the webhook: Slack or Discord JSON, or plain text with a Title header (module
    docstring)."""
    if fmt == "slack" or (fmt == "auto" and "hooks.slack.com" in url):
        return {"Content-Type": "application/json"}, json.dumps({"text": f"*{title}*\n{text}"}).encode()
    if fmt == "discord" or (fmt == "auto" and url.startswith(_DISCORD_HOSTS)):
        content = f"**{title}**\n{text}"
        if len(content) > DISCORD_LIMIT:
            content = content[: DISCORD_LIMIT - 1] + "…"
        body = {"content": content, "allowed_mentions": {"parse": []}}
        return {"Content-Type": "application/json"}, json.dumps(body).encode()
    # Header values must be ASCII; the body keeps the full text.
    headers = {"Title": title.encode("ascii", "replace").decode(), "Content-Type": "text/plain; charset=utf-8"}
    return headers, text.encode()


def post(settings: AlertSettings, title: str, text: str, client: httpx.Client | None = None) -> bool:
    """Send one alert; False (and nothing sent) when no webhook is configured. Raises on an HTTP error, so a
    sensor tick fails visibly and retries instead of losing the alert. Errors never include the URL."""
    if not settings.enabled or settings.alert_webhook_url is None:
        return False
    url = settings.alert_webhook_url.get_secret_value()
    headers, body = webhook_request(url, settings.alert_webhook_format, title, text)
    own = client is None
    http = client or httpx.Client(timeout=20)
    try:
        response = http.post(url, headers=headers, content=body)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"alert webhook request failed: {type(exc).__name__}") from None
    finally:
        if own:
            http.close()
    if response.status_code >= 400:
        raise RuntimeError(f"alert webhook answered HTTP {response.status_code}")
    return True
