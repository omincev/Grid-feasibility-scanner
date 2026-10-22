"""`scanner.cli token`: an HS256 JWT PostgREST accepts for role authenticated (offline)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from scanner.cli import main, mint_token
from scanner.settings import get_settings

SECRET = "x" * 40


def _decode(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, str]:
    head, payload, sig = token.split(".")

    def part(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    return json.loads(part(head)), json.loads(part(payload)), part(sig), f"{head}.{payload}"


def test_token_is_a_signed_hs256_jwt_with_role_and_expiry() -> None:
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    header, payload, sig, signing_input = _decode(mint_token(SECRET, 14, now))
    assert header == {"alg": "HS256", "typ": "JWT"}
    assert payload["role"] == "authenticated"
    assert payload["exp"] == int(now.timestamp()) + 14 * 86400
    assert sig == hmac.new(SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    assert sig != hmac.new(b"y" * 40, signing_input.encode(), hashlib.sha256).digest()


def test_token_command_prints_a_token_and_needs_the_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("API_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    assert main(["token", "--days", "3"]) == 0
    _, payload, sig, signing_input = _decode(capsys.readouterr().out.strip())
    assert payload["exp"] - payload["iat"] == 3 * 86400
    assert sig == hmac.new(SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()

    monkeypatch.setenv("API_JWT_SECRET", "")
    get_settings.cache_clear()
    assert main(["token"]) == 1
    assert "API_JWT_SECRET" in capsys.readouterr().err
    get_settings.cache_clear()
