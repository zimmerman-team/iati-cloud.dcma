# iati-cloud.dcma

## Features

- **Flask REST API** with CORS, Swagger UI, and API key authentication
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
- [Deployment](./docs/DIAGRAMS.md#deployment)

## Requirements

- Python 3.11+
- UV (package manager)
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
# Edit .env with your values
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
make test
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
| `SECRET_KEY` | API authentication key (`Authorization` header) | `` |
| `PORT` | Port the server listens on | `5000` |
| `FLASK_DEBUG` | Enable Flask debug mode for local development | `false` |
| `CORS_ALLOWED_ORIGINS` | Allowed origins for CORS | `["http://localhost:3000"]` |

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
