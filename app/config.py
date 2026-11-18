import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Annotated
from urllib.parse import urlparse

from pydantic import AfterValidator, Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.blueprint import AUTH_EXEMPT_PATHS as _BLUEPRINT_EXEMPT_PATHS

_URL_PREFIX = "/iati-cloud-dcma"

logger = logging.getLogger("app.config")

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_SIZE = 10 * 1024 * 1024  # 10 MB

# A mention query uses 4 boolean clauses per identifier, one per link field. 250 x 4 = 1,000 stays under Solr's default
# maxBooleanClauses of 1,024.
MAX_ID_BATCH_SIZE = 250
# The tree format is built recursively, one level per hop; this keeps it far from Python's recursion limit.
MAX_DEPTH_CEILING = 50


def _url_validator(*schemes: str, needs_host: bool = True):
    """Build a validator that accepts a URL with one of the given schemes (and a host, if required)."""

    def check(value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in schemes or (needs_host and not parsed.netloc):
            raise ValueError(f"must be a URL with scheme {' or '.join(schemes)}")
        if not needs_host and not (parsed.netloc or parsed.path):
            raise ValueError("must name a host or a socket path")
        return value

    return AfterValidator(check)


_SOLR_SCHEMES = ("http", "https")
_REDIS_SCHEMES = ("redis", "rediss", "unix")


def setup_logging() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(os.path.join(_LOG_DIR, "iati-cloud-dcma.log"), maxBytes=LOG_SIZE, backupCount=5)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Root at WARNING: library noise (werkzeug, pysolr) stays out unless it's an error
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # All app.* loggers inherit INFO from this parent
    logging.getLogger("app").setLevel(logging.INFO)

    # pysolr logs every failed request at ERROR with a traceback and the full URL. app.iati_cloud_dcma.source catches
    # each failure and logs it once, so pysolr's own line is a duplicate.
    logging.getLogger("pysolr").setLevel(logging.CRITICAL)


class Settings(BaseSettings):
    """Application settings"""

    # Server
    port: int = 5000
    flask_debug: bool = False
    cors_allowed_origins: list[str] = ["http://localhost:3000"]

    # Authentication: required. With an empty key, an empty Authorization header would be accepted.
    secret_key: str = Field(min_length=1)

    # Swagger UI and the health check are reachable without the key
    auth_exempt_paths: set[str] = {f"{_URL_PREFIX}{p}" for p in _BLUEPRINT_EXEMPT_PATHS}

    # Data source: an IATI.cloud Solr activity core
    solr_url: Annotated[str, _url_validator(*_SOLR_SCHEMES)] = "https://fcdo.iati.cloud/search/activity/"
    solr_timeout: PositiveInt = 10  # seconds, per Solr query
    request_timeout: PositiveInt = 30  # seconds, overall budget per /chain request

    # Traversal limits: ceilings that callers may lower but not raise
    max_depth: Annotated[int, Field(gt=0, le=MAX_DEPTH_CEILING)] = 20
    max_nodes: PositiveInt = 1000
    id_batch_size: Annotated[int, Field(gt=0, le=MAX_ID_BATCH_SIZE)] = 200

    # Response cache
    redis_url: Annotated[str, _url_validator(*_REDIS_SCHEMES, needs_host=False)] = "redis://localhost:6379/0"
    cache_ttl: PositiveInt = 86400  # seconds

    # Model config
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="forbid")


settings = Settings()
setup_logging()
