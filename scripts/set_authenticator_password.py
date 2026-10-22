"""Set the local PostgREST login role's password (local-only step; never commit the value).

Usage: uv run python scripts/set_authenticator_password.py <admin_database_url> [password]
The password defaults to AUTHENTICATOR_PASSWORD from the environment or .env (the same value docker compose
gives PostgREST), then to the local-only default 'authenticator'. Roles are cluster-wide, so running this once
per Postgres server is enough.
"""

from __future__ import annotations

import sys

import psycopg
from psycopg import sql

from scanner.settings import get_settings


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    configured = get_settings().authenticator_password
    password = sys.argv[2] if len(sys.argv) > 2 else (configured.get_secret_value() if configured else "authenticator")
    with psycopg.connect(sys.argv[1], autocommit=True) as conn:
        conn.execute(sql.SQL("alter role authenticator login password {}").format(sql.Literal(password)))
    print("authenticator can log in" + ("" if configured or len(sys.argv) > 2 else " (local default password)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
