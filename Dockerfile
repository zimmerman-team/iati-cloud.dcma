FROM python:3.11-slim

WORKDIR /app

# Install UV
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml .
COPY uv.lock .
COPY app ./app
COPY data ./data

# Install dependencies using UV (frozen = reproduce lockfile exactly)
RUN uv sync --frozen --no-dev

# Add venv binaries to PATH so gunicorn and other entrypoints are found
ENV PATH="/app/.venv/bin:$PATH"

# Expose port (default 5000, override with PORT env var)
EXPOSE 5000
