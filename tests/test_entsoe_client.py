"""Token safety for ENTSO-E requests (src/scanner/adapters/entsoe/client.py).

Offline: every request goes to an httpx.MockTransport. FAKE_TOKEN is SYNTHETIC; the real token is never
needed. Each test asserts the token does not survive into a string the runtime stores or logs:
FetchResult (url, params, headers), raw-store sidecars, ops.ingest_run.error, log records.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from scanner.adapters.entsoe import (
    EntsoeHttpError,
    SecurityTokenAuth,
    TokenLeakError,
    assert_no_token,
    get_document,
    redact,
    redact_params,
)
from scanner.core.raw_store import RawStore

FAKE_TOKEN = "00000000-synthetic-test-token-0000"
TOKEN = SecretStr(FAKE_TOKEN)
PARAMS = {"documentType": "A44", "in_Domain": "10YDK-1--------W", "out_Domain": "10YDK-1--------W"}
XML = b"<Publication_MarketDocument/>"

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=XML, headers={"content-type": "text/xml", "set-cookie": "s=1"})


def test_token_goes_in_header_never_in_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    result = get_document(_client(handler), PARAMS, TOKEN)
    (req,) = seen
    assert req.headers["SECURITY_TOKEN"] == FAKE_TOKEN
    assert FAKE_TOKEN not in str(req.url) and "securitytoken" not in str(req.url).lower()
    assert result.status == 200 and result.content == XML
    assert result.params == PARAMS
    assert result.headers == {"content-type": "text/xml"}  # allowlisted headers only
    assert FAKE_TOKEN not in repr(result)


def test_auth_repr_hides_token() -> None:
    assert FAKE_TOKEN not in repr(SecurityTokenAuth(TOKEN))


@pytest.mark.parametrize("key", ["securityToken", "SECURITYTOKEN", "SECURITY_TOKEN"])
def test_token_as_parameter_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="header"):
        get_document(_client(_ok), {**PARAMS, key: FAKE_TOKEN}, TOKEN)


def test_transport_error_message_is_redacted_and_unchained() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}&securityToken={FAKE_TOKEN}", request=request)

    with pytest.raises(EntsoeHttpError) as exc:
        get_document(_client(handler), PARAMS, TOKEN, attempts=1)
    err = exc.value
    assert FAKE_TOKEN not in str(err) and "<redacted>" in str(err)
    assert err.__cause__ is None and err.__context__ is None  # no path back to the httpx request
    # What run_ingest would write to ops.ingest_run.error:
    assert FAKE_TOKEN not in f"error:{type(err).__name__}:{err}"


def test_final_server_error_is_returned_for_the_fingerprint() -> None:
    """A 5xx or 429 that outlives the retries is stored and fingerprinted (hard), like EDS."""
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(503, content=b"maintenance")

    result = get_document(_client(handler), PARAMS, TOKEN, attempts=3, sleep=waits.append)
    assert calls == 3 and result.status == 503 and result.content == b"maintenance"
    assert waits[0] == 8.0  # Retry-After honoured (plus one second)
    assert FAKE_TOKEN not in result.url


def test_client_errors_are_returned_for_the_caller_to_judge() -> None:
    result = get_document(_client(lambda r: httpx.Response(401, content=b"Unauthorized")), PARAMS, TOKEN)
    assert result.status == 401


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=f"<Ack><text>token {FAKE_TOKEN}</text></Ack>".encode()),
        httpx.Response(400, content=f"bad securityToken={FAKE_TOKEN}".encode()),
    ],
)
def test_echoed_token_in_body_is_refused(response: httpx.Response) -> None:
    with pytest.raises(TokenLeakError):
        get_document(_client(lambda r: response), PARAMS, TOKEN)


def test_echoed_token_in_stored_header_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=XML, headers={"content-disposition": f"attachment; filename={FAKE_TOKEN}.xml"}
        )

    result = get_document(_client(handler), PARAMS, TOKEN)
    assert result.headers["content-disposition"] == "attachment; filename=<redacted>.xml"


def test_raw_store_sidecar_has_no_token(tmp_path: Path) -> None:
    result = get_document(_client(_ok), PARAMS, TOKEN)
    store = RawStore(f"file://{tmp_path.as_posix()}")
    store.put(
        source="entsoe",
        dataset="DayAheadPrices",
        run_id="test",
        seq=0,
        content=result.content,
        ext=result.ext,
        fetched_at=datetime(2026, 10, 18, tzinfo=UTC),
        meta={"url": result.url, "params": result.params, "status": result.status, "headers": result.headers},
    )
    written = [p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file()]
    assert written and all(FAKE_TOKEN not in w for w in written)
    sidecar = json.loads(next(w for w in written if '"sha256"' in w))
    assert sidecar["params"] == PARAMS


def test_logs_have_no_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        get_document(_client(_ok), PARAMS, TOKEN)
    assert any("HTTP Request" in r.getMessage() for r in caplog.records)  # httpx logs the URL
    assert all(FAKE_TOKEN not in r.getMessage() for r in caplog.records)


def test_redact_forms() -> None:
    encoded = FAKE_TOKEN.replace("-", "%2D")
    assert redact(f"x {FAKE_TOKEN} y", TOKEN) == "x <redacted> y"
    assert redact(f"?a=1&securityToken={encoded}&b=2", TOKEN) == "?a=1&securityToken=<redacted>&b=2"
    # Without knowing the token: parameter and header forms are still removed.
    assert redact("https://h/api?securityToken=abc&b=2") == "https://h/api?securityToken=<redacted>&b=2"
    assert redact("{'SECURITY_TOKEN': 'abc'}") == "{'SECURITY_TOKEN': '<redacted>'}"
    assert redact("SECURITY_TOKEN: abc") == "SECURITY_TOKEN: <redacted>"
    once = redact(f"securityToken={FAKE_TOKEN}", TOKEN)
    assert redact(once, TOKEN) == once


def test_redact_params_and_assert_no_token() -> None:
    assert redact_params({"securityToken": FAKE_TOKEN, "SECURITY_TOKEN": "x", "a": f"v{FAKE_TOKEN}"}, TOKEN) == {
        "a": "v<redacted>"
    }
    assert_no_token(TOKEN, "clean", b"clean")
    assert_no_token(None, FAKE_TOKEN)  # no token configured: nothing to find
    with pytest.raises(TokenLeakError):
        assert_no_token(TOKEN, "clean", f"x{FAKE_TOKEN}".encode())
