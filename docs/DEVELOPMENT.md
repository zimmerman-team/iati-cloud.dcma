# Development

## Project Structure

```
iati-cloud-dcma/
├── app/
│   ├── iati-cloud-dcma/
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
@app.route("/iati-cloud-dcma/my-endpoint", methods=["GET"])
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
    response = client.get("/iati-cloud-dcma/my-endpoint", headers=auth_headers)
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

## Default Limits

Every limit that bounds the work or the size of one request. The ones in the first table are settings and can be set from `.env`. The others are constants in the code.

### Configurable (`app/config.py`, `Settings`)

| Setting | Default | Bound | What it limits |
|---|---|---|---|
| `MAX_DEPTH` | 20 | 1–50 (`MAX_DEPTH_CEILING`) | Hops per walk (up or down). A ceiling: callers can lower it with `?max_depth=` but not raise it. |
| `MAX_NODES` | 1,000 | ≥ 1 | Nodes per response, organisations included. A ceiling, like `MAX_DEPTH`. It also sets the reverse-lookup limit (`mention_limit`): a mention query stops after this many matching documents. |
| `ID_BATCH_SIZE` | 200 | 1–250 (`MAX_ID_BATCH_SIZE`) | Identifiers per Solr query (fetch and mention). |
| `SOLR_TIMEOUT` | 10 s | > 0 | One Solr query (the pysolr client timeout). |
| `REQUEST_TIMEOUT` | 30 s | > 0 | Time budget for one `/chain` request. When it runs out, the result is truncated (`time_budget`). |
| `CACHE_TTL` | 86,400 s | > 0 | How long a response stays in Redis. |

### Constants

| Constant | Value | Where | What it limits |
|---|---|---|---|
| `MAX_DEPTH_CEILING` | 50 | `app/config.py` | Highest allowed `MAX_DEPTH`. The tree format recurses one level per hop, so this keeps it far from Python's recursion limit. |
| `MAX_ID_BATCH_SIZE` | 250 | `app/config.py` | Highest allowed `ID_BATCH_SIZE`. A mention query uses 4 boolean clauses per identifier, and 250 × 4 = 1,000 stays under Solr's default `maxBooleanClauses` (1,024). |
| `FETCH_MIN_ROWS` | 100 | `app/iati_cloud_dcma/source.py` | Smallest page size of a fetch by identifier, so a heavily duplicated identifier does not cost one query per copy. |
| `MENTION_ROWS_PER_ID` | 5 | `app/iati_cloud_dcma/source.py` | Page size of a mention query, per identifier in the batch. |
| `MENTION_MIN_ROWS` | 100 | `app/iati_cloud_dcma/source.py` | Smallest page size of a mention query. |
| `CONNECTION_RETRIES` | 2 | `app/iati_cloud_dcma/source.py` | Retries of a Solr request after a connection failure. Read timeouts and HTTP errors are never retried, and no retry starts once the request budget is spent. |
| `MAX_WALK_ROUNDS` | 3 | `app/iati_cloud_dcma/traversal.py` | How often the walks are repeated when a new spelling of an identifier turns up. |
| `MAX_IDENTIFIER_LENGTH` | 255 | `app/iati_cloud_dcma/nodes.py` | Longer published identifiers are ignored, never sent back to Solr. |
| `REDIS_TIMEOUT_SECONDS` | 2 | `app/cache.py` | Redis connect and read timeout. On failure the cache fails open. |
| `LOG_SIZE` | 10 MB (5 backups) | `app/config.py` | Size of the rotating log file. |

### Deployment

| Setting | Value | Where | Note |
|---|---|---|---|
| Gunicorn `--workers` | 4 | `Dockerfile`, `docker-compose.yaml` | Concurrent `/chain` requests per container. Not load-tested. |
| Gunicorn `--timeout` | 60 s | `Dockerfile`, `docker-compose.yaml` | Must stay above `REQUEST_TIMEOUT + SOLR_TIMEOUT`: a query that starts just before the budget runs out can still take its full timeout. Raise it with either setting. |

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
