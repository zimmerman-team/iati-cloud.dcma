# iati-cloud.dcma

The **Delivery Chain Mapping API**. Given one IATI activity identifier, it returns the activities that fund it and the
activities it funds, following the published links recursively, in a single call.

```bash
curl -H "Authorization: $SECRET_KEY" \
  "http://localhost:5000/iati-cloud-dcma/chain?activity=GB-CHC-1047501-23SOELNF&mode=constrained"
```

## The API

`GET /iati-cloud-dcma/chain`

| Parameter | Values | Default | Meaning |
|---|---|---|---|
| `activity` | an IATI activity identifier | required | The seed. Any letter case. Pass it URL-encoded: identifiers can contain `/`. |
| `mode` | `full`, `constrained` | `full` | `constrained`: the seed, everything that funds it, everything it funds. `full`: also everything else its funders fund, and the direct co-funders of those. |
| `format` | `graph`, `tree` | `graph` | `graph`: `nodes`, `edges`, `associations`. `tree`: the same chain as a tree rooted at the seed, with `upstream` and `downstream` children and the unused edges in `extra_edges`. |
| `orgs` | `identified`, `refs`, `all` | `identified` | Which organisations (named where no activity is named) to include: those with an identifier; also those with free text typed into the ref; also those with only a name. |
| `max_depth`, `max_nodes` | integer ≥ 1 | `MAX_DEPTH`, `MAX_NODES` | Lower the limits for one request. They cannot be raised above the configured values. |

What to know when reading a response:

- **Links are directed funder → funded** (`from` → `to`). `kinds` says what the link is based on (`hierarchy`,
  `transaction`, `participating-org`) and `declared_by` says which activities published it.
- **Gaps in the data are shown, not hidden.** An activity that is referenced but not published is a node with
  `published: false`. Where a publisher names only an organisation, the chain ends in an organisation node.
- **A cut-off result says so.** `truncated: true`, with the cause in `truncation_reasons` (`max_depth`, `max_nodes`,
  `mention_limit`, `time_budget`). Nodes that could not be fully explored have `expanded: false`.
- **Call it from a server.** Every route except the health check and the Swagger pages needs the API key in the
  `Authorization` header. Do not put that key in a browser.

`GET /iati-cloud-dcma/health-check` needs no key. It answers `healthy`, `degraded` (the cache is down; requests still
work) or `unhealthy` (the data source does not answer; HTTP 503).

The full request and response schema is in the Swagger UI at `/iati-cloud-dcma/docs/`.

To make an activity appear in delivery chains, publish its links as IATI's guidance on linking an activity to its
funder describes: the funder's activity identifier as `provider-activity-id` on incoming transactions, and the funder
as a `participating-org` with role 1.

## Features

- **Flask REST API** with CORS, Swagger UI, and API key authentication
- **Live traversal of an IATI.cloud Solr activity core**, with a Redis response cache that fails open
- **Flasgger** provides a Swagger UI at `/iati-cloud-dcma/docs/`, spec at `/iati-cloud-dcma/apispec.json`
- **Pydantic settings** loaded from `.env` for typed configuration
- **Pre-commit hooks** enforcing formatting, linting, type checks, security scanning, and conventional commits
- **100% test coverage** enforced via pytest-cov
- **Docker-ready** with Gunicorn and docker-compose
- **Makefile** support for easy project usage

## Architecture

See [DIAGRAMS.md](./docs/DIAGRAMS.md) for system architecture.

