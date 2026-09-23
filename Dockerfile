FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Dependencies first, so a source change does not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY README.md ./
RUN uv sync --frozen --no-dev

# Bake in DuckDB's excel extension so XLSX works without runtime network access.
RUN uv run python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL excel')"

ENV AU_OPENDATA_CACHE_DIR=/var/cache/au-opendata-mcp
RUN mkdir -p /var/cache/au-opendata-mcp

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uv", "run", "au-opendata-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
