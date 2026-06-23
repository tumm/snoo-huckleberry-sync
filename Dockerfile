FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (cached layer when code changes but deps don't)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache

# Copy application code
COPY sync/ sync/

# Create data directory and non-root user
RUN useradd -r -u 1001 appuser \
    && mkdir -p /data \
    && chown appuser /data /app
USER appuser

VOLUME /data

CMD [".venv/bin/python", "-m", "sync.runner", "--loop"]