- [Application Architecture](./docs/DIAGRAMS.md#application-architecture)
- [Request Flow](./docs/DIAGRAMS.md#request-flow)
- [Link Model](./docs/DIAGRAMS.md#link-model)
- [Traversal](./docs/DIAGRAMS.md#traversal)
- [Deployment](./docs/DIAGRAMS.md#deployment)

## Requirements

- Python 3.11+
- UV (package manager)
- Access to an IATI.cloud Solr activity core (`SOLR_URL`)
- (optional) Redis, for the response cache. Docker Compose starts one.
- (optional) Docker and Docker Compose
- (optional) Make

## Quick Start

### 1. Clone and Setup

```bash
git clone git@github.com:zimmerman-team/iati-cloud.dcma.git
cd iati-cloud.dcma
```

The repo includes a committed `uv.lock` - all dependency versions are pinned for reproducible installs.

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env: SECRET_KEY is required, the app does not start without it
```

### 3. Run with Docker Compose

```bash
make docker-up
```

The API will be available at `http://localhost:5000`

### 4. Local Development

```bash
# Install all dependencies from uv.lock and install pre-commit hooks
make dev-install

# Run app
make run
```

## Testing

```bash
make test                               # unit tests, 100 % coverage required, no network
uv run pytest -m live --no-cov          # smoke tests against the live data source (SOLR_URL)
uv run python scripts/measure_chains.py # time a set of live chains
```

## Upgrading Dependencies

Two scripts help keep dependencies up to date. Both try the upgrade and then run all checks (`make check-all` and the pre-commit hooks). They stop at the first failure, leaving any remaining fixes to you.

```bash
# Upgrade specific packages, e.g. ones with reported CVEs (only uv.lock changes)
scripts/upgrade_affected_dep.sh werkzeug mistune

# Upgrade everything: uv.lock, minimum versions in pyproject.toml, and pre-commit hook versions
scripts/upgrade_deps.sh
```

If an affected package is a direct dependency in `pyproject.toml`, also raise its minimum version.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | API authentication key (`Authorization` header). Required: the app does not start without it | — |
| `PORT` | Port the server listens on | `5000` |
| `FLASK_DEBUG` | Enable Flask debug mode for local development | `false` |
| `CORS_ALLOWED_ORIGINS` | Allowed origins for CORS | `["http://localhost:3000"]` |
| `SOLR_URL` | The IATI.cloud endpoint that answers Solr select queries for the activity core. For a plain Solr core, include the handler: `…/solr/<core>/select` | `https://fcdo.iati.cloud/search/activity/` |
| `SOLR_TIMEOUT` | Seconds allowed for one Solr query | `10` |
| `REQUEST_TIMEOUT` | Seconds allowed for one `/chain` request. When it runs out, the result is returned truncated | `30` |
| `MAX_DEPTH` | Maximum hops per walk | `20` |
| `MAX_NODES` | Maximum nodes per response | `1000` |
| `ID_BATCH_SIZE` | Identifiers per Solr query (at most 250) | `200` |
| `REDIS_URL` | Redis for the response cache. Docker Compose overrides it | `redis://localhost:6379/0` |
| `CACHE_TTL` | Seconds a cached response is kept | `86400` |

The gunicorn worker timeout (60 s in `Dockerfile` and `docker-compose.yaml`) must stay above
`REQUEST_TIMEOUT + SOLR_TIMEOUT`.

## Project Structure

See [DEVELOPMENT.md](./docs/DEVELOPMENT.md#project-structure) for the full annotated project structure.

## Development

_This project was co-developed with AI to accelerate feature delivery. All code has been manually reviewed and tested for quality._

See [DEVELOPMENT.md](./docs/DEVELOPMENT.md) for setup, formatting, linting, adding new validations, debugging, and more. For agentic development, [CLAUDE.md](./CLAUDE.md) and [AGENTS.md](./AGENTS.md) are included. The former with extensive context, the latter with a quick overview and a reference back to _CLAUDE.md_ to reuse that extensive context. Both to be used by OpenCode, Claude Code, Codex, Abacus or other tools as per the developers' preferences.

## License

See [LICENSE.MD](./LICENSE.MD).

## Contributing

When contributing, make sure to follow the [Code of Conduct](./docs/CODE_OF_CONDUCT.md). For git-related tasks such as creating a commit message or a branch name, refer to [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## Support

For issues and questions:
- [GitHub Issues](https://github.com/zimmerman-team/iati-cloud.dcma/issues)
