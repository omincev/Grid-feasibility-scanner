"""HTTP access to the ENTSO-E Transparency Platform that keeps the security token out of stored strings.

The token travels only in the `SECURITY_TOKEN` request header, which the API accepts for GET requests
(docs/sources/eu/entsoe.md, "Access"). URLs therefore never contain it, so neither httpx's request log
lines nor its exception messages (which quote the URL) can leak it. Everything `get_document` returns
goes through the redaction helpers anyway, and a final check refuses to hand back a FetchResult in which
the token still appears: that object feeds raw-store sidecars, `ops.ingest_run.error` and fixtures.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Generator, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
from pydantic import SecretStr
from tenacity import RetryCallState, Retrying, retry_if_exception_type, retry_if_result, stop_after_attempt
from tenacity import wait_exponential as _wait_exponential

from scanner.adapters.base import FetchResult

BASE_URL = "https://web-api.tp.entsoe.eu/api"
TOKEN_HEADER = "SECURITY_TOKEN"
TOKEN_PARAM = "securityToken"
REDACTED = "<redacted>"
MAX_RETRY_AFTER_S = 120.0
_backoff = _wait_exponential(multiplier=2, max=60)

_TOKEN_KEYS = frozenset({TOKEN_PARAM.lower(), TOKEN_HEADER.lower()})
_STORED_HEADERS = ("content-type", "content-disposition", "date", "etag", "last-modified")
_PARAM_RE = re.compile(r"(?i)(securityToken=)[^&\s\"'#]*")
_HEADER_RE = re.compile(r"(?i)(security_token['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+")


class EntsoeHttpError(RuntimeError):
    """A request that failed after retries. The message is redacted.

    Raised without exception chaining: httpx exceptions hold the request, headers included.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TokenLeakError(RuntimeError):
    """The token was found in something about to be returned or stored. Nothing was written."""


class SecurityTokenAuth(httpx.Auth):
    """Adds the token header when a request is sent; the token never sits in client config or reprs."""

    def __init__(self, token: SecretStr):
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers[TOKEN_HEADER] = self._token.get_secret_value()
        yield request

    def __repr__(self) -> str:
        return f"{type(self).__name__}({TOKEN_HEADER}={REDACTED})"


def _forms(token: SecretStr | str | None) -> list[str]:
    """The token as it could appear in text: raw and URL-encoded."""
    value = token.get_secret_value() if isinstance(token, SecretStr) else token
    return sorted({value, quote(value, safe=""), quote_plus(value)}, key=len, reverse=True) if value else []


def redact(text: str, token: SecretStr | str | None = None) -> str:
    """Remove the token (raw or URL-encoded) and any securityToken= or SECURITY_TOKEN value from text."""
    for form in _forms(token):
        text = text.replace(form, REDACTED)
    text = _PARAM_RE.sub(rf"\g<1>{REDACTED}", text)
    return _HEADER_RE.sub(rf"\g<1>{REDACTED}", text)


def redact_params(params: Mapping[str, Any], token: SecretStr | str | None = None) -> dict[str, Any]:
    """Request parameters safe to store: token keys dropped, string values redacted."""
    return {k: redact(v, token) if isinstance(v, str) else v for k, v in params.items() if k.lower() not in _TOKEN_KEYS}


def stored_headers(headers: Mapping[str, str], token: SecretStr | str | None = None) -> dict[str, str]:
    """The response headers worth keeping in a sidecar, redacted."""
    return {k.lower(): redact(v, token) for k, v in headers.items() if k.lower() in _STORED_HEADERS}


def assert_no_token(token: SecretStr | str | None, *parts: str | bytes) -> None:
    """Raise TokenLeakError if the token, raw or URL-encoded, appears in any part."""
    for form in _forms(token):
        needle = form.encode()
        for part in parts:
            if (needle in part) if isinstance(part, bytes) else (form in part):
                raise TokenLeakError("the security token appears in output that would be stored; nothing written")


def get_document(
    client: httpx.Client,
    params: Mapping[str, str],
    token: SecretStr,
    *,
    url: str = BASE_URL,
    attempts: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """GET one API response with the token in the header. Retries transport errors, 429 and 5xx,
    honouring Retry-After (capped).

    Every other status (400 with an Acknowledgement, 401), and a 5xx or 429 that outlives the
    retries, comes back as a FetchResult for the runtime to store and fingerprint, as in the Energi
    Data Service adapter. Only a transport failure with no response raises (EntsoeHttpError). The
    returned FetchResult is safe to store.
    """
    if _TOKEN_KEYS & {k.lower() for k in params}:
        raise ValueError(f"send the token in the {TOKEN_HEADER} header, never as a parameter")
    outcome = _send(client, url, dict(params), token, attempts, sleep)
    if isinstance(outcome, EntsoeHttpError):
        raise outcome  # raised outside the except block, so it carries no reference to the httpx error
    result = FetchResult(
        url=redact(str(outcome.request.url), token),
        params=redact_params(params, token),
        status=outcome.status_code,
        content_type=outcome.headers.get("content-type", ""),
        content=outcome.content,
        fetched_at=datetime.now(UTC),
        headers=stored_headers(outcome.headers, token),
    )
    assert_no_token(
        token, result.url, json.dumps(result.params, default=str), json.dumps(result.headers), result.content
    )
    return result


def _retryable(resp: httpx.Response) -> bool:
    return resp.status_code == 429 or resp.status_code >= 500


def _wait(state: RetryCallState) -> float:
    """Retry-After on a 429 (the API bans a token for about 10 minutes), else exponential backoff."""
    outcome = state.outcome
    if outcome is not None and not outcome.failed:
        retry_after = outcome.result().headers.get("retry-after", "")
        if retry_after.isdigit():
            return min(float(retry_after) + 1.0, MAX_RETRY_AFTER_S)
    return float(_backoff(state))


def _send(
    client: httpx.Client,
    url: str,
    params: dict[str, str],
    token: SecretStr,
    attempts: int,
    sleep: Callable[[float], None],
) -> httpx.Response | EntsoeHttpError:
    # Redirects are not followed (httpx default): a custom header would be re-sent to the new host.
    auth = SecurityTokenAuth(token)
    retrying = Retrying(
        retry=retry_if_exception_type(httpx.TransportError) | retry_if_result(_retryable),
        wait=_wait,
        stop=stop_after_attempt(attempts),
        sleep=sleep,
        retry_error_callback=lambda state: state.outcome.result() if state.outcome else None,
    )
    try:
        resp: httpx.Response = retrying(client.get, url, params=params, auth=auth)
        return resp
    except httpx.HTTPError as exc:
        return EntsoeHttpError(redact(f"{type(exc).__name__}: {exc}", token))
