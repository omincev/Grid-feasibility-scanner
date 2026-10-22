# One image for the orchestrator (Dagster webserver and daemon) and the one-off CLI containers, locally
# (../docker-compose.yml) and on the VPS (docker-compose.prod.yml, image ghcr.io/omincev/grid-feasibility-scanner).
# Pinned to the versions the orchestrator image was built with on 2026-10-18 (Python 3.12.15, Debian trixie).
FROM python:3.12.15-slim-trixie
RUN pip install --no-cache-dir uv==0.13.1
# Docker CLI only: `scanner.cli backup` / `restore` run pg_dump and pg_restore in the db container through
# `docker exec`. Only the VPS `backup` one-off service mounts the Docker socket; no long-running service does.
COPY --from=docker:28.5.1-cli@sha256:9190b0613792e658a7783cf14b2d5ace5941bb68ede7276922ea36ee457d76ad \
     /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY pyproject.toml uv.lock* README.md* workspace.yaml ./
# Chromium is only needed by Playwright adapters (no active snapshot uses `render: browser`); the VPS image is
# built with the default, false. The local compose file passes INSTALL_BROWSER explicitly.
ARG INSTALL_BROWSER=false
# Dependencies first, so a code-only change reuses this layer.
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
      uv sync --frozen --no-dev --no-install-project --extra browser --extra pdf \
      && /app/.venv/bin/playwright install --with-deps chromium; \
    else \
      uv sync --frozen --no-dev --no-install-project --extra pdf; \
    fi
# SQLAlchemy 2.1 picks psycopg 3 for plain postgresql:// URLs and dagster-postgres then fails on `NOTIFY ... $1`.
# Use DAGSTER_PG_URL=postgresql+psycopg2://... with psycopg2; only the orchestrator image needs it, so not in pyproject.
RUN uv pip install --python /app/.venv/bin/python psycopg2-binary==2.9.13
COPY src ./src
# Method cards and source dossiers: read by the curated loader, the contract checks and the asset metadata.
COPY docs ./docs
COPY registry ./registry
COPY codebook ./codebook
COPY market_rules ./market_rules
COPY assumptions ./assumptions
COPY benchmarks ./benchmarks
COPY claims ./claims
# Migrations (`scanner.cli migrate` in a one-off container), deploy and backup helpers, and the VPS files that
# infra/deploy.sh extracts from the image it deploys, so compose files always match the code.
COPY db ./db
COPY scripts ./scripts
COPY infra ./infra
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
      uv sync --frozen --no-dev --extra browser --extra pdf; \
    else \
      uv sync --frozen --no-dev --extra pdf; \
    fi
ENV PATH="/app/.venv/bin:$PATH"
# Instance config (run storage in Postgres, run queue); the local compose file bind-mounts the same file.
ENV DAGSTER_HOME=/opt/dagster
COPY infra/dagster.yaml /opt/dagster/dagster.yaml
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
LABEL org.opencontainers.image.source="https://github.com/omincev/grid-feasibility-scanner" \
      org.opencontainers.image.title="grid-scanner" \
      org.opencontainers.image.revision=$GIT_SHA
