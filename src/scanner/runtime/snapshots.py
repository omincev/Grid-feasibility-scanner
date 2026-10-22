"""Snapshot watcher (ADR 0003, 0013; WP1.4).

Captures sources that only show their current state, watched documents and metadata pages. Every
capture goes to the raw store. A changed hash is recorded and, for on_change=research, enqueues a
research bead; on_change=parse:<name> runs that parser (scanner.runtime.snapshot_parsers) on every
successful capture, which writes only facts that changed.

Requests: GET, or POST with a JSON body and headers declared on the registry entry
(scanner.core.registry.Snapshot). A Power BI public report's resource key is read from its embed page
on every capture (`powerbi_embed_page`) instead of being stored.

Change detection can hash a normalised form of the capture, chosen per snapshot, so per-request noise
does not count as a change: `normalise: visible_text` reduces an HTML page to its visible text plus
link and iframe targets (scripts, styles, comments and attributes such as random element ids or
obfuscated e-mail tokens dropped); `ignore_keys` drops JSON keys. Pages that render their content from
scripts keep the default (the bytes as received), since visible text would hide their changes.
ops.snapshot.sha256 holds the hash used; the raw store sidecar keeps it and the hash of the raw bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

import httpx

from scanner.adapters.energinet.connection_queue import powerbi_resource_key
from scanner.core import db
from scanner.core.raw_store import RawStore
from scanner.core.registry import Snapshot
from scanner.runtime.snapshot_parsers import PARSERS, Capture
from scanner.settings import get_settings


@dataclass
class SnapshotOutcome:
    snapshot_id: str
    status_code: int | None
    sha256: str | None
    changed: bool
    raw_uri: str | None
    error: str | None
    facts_written: int = 0
    notes: list[str] = field(default_factory=list)


def _fetch_http(snap: Snapshot) -> tuple[int, str, bytes]:
    assert snap.url
    s = get_settings()
    headers = {"User-Agent": s.user_agent, **snap.headers}
    with httpx.Client(timeout=s.http_timeout_s, follow_redirects=True) as c:
        if snap.powerbi_embed_page:
            page = c.get(snap.powerbi_embed_page, headers={"User-Agent": s.user_agent})
            page.raise_for_status()
            headers["X-PowerBI-ResourceKey"] = powerbi_resource_key(page.text)
        r = c.request(snap.method, snap.url, headers=headers, json=snap.body)
        return r.status_code, r.headers.get("content-type", ""), r.content


def _fetch_browser(url: str) -> tuple[int, str, bytes]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # optional dependency group "browser"
        raise RuntimeError("install the 'browser' extra and run `playwright install chromium`") from exc
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=get_settings().user_agent)
            resp = page.goto(url, wait_until="networkidle")
            html = page.content().encode()
            return (resp.status if resp else 0), "text/html", html
        finally:
            browser.close()


class _VisibleText(HTMLParser):
    """Text outside script/style/noscript/template, plus a[href] and iframe[src] without fragments."""

    SKIP = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
            return
        wanted = {"a": "href", "iframe": "src"}.get(tag)
        for name, value in attrs:
            if name == wanted and value:
                self.links.append(value.split("#", 1)[0])

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def visible_text(html: str) -> str:
    p = _VisibleText()
    p.feed(html)
    p.close()
    text = re.sub(r"\s+", " ", " ".join(p.parts)).strip()
    return text + "\n" + "\n".join(p.links)


def _drop_keys(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {k: _drop_keys(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [_drop_keys(v, keys) for v in value]
    return value


def normalised(snap: Snapshot, content: bytes) -> bytes:
    """The bytes whose hash decides whether a capture changed (see the module docstring)."""
    if snap.normalise == "visible_text":
        return visible_text(content.decode("utf-8", errors="replace")).encode()
    if snap.kind == "json" and snap.ignore_keys:
        try:
            data = json.loads(content)
        except ValueError:
            return content  # not JSON (an error page, say): hash it as received
        return json.dumps(_drop_keys(data, frozenset(snap.ignore_keys)), sort_keys=True, ensure_ascii=False).encode()
    return content


def run_snapshot(snap: Snapshot) -> SnapshotOutcome:
    assert snap.url, "only active snapshots with a URL are run"
    settings = get_settings()
    conn = db.connect(settings.database_url)
    try:
        try:
            status, ctype, content = _fetch_browser(snap.url) if snap.render == "browser" else _fetch_http(snap)
            sha = hashlib.sha256(normalised(snap, content)).hexdigest()
        except Exception as exc:
            db.record_snapshot(
                conn,
                snapshot_id=snap.id,
                url=snap.url,
                status_code=None,
                sha256=None,
                raw_uri=None,
                changed=False,
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )
            return SnapshotOutcome(snap.id, None, None, False, None, str(exc))
        now = datetime.now(UTC)
        run_id = uuid.uuid4()
        ext = {"json": "json", "pdf": "pdf", "terms": "html", "web_page": "html", "file": "bin"}[snap.kind]
        ref = RawStore(settings.raw_store_url).put(
            source="snapshots",
            dataset=snap.id,
            run_id=str(run_id),
            seq=0,
            content=content,
            ext=ext,
            fetched_at=now,
            meta={
                "url": snap.url,
                "method": snap.method,
                "status": status,
                "content_type": ctype,
                "render": snap.render,
                "normalised_sha256": sha,
            },
        )
        previous = db.last_snapshot_hash(conn, snap.id)
        changed = previous is not None and previous != sha
        db.record_snapshot(
            conn,
            snapshot_id=snap.id,
            url=snap.url,
            status_code=status,
            sha256=sha,
            raw_uri=ref.uri,
            changed=changed,
            error=None,
        )
        if changed and snap.on_change == "research":
            db.enqueue_bead(
                conn,
                kind="research-source",
                title=f"snapshot changed: {snap.id}",
                body=f"The watched source {snap.id} changed. Compare the new capture with the previous one "
                f"and decide whether adapters, codebook entries or market rules need updating.",
                raw_uri=ref.uri,
                source="snapshots",
                dataset=snap.id,
            )
        outcome = SnapshotOutcome(snap.id, status, sha, changed, ref.uri, None)
        if snap.on_change.startswith("parse:") and status == 200:
            _parse(conn, snap, Capture(snap, content, ctype, ref, now, run_id), outcome)
        return outcome
    finally:
        conn.close()


def _parse(conn: Any, snap: Snapshot, cap: Capture, outcome: SnapshotOutcome) -> None:
    """Run the snapshot's parser in its own transaction; a failure files a parser-fix bead."""
    name = snap.on_change.removeprefix("parse:")
    try:
        with conn.transaction():
            result = PARSERS[name](conn, cap)
    except Exception as exc:
        outcome.error = f"parser {name} failed: {type(exc).__name__}: {exc}"[:2000]
        db.enqueue_bead(
            conn,
            kind="parser-fix",
            title=f"snapshot parser failed: {snap.id}",
            body=f"{outcome.error}. Reproduce with the raw capture and fix scanner.runtime.snapshot_parsers.{name}.",
            raw_uri=cap.raw.uri,
            source="snapshots",
            dataset=snap.id,
        )
        return
    outcome.facts_written, outcome.notes = result.facts_written, result.notes
