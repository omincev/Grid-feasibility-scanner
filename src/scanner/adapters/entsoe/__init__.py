"""ENTSO-E Transparency Platform, the country-agnostic base tier (ADR 0011, WP3.1).

`decode` turns API XML into flat records; `client` fetches with the token kept out of stored strings;
`EntsoeAdapter` implements the adapter protocol on top of both. See README.md.
"""

from scanner.adapters.entsoe.adapter import EntsoeAdapter
from scanner.adapters.entsoe.client import (
    BASE_URL,
    EntsoeHttpError,
    SecurityTokenAuth,
    TokenLeakError,
    assert_no_token,
    get_document,
    redact,
    redact_params,
)
from scanner.adapters.entsoe.decode import AcknowledgementError, Reason, Record, decode

__all__ = [
    "BASE_URL",
    "AcknowledgementError",
    "EntsoeAdapter",
    "EntsoeHttpError",
    "Reason",
    "Record",
    "SecurityTokenAuth",
    "TokenLeakError",
    "assert_no_token",
    "decode",
    "get_document",
    "redact",
    "redact_params",
]
