# AGENTS.md

## Critical Commands

```bash
make dev-install    # Install deps + pre-commit hooks (order matters)
make run            # Flask dev server: uv run flask --app app.main:app run
make test           # Requires 100% coverage (fails below)
uv run pytest tests/test_main.py::test_name  # Single test
```

## Common Pitfalls

- **Always use `uv`**: All commands require `uv run` or the Makefile (e.g., `make test`, not `pytest`)
- **Flask app path**: Use `--app app.main:app`, not just `flask run`
- **Type checker is `ty`**: Not mypy. Run via `make typecheck` or `uv run ty check app`
- **Coverage 100%**: Tests fail if coverage drops below 100% (`app/docs.py` is excluded)

## Auth Exempt Paths

To exempt a route from API key auth, add it to `AUTH_EXEMPT_PATHS` in `app/blueprint.py` (relative to blueprint, e.g., `"/health-check"`). Do not modify `app/main.py`.

## Testing

- Tests in `tests/`, follow `test_*.py` pattern
- `app/docs.py` is excluded from coverage automatically

## Swagger

- UI: `/iati-cloud-dcma/docs/`
- Spec: `/iati-cloud-dcma/apispec.json`

## Reference

See `CLAUDE.md` for detailed architecture and `pyproject.toml` for tool config.
