# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All commands use `uv` for environment isolation.

```bash
# Setup
make dev-install       # Install all deps + pre-commit hooks

# Run
make run               # Flask dev server (debug, port 5000)

# Test
make test              # Run tests with coverage (100% required)
make test-verbose      # With detailed output
make test-quiet        # Minimal output
uv run pytest tests/test_main.py::test_name  # Run a single test

# Code quality
make format            # Auto-format with ruff check --fix + ruff format
make format-check      # Check formatting without modifying files
make lint              # flake8
make typecheck         # ty (type checker)
make security          # bandit security scan
make check-all         # format-check + lint + typecheck + security + tests

# Dependency upgrades
scripts/upgrade_affected_dep.sh <pkg> [<pkg> ...]  # Upgrade specific packages (e.g. CVE fixes) in uv.lock
scripts/upgrade_deps.sh                            # Upgrade everything, raise pyproject floors, update pre-commit hooks

# Docker
make docker-up         # Start services
make docker-rebuild    # Rebuild + restart
make docker-down       # Stop services
```

## Architecture

Flask REST API template using:
- **Flask 3.1** + **flask-cors** — web framework
- **Pydantic v2** + **pydantic-settings** — data models and config from `.env`
- **Flasgger** — Swagger UI at `/backend-template/docs/`, spec at `/backend-template/apispec.json`
- **Gunicorn** — production WSGI server (4 workers, configured in Dockerfile)

### Request lifecycle

`app/main.py` wires the app together: registers `app/blueprint.py` at `/backend-template`, calls `init_swagger` from `app/docs.py`, and installs a `before_request` hook that enforces API key auth.

### Authentication

Every request goes through a `before_request` hook in `app/main.py` that checks the `Authorization` header against `settings.secret_key`. Exempt paths bypass this check. Default key is ''; override via `.env`.

Auth-exempt paths are declared in `app/blueprint.py` as `AUTH_EXEMPT_PATHS` (relative to the blueprint, e.g. `"/health-check"`). `app/config.py` imports that set and prefixes each entry with `_URL_PREFIX` (`/backend-template`) to build `settings.auth_exempt_paths`. To exempt a new route, add it to `AUTH_EXEMPT_PATHS` in `app/blueprint.py`.

### Adding routes

Add routes to `app/blueprint.py`. Document them with a YAML docstring (flasgger reads these to generate the OpenAPI spec):

```python
@bp.route("/my-route", methods=["GET"])
def my_route():
    """
    Short description.
    ---
    tags:
        - MyTag
    responses:
        200:
            description: ...
    """
```

Shared response schemas go in `app/docs.py` under `_SWAGGER_TEMPLATE["definitions"]`.

### Configuration

`app/config.py` defines a `Settings` class (pydantic-settings `BaseSettings`) that reads from `.env`. Copy `.env.example` to `.env` and set `SECRET_KEY`.

Logging writes to `logs/backend-template.log` (rotating, 10MB, 5 backups) and stdout. Root logger is at WARNING; `app.*` loggers are at INFO.

### Testing

Tests live in `tests/`. Coverage must be 100% — `app/docs.py` is excluded from coverage. `conftest.py` and `test_main.py` are currently empty stubs ready for implementation.

### Upgrading dependencies

Use the scripts in `scripts/` rather than running the upgrade steps by hand. Both stop at the first failing step (`set -euo pipefail`) and end by running `make check-all` and `pre-commit run --all-files`.

- **`scripts/upgrade_affected_dep.sh <pkg> ...`** — for targeted upgrades, e.g. when packages have reported CVEs. Runs `uv lock --upgrade-package` for each package. Most such packages are transitive, so only `uv.lock` changes. If an affected package is a *direct* dependency, also raise its floor in `pyproject.toml` to the fixed version by hand.
- **`scripts/upgrade_deps.sh`** — for a full upgrade. Runs `uv lock --upgrade`, raises every direct dependency's `>=` floor in `pyproject.toml` to the newly locked version (via `uv add`), and runs `pre-commit autoupdate` so hook revisions in `.pre-commit-config.yaml` match the locked tool versions.

The scripts deliberately do not try to fix anything. When a step fails, investigate and fix the cause yourself, then re-run the checks:
- New lint/type/format findings from upgraded tools (ruff, ty, flake8) → fix the code, or adjust config if the finding is a tool-behaviour change rather than a real issue.
- A pre-commit hook failing after `autoupdate` → the hook's definition may have changed (e.g. ruff-format v0.17 started formatting markdown, which broke on the uppercase `LICENSE.MD`; fixed with an `exclude` in `.pre-commit-config.yaml`).
- Resolution conflicts or test failures from a major version bump → check the package's changelog before pinning anything back.

After upgrading, summarise the notable version changes (especially major bumps) for the user. Do not commit unless asked; commit messages follow `chore: ...`.

### Commit conventions

Pre-commit hooks enforce: ruff check (import sorting), ruff format, flake8 linting, bandit security scan, ty type checking, and commitlint (conventional commits). All checks must pass before a commit is accepted.
