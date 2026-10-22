"""Offline check of scripts/record_entsoe_fixtures.py with a mocked transport.

FAKE_TOKEN is SYNTHETIC and reaches the script only through scanner.settings, as the real one will.
No network: every request goes to an httpx.MockTransport that answers with the official A44 example.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from conftest import FIXTURES

from scanner.settings import get_settings

FAKE_TOKEN = "11111111-synthetic-recorder-token-1111"
EXAMPLE = (FIXTURES / "entsoe" / "postman__A44_AT_2024-07-28.xml").read_bytes()


@pytest.fixture(scope="module")
def rec(root: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "record_entsoe_fixtures", root / "scripts" / "record_entsoe_fixtures.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["record_entsoe_fixtures"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Token only via the environment; cwd moved so no real .env file can be picked up."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENTSOE_API_KEY", raising=False)
    monkeypatch.setenv("ENTSOE_API_TOKEN", FAKE_TOKEN)
    get_settings.cache_clear()
    yield tmp_path / "out"
    get_settings.cache_clear()


def _run(
    rec: ModuleType, out: Path, handler: Callable[[httpx.Request], httpx.Response], *extra: str
) -> tuple[int, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    code = rec.main(["--out", str(out), "--pause", "0", *extra], transport=httpx.MockTransport(wrapped))
    return code, seen


def _xml(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=EXAMPLE, headers={"content-type": "text/xml"})


def _all_text(out: Path) -> str:
    return "".join(p.read_bytes().decode("utf-8", "replace") for p in out.iterdir())


def test_records_every_case_without_the_token(rec: ModuleType, env: Path) -> None:
    code, seen = _run(rec, env, _xml)
    assert code == 0
    assert len(seen) == len(rec.CASES)
    for req in seen:
        assert req.headers["SECURITY_TOKEN"] == FAKE_TOKEN
        assert FAKE_TOKEN not in str(req.url)
    for case in rec.CASES:
        xml = env / f"{case.name}.xml"
        meta = json.loads((env / f"{case.name}.xml.meta.json").read_text(encoding="utf-8"))
        assert xml.read_bytes() == EXAMPLE
        assert meta["sha256"] == hashlib.sha256(EXAMPLE).hexdigest()
        assert meta["params"] == case.params and meta["status"] == 200
        assert "securityToken" not in meta["url"] and meta["fetched_at"]
    assert FAKE_TOKEN not in _all_text(env)


def test_existing_cases_are_skipped(rec: ModuleType, env: Path) -> None:
    first = rec.CASES[0].name
    _run(rec, env, _xml, "--case", first)
    code, seen = _run(rec, env, _xml, "--case", first)
    assert code == 0 and seen == []


def test_echoed_token_writes_nothing(rec: ModuleType, env: Path) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=EXAMPLE.replace(b"A44", FAKE_TOKEN.encode(), 1))

    code, _ = _run(rec, env, echo, "--case", rec.CASES[0].name)
    assert code == 1
    assert not env.exists() or not any(env.iterdir())


def test_unauthorized_stops_at_first_request(rec: ModuleType, env: Path) -> None:
    code, seen = _run(rec, env, lambda r: httpx.Response(401, content=b"Unauthorized"))
    assert code == 1 and len(seen) == 1
    assert not any(env.iterdir())


def test_missing_token(rec: ModuleType, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENTSOE_API_TOKEN")
    get_settings.cache_clear()
    code, seen = _run(rec, env, _xml)
    assert code == 2 and seen == []


def test_case_names_are_unique_and_tokenless(rec: ModuleType) -> None:
    names = [c.name for c in rec.CASES]
    assert len(names) == len(set(names))
    for c in rec.CASES:
        assert not {k.lower() for k in c.params} & {"securitytoken", "security_token"}
