"""Tests for app.main."""

import pytest
from flask import jsonify
from freezegun import freeze_time

from app.config import settings
from app.main import create_app, default_services
from tests.conftest import make_services

_PROTECTED_PATH = "/iati-cloud-dcma/_test-protected"


@pytest.fixture
def protected_client():
    """A client for an app with an extra, test-only route that requires the API key."""
    app = create_app(make_services())
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


def test_empty_auth_header_is_rejected(protected_client):
    response = protected_client.get(_PROTECTED_PATH, headers={"Authorization": ""})
    assert response.status_code == 401


def test_non_ascii_auth_key_is_rejected(protected_client):
    response = protected_client.get(_PROTECTED_PATH, headers={"Authorization": "cl\u00e9"})
    assert response.status_code == 401


def test_cors_preflight_needs_no_key(protected_client):
    response = protected_client.options(_PROTECTED_PATH, headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 200


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


def test_default_services_come_from_the_settings_without_connecting():
    services = default_services()
    assert (services.limits.max_depth, services.limits.max_nodes) == (settings.max_depth, settings.max_nodes)
    assert services.request_timeout == settings.request_timeout
    first, second = services.new_source(), services.new_source()
    assert first is not second  # one source per request
    assert first.query_count == 0


def test_app_without_explicit_services_uses_the_default_ones():
    app = create_app()
    assert app.extensions["iati_cloud_dcma"].limits.max_nodes == settings.max_nodes
