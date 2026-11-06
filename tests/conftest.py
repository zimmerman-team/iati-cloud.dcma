"""Pytest configuration and fixtures."""

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from app.config import settings
from app.main import create_app

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "activities"


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def auth_headers():
    return {"Authorization": settings.secret_key}


def load_activity_fixture(identifier: str) -> dict:
    """Load a raw Solr activity document saved by scripts/fetch_fixture.py."""
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", identifier) + ".json"
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


@pytest.fixture
def activity_fixture() -> Callable[[str], dict]:
    return load_activity_fixture
