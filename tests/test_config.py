"""Tests for app.config.Settings: DCM settings, defaults and validation."""

import logging
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import MAX_ID_BATCH_SIZE, Settings

_ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

_DCM_ENV_VARS = [
    "SOLR_URL",
    "SOLR_TIMEOUT",
    "REQUEST_TIMEOUT",
    "MAX_DEPTH",
    "MAX_NODES",
    "ID_BATCH_SIZE",
    "REDIS_URL",
    "CACHE_TTL",
]

_POSITIVE_INT_VARS = ["SOLR_TIMEOUT", "REQUEST_TIMEOUT", "MAX_DEPTH", "MAX_NODES", "ID_BATCH_SIZE", "CACHE_TTL"]

# Fields that are derived in code rather than configured through the environment.
_NON_ENV_FIELDS = {"auth_exempt_paths"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Make sure variables from the developer's shell don't leak into these tests."""
    for name in _DCM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_dcm_defaults_match_spec():
    s = _settings()
    assert s.solr_url == "https://fcdo.iati.cloud/search/activity/"
    assert s.solr_timeout == 10
    assert s.request_timeout == 30
    assert s.max_depth == 20
    assert s.max_nodes == 1000
    assert s.id_batch_size == 200
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.cache_ttl == 86400


@pytest.mark.parametrize("value", ["", None])
def test_secret_key_is_required(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SECRET_KEY")
    else:
        monkeypatch.setenv("SECRET_KEY", value)
    with pytest.raises(ValidationError):
        _settings()


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("SOLR_URL", "http://solr:8983/solr/activity/")
    monkeypatch.setenv("MAX_DEPTH", "4")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/1")
    s = _settings()
    assert s.solr_url == "http://solr:8983/solr/activity/"
    assert s.max_depth == 4
    assert s.redis_url == "redis://redis:6379/1"


def test_settings_read_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SOLR_URL=https://example.org/search/activity/\nMAX_NODES=50\nCACHE_TTL=60\n")
    s = Settings(_env_file=env_file)
    assert s.solr_url == "https://example.org/search/activity/"
    assert s.max_nodes == 50
    assert s.cache_ttl == 60


def test_unknown_variable_in_env_file_is_rejected(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NOT_A_SETTING=1\n")
    with pytest.raises(ValidationError, match="not_a_setting"):
        Settings(_env_file=env_file)


@pytest.mark.parametrize("name", _POSITIVE_INT_VARS)
@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_integer_settings_must_be_positive(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        _settings()


def test_id_batch_size_upper_bound(monkeypatch):
    monkeypatch.setenv("ID_BATCH_SIZE", str(MAX_ID_BATCH_SIZE))
    assert _settings().id_batch_size == 250
    monkeypatch.setenv("ID_BATCH_SIZE", str(MAX_ID_BATCH_SIZE + 1))
    with pytest.raises(ValidationError):
        _settings()


@pytest.mark.parametrize("value", ["not-a-url", "ftp://solr/activity/", "http://", "redis://solr:6379/0"])
def test_solr_url_must_be_http(monkeypatch, value):
    monkeypatch.setenv("SOLR_URL", value)
    with pytest.raises(ValidationError):
        _settings()


@pytest.mark.parametrize("value", ["redis://redis:6379/0", "rediss://cache.example.org:6380/0", "unix:///tmp/r.sock"])
def test_redis_url_accepts_redis_schemes(monkeypatch, value):
    monkeypatch.setenv("REDIS_URL", value)
    assert _settings().redis_url == value


@pytest.mark.parametrize("value", ["localhost:6379", "http://redis:6379/0", "redis://"])
def test_redis_url_rejects_other_schemes(monkeypatch, value):
    monkeypatch.setenv("REDIS_URL", value)
    with pytest.raises(ValidationError):
        _settings()


def test_env_example_documents_every_setting():
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", _ENV_EXAMPLE.read_text(), flags=re.MULTILINE))
    expected = {name.upper() for name in Settings.model_fields} - {f.upper() for f in _NON_ENV_FIELDS}
    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"


def test_max_depth_upper_bound(monkeypatch):
    monkeypatch.setenv("MAX_DEPTH", "50")
    assert _settings().max_depth == 50
    monkeypatch.setenv("MAX_DEPTH", "51")
    with pytest.raises(ValidationError):
        _settings()


def test_pysolr_errors_are_logged_once_by_the_source_not_by_pysolr():
    # setup_logging runs when app.config is imported; app.iati_cloud_dcma.source logs each handled Solr failure.
    assert logging.getLogger("pysolr").getEffectiveLevel() == logging.CRITICAL
    assert logging.getLogger("app.iati_cloud_dcma.source").getEffectiveLevel() == logging.INFO
