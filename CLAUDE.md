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

# Docs
make pdf docs/FILE.md                              # Markdown → PDF next to it (pandoc + headless Chrome; renders mermaid)

# Docker
make docker-up         # Start services
make docker-rebuild    # Rebuild + restart
make docker-down       # Stop services
```

## Architecture

Flask REST API using:
- **Flask 3.1** + **flask-cors** — web framework
- **Pydantic v2** + **pydantic-settings** — data models and config from `.env`
- **Flasgger** — Swagger UI at `/iati-cloud-dcma/docs/`, spec at `/iati-cloud-dcma/apispec.json`
- **pysolr** — reads the IATI.cloud Solr activity core (`SOLR_URL`); **redis** — response cache, fails open
- **Gunicorn** — production WSGI server (4 workers, 60 s timeout, configured in Dockerfile and docker-compose.yaml)

### Request lifecycle

`app/main.py` wires the app together: registers `app/blueprint.py` at `/iati-cloud-dcma`, calls `init_swagger` from `app/docs.py`, installs a `before_request` hook that enforces API key auth, and stores a `Services` object (Solr source factory, cache, limits) in `app.extensions`. `create_app(services)` lets tests inject fakes.

### The delivery chain (`app/iati_cloud_dcma/`)

The behaviour is specified in `docs/SPEC.md`. One request flows through these modules:

| Module | Role |
|---|---|
| `nodes.py` | Pure. The **node keys** every other module works in: `act:<case-folded id>`, `org:<case-folded ref>`, `org-name:<normalised name>`, plus `normalise_id` (trim, 255-character limit) and the organisation `identification_of` levels. |
| `links.py` | Pure. One Solr document → edges (L1–L6), organisation links (O1–O3), associations (L7), in node keys; original spellings are kept in `labels`. `ActivityDoc.merge` unions the copies of a duplicated identifier. |
| `source.py` | `LinkSource` protocol and `SolrLinkSource`: `fetch` by identifier, `mentioning` (reverse lookup). One source per request. The shared pysolr client retries failures before any response (`ConnectionRetry`, spec §7 Resilience), never read timeouts or HTTP errors. |
| `traversal.py` | `build_chain`: up- and down-walks, the two modes, limits, truncation reasons. Request-scoped cache in `_Knowledge`. Translates keys to shown spellings only in `_assemble`. |
| `chain.py` | Immutable result types (`Chain`, nodes, edges) in shown spellings. |
| `tree.py` | Pure. `Chain` → spanning tree plus `extra_edges`. |
| `service.py` | `chain_payload`: cache lookup, traversal, response, request log line. `health`. |
| `app/models.py` | Pydantic `ChainQuery` and the response models; `app/docs.py` generates the Swagger definitions from them. |
| `app/cache.py` | Redis response cache. |

`app/blueprint.py` must not import `app.config` (circular: `config.py` imports `AUTH_EXEMPT_PATHS` from it). Routes read what they need from `current_app.extensions`.

### Authentication

Every request goes through a `before_request` hook in `app/main.py` that checks the `Authorization` header against `settings.secret_key`. Exempt paths and `OPTIONS` requests (CORS preflights) bypass this check. `SECRET_KEY` has no default: the app refuses to start without it. Tests set one in `tests/__init__.py`.

Auth-exempt paths are declared in `app/blueprint.py` as `AUTH_EXEMPT_PATHS` (relative to the blueprint, e.g. `"/health-check"`). `app/config.py` imports that set and prefixes each entry with `_URL_PREFIX` (`/iati-cloud-dcma`) to build `settings.auth_exempt_paths`. To exempt a new route, add it to `AUTH_EXEMPT_PATHS` in `app/blueprint.py`.

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

Logging writes to `logs/iati-cloud-dcma.log` (rotating, 10MB, 5 backups) and stdout. Root logger is at WARNING; `app.*` loggers are at INFO.

### Testing

Tests live in `tests/`. Coverage must be 100% — `app/docs.py` is excluded from coverage. No test touches the network: traversal and API tests use `FakeLinkSource` and `make_services` from `tests/conftest.py` (in-memory source, fakeredis), with real Solr documents in `tests/fixtures/`. Tests of the real Solr client stub only urllib3's socket exchange (fixture `solr_wire`). `tests/live/` holds smoke tests against the live source, deselected by default: `uv run pytest -m live --no-cov`. Refresh fixtures with `scripts/fetch_fixture.py` and `scripts/fetch_chain_fixture.py`; time live chains with `scripts/measure_chains.py`.

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
