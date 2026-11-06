"""Tests for app.main."""

from freezegun import freeze_time


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


def test_requires_auth(client):
    response = client.get("/iati-cloud-dcma/iati-cloud-dcma-route")
    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}


def test_wrong_auth_key(client):
    response = client.get("/iati-cloud-dcma/iati-cloud-dcma-route", headers={"Authorization": "wrong"})
    assert response.status_code == 401


def test_correct_auth_key(client, auth_headers):
    response = client.get("/iati-cloud-dcma/iati-cloud-dcma-route", headers=auth_headers)
    assert response.status_code == 200


def test_swagger_ui_exempt_from_auth(client):
    response = client.get("/iati-cloud-dcma/docs/")
    assert response.status_code == 200


def test_apispec_exempt_from_auth(client):
    response = client.get("/iati-cloud-dcma/apispec.json")
    assert response.status_code == 200
