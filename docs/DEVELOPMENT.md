# Development

## Project Structure

```
backend-template/
├── app/
│   ├── backend-template/
│   |   └── __init__.py  # Content of the actual feature goes in this directory
│   ├── blueprint.py     # Flask routes
│   ├── main.py          # Flask app, routes, auth middleware
│   ├── config.py        # Pydantic settings, logging setup
│   ├── docs.py          # Swagger/Flasgger configuration
│   └── models.py        # Pydantic data models
├── tests/
│   ├── conftest.py      # Pytest fixtures (Flask test client)
│   └── test_main.py     # Route and auth tests
├── data/                # Static data files
├── logs/                # Rotating log output (runtime)
├── scripts/
│   └── init_template.sh # Rename template to your project
├── docker-compose.yaml
├── Dockerfile
├── Makefile
└── pyproject.toml
```

Basic directory rules:

* Name directories after the domain, never src or api.
* Files live at the level of their most general consumer.
* Nest only when a subdomain is large enough to warrant it.
* Let the parent directory provide the namespace, never repeat it in filenames.
* Routes belong in blueprint.py, not in a subdirectory, unless there are too many routes on multiple domains, in which case we switch to a routes directory.

## Adding a New Endpoint

**Step 1**: Add the route to `app/main.py`

```python
@app.route("/backend-template/my-endpoint", methods=["GET"])
def my_endpoint():
    """
    Endpoint description.
    ---
    tags:
        - MyTag
    responses:
        200:
            description: Success.
    """
    return jsonify({"message": "success"})
```

**Step 2**: Add tests in `tests/test_main.py`

```python
def test_my_endpoint(client, auth_headers):
    response = client.get("/backend-template/my-endpoint", headers=auth_headers)
    assert response.status_code == 200
```

## Adding a New Configuration Option

**Step 1**: Add the field to `Settings` in `app/config.py`

```python
class Settings(BaseSettings):
    my_setting: str = "default"
```

**Step 2**: Reference it via `settings.my_setting` anywhere in the app.

**Step 3**: Add it to `.env.example`

```bash
MY_SETTING=
```

## Code Quality

```bash
make format        # Auto-format with `ruff check --fix` and `ruff format`
make lint          # flake8
make typecheck     # ty
make security      # bandit
make check-all     # All of the above + tests
```

## Updating Dependencies

```bash
# Add a dependency
uv add package-name

# Add a dev-only dependency
uv add --optional dev package-name

# Upgrade all to latest allowed versions
uv lock --upgrade
```

Always commit the updated `uv.lock`.

## Best Practices

1. Testing
   1. Write proper tests that test real cases
   2. Test edge cases
   3. Use fixtures - reuse test data across tests
   4. Mock external services - Don't hit real external services in tests
2. Keep functions small - functions should do one thing
3. Keep dependencies minimal - only add what's necessary
4. Keep dependencies up to date - make sure dependencies do not pose any security risks.
5. Document edge cases - Explain non-obvious logic choices
6. Update documentation - Keep documentation in sync with the code
7. Keep secrets safe - Move secrets to version controlled .env file, always replace default values when deploying.
