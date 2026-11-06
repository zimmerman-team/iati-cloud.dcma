import logging
import os
from logging.handlers import RotatingFileHandler

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.blueprint import AUTH_EXEMPT_PATHS as _BLUEPRINT_EXEMPT_PATHS

_URL_PREFIX = "/iati-cloud.dcma"

logger = logging.getLogger("app.config")

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_SIZE = 10 * 1024 * 1024  # 10 MB


def setup_logging() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(os.path.join(_LOG_DIR, "iati-cloud.dcma.log"), maxBytes=LOG_SIZE, backupCount=5)
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

    # Model config
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="forbid")


settings = Settings()
setup_logging()
