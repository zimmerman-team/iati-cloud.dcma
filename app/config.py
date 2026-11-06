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

# A mention query uses 4 boolean clauses per identifier (spec §5.4). 250 x 4 = 1,000 stays under Solr's default
# maxBooleanClauses of 1,024.
MAX_ID_BATCH_SIZE = 250


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

    # Root at WARNING - library noise (werkzeug, pysolr) stays out unless it's an error
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # All app.* loggers inherit INFO from this parent
    logging.getLogger("app").setLevel(logging.INFO)


class Settings(BaseSettings):
    """Application settings"""

    # Server
    port: int = 5000
    flask_debug: bool = False
    cors_allowed_origins: list[str] = ["http://localhost:3000"]

    # Authentication
    secret_key: str = ""

    # Swagger UI paths are exempt from authentication
    auth_exempt_paths: set[str] = {f"{_URL_PREFIX}{p}" for p in _BLUEPRINT_EXEMPT_PATHS}

    # Data source: an IATI.cloud Solr activity core (spec §5.1)
    solr_url: Annotated[str, _url_validator(*_SOLR_SCHEMES)] = "https://fcdo.iati.cloud/search/activity/"
    solr_timeout: PositiveInt = 10  # seconds, per Solr query
    request_timeout: PositiveInt = 30  # seconds, overall budget per /chain request

    # Traversal limits (spec §6.3): ceilings that callers may lower but not raise
    max_depth: PositiveInt = 10
    max_nodes: PositiveInt = 500
    id_batch_size: Annotated[int, Field(gt=0, le=MAX_ID_BATCH_SIZE)] = 100

    # Response cache (spec §8)
    redis_url: Annotated[str, _url_validator(*_REDIS_SCHEMES, needs_host=False)] = "redis://localhost:6379/0"
    cache_ttl: PositiveInt = 86400  # seconds

    # Model config
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="forbid")


settings = Settings()
setup_logging()
