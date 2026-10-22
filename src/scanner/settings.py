"""Runtime settings from environment variables (see .env.example). Secrets never have defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from scanner import REPO_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str = "postgresql://scanner:scanner@localhost:5432/scanner"
    # The analytics code location's connection for features, scores, valuation and the export: a login role in
    # scanner_analytics (migration 20261018130000, infra/README.md). Unset or empty: DATABASE_URL.
    analytics_database_url: str | None = None
    raw_store_url: str = "file://./.data/raw"
    repo_root: Path = REPO_ROOT
    # Restricted repository with curated material above public sensitivity (ADR 0006). Optional.
    restricted_curated_path: Path | None = None

    entsoe_api_token: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ENTSOE_API_TOKEN", "ENTSOE_API_KEY")
    )
    anthropic_api_key: SecretStr | None = None
    # Datafordeler (DAGI boundaries and other Danish registers). Runtime-only; send it in a header or
    # redact it from any URL that is logged, stored in a raw-capture sidecar or written to a meta file.
    datafordeler_api_key: SecretStr | None = None
    # Local PostgREST login role password; read by scripts/set_authenticator_password.py (compose reads .env too).
    authenticator_password: SecretStr | None = None
    # HS256 secret PostgREST verifies API tokens with (PGRST_JWT_SECRET); `scanner.cli token` signs with it.
    api_jwt_secret: SecretStr | None = None

    # Lets code fall back to an assumption's `proposed` value when `value` is null; outputs must say so.
    allow_proposed_assumptions: bool = Field(default=False, validation_alias="SCANNER_ALLOW_PROPOSED_ASSUMPTIONS")
    # Dagster schedules start as running only in a deployment (docker-compose.yml sets it for the orchestrator
    # containers), so `dagster dev` on a laptop never starts live ingests. Not meant for .env.
    schedules_default_running: bool = Field(default=False, validation_alias="SCANNER_SCHEDULES_DEFAULT_RUNNING")

    # `scanner.cli backup` / `restore` (scanner.runtime.backup): the docker compose `db` container they run
    # pg_dump and pg_restore in, where dumps go, and how many dumps of a database are kept.
    db_container: str = "grid-scanner-db-1"
    backup_dir: Path = Path(".data/backups")
    backup_keep: int = 7

    http_timeout_s: float = 60.0
    user_agent: str = "grid-scanner/0.1 (+contact: set in .env)"

    def analytics_url(self) -> str:
        """ANALYTICS_DATABASE_URL, falling back to DATABASE_URL."""
        return self.analytics_database_url or self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
