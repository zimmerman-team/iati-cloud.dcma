"""Tests for app.main."""

import pytest
from flask import jsonify
from freezegun import freeze_time

from app.main import create_app

_PROTECTED_PATH = "/iati-cloud-dcma/_test-protected"


@pytest.fixture
def protected_client():
    """A client for an app with an extra, test-only route that requires the API key."""
    app = create_app()
    app.config["TESTING"] = True

    @app.route(_PROTECTED_PATH)
    def _protected():
        return jsonify({"ok": True})

    with app.test_client() as client:
        yield client


def test_health_check(client, auth_headers):
    response = client.get("/iati-cloud-dcma/health-check", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "healthy"
    assert "timestamp" in data


@freeze_time("2024-01-01T00:00:00")
def test_health_check_timestamp(client, auth_headers):
    response = client.get("/iati-cloud-dcma/health-check", headers=auth_headers)
    assert response.get_json()["timestamp"] == "2024-01-01T00:00:00"


def test_health_check_exempt_from_auth(client):
    response = client.get("/iati-cloud-dcma/health-check")
    assert response.status_code == 200


def test_requires_auth(protected_client):
    response = protected_client.get(_PROTECTED_PATH)
    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}


def test_wrong_auth_key(protected_client):
    response = protected_client.get(_PROTECTED_PATH, headers={"Authorization": "wrong"})
    assert response.status_code == 401


def test_correct_auth_key(protected_client, auth_headers):
    response = protected_client.get(_PROTECTED_PATH, headers=auth_headers)
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_demo_route_removed(client, auth_headers):
    response = client.get("/iati-cloud-dcma/iati-cloud-dcma-route", headers=auth_headers)
    assert response.status_code == 404


def test_swagger_ui_exempt_from_auth(client):
    response = client.get("/iati-cloud-dcma/docs/")
    assert response.status_code == 200


def test_apispec_exempt_from_auth(client):
    response = client.get("/iati-cloud-dcma/apispec.json")
    assert response.status_code == 200
