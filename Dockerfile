FROM python:3.11-slim

WORKDIR /app

# Install UV
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml .
COPY uv.lock .
COPY app ./app
COPY data ./data

# Install dependencies using UV (frozen = reproduce lockfile exactly). The project itself is not installed:
# gunicorn imports app.main from WORKDIR, and building it would need README.md, which .dockerignore excludes.
RUN uv sync --frozen --no-dev --no-install-project

# Add venv binaries to PATH so gunicorn and other entrypoints are found
ENV PATH="/app/.venv/bin:$PATH"

# Expose port (default 5000, override with PORT env var)
EXPOSE 5000

# The worker timeout must exceed REQUEST_TIMEOUT + SOLR_TIMEOUT (30 s + 10 s by default): a Solr query that starts
# just before the request budget runs out may still take its full timeout. docker-compose.yaml overrides this command.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 4 --timeout 60 app.main:app"]
