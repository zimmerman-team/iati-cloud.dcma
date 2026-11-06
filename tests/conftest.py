"""Pytest configuration and fixtures."""

import pytest

from app.config import settings
from app.main import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def auth_headers():
    return {"Authorization": settings.secret_key}
