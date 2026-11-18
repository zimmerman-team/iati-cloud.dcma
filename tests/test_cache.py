"""Tests for app.cache: the Redis response cache. Redis is an in-memory fake."""

import logging

import fakeredis
import pytest
import redis

from app.cache import KEY_PREFIX, ResponseCache, cache_key, create_redis_client

LOGGER = "app.cache"


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


def _key(activity="GB-1", mode="full", response_format="graph", orgs="identified", max_depth=10, max_nodes=500) -> str:
    return cache_key(activity, mode, response_format, orgs, max_depth, max_nodes)


def test_cache_key_is_a_prefixed_hash():
    key = _key()
    assert key.startswith(KEY_PREFIX)
    assert len(key) == len(KEY_PREFIX) + 64
    assert "GB-1" not in key


@pytest.mark.parametrize("activity", ["gb-1", " GB-1 ", "Gb-1\n"])
def test_equivalent_spellings_of_the_activity_share_a_key(activity):
    assert _key(activity=activity) == _key()


@pytest.mark.parametrize(
    "change",
    [
        {"activity": "GB-2"},
        {"mode": "constrained"},
        {"response_format": "tree"},
        {"orgs": "refs"},
        {"orgs": "all"},
        {"max_depth": 9},
        {"max_nodes": 499},
    ],
)
def test_everything_that_shapes_the_answer_is_in_the_key(change):
    assert _key(**change) != _key()


def test_set_then_get_round_trips_the_payload(fake_redis):
    cache = ResponseCache(fake_redis, ttl=60)
    payload = {"seed": "GB-1", "nodes": [{"id": "GB-1", "title": "Écoles"}], "truncated": False}
    cache.set("k", payload)
    assert cache.get("k") == payload


def test_get_of_a_missing_key_is_none(fake_redis):
    assert ResponseCache(fake_redis, ttl=60).get("nothing") is None


def test_entries_expire_after_the_ttl(fake_redis):
    ResponseCache(fake_redis, ttl=86400).set("k", {"a": 1})
    assert 0 < fake_redis.ttl("k") <= 86400


@pytest.mark.parametrize("stored", ["{not json", "[1, 2]", '"text"'])
def test_unreadable_entry_is_ignored_and_logged(fake_redis, caplog, stored):
    fake_redis.set("k", stored)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert ResponseCache(fake_redis, ttl=60).get("k") is None
    assert ("{" in stored) == bool([r for r in caplog.records if r.name == LOGGER])


@pytest.fixture
def broken_redis(mocker):
    client = mocker.create_autospec(redis.Redis, instance=True)
    for method in (client.get, client.set, client.ping):
        method.side_effect = redis.ConnectionError("connection refused")
    return client


def test_read_error_is_logged_and_gives_a_miss(broken_redis, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert ResponseCache(broken_redis, ttl=60).get("k") is None
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]


def test_write_error_is_logged_and_does_not_raise(broken_redis, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ResponseCache(broken_redis, ttl=60).set("k", {"a": 1})
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]


def test_ping(fake_redis, broken_redis):
    assert ResponseCache(fake_redis, ttl=60).ping() is True
    assert ResponseCache(broken_redis, ttl=60).ping() is False


def test_create_redis_client_does_not_connect():
    client = create_redis_client("redis://nowhere.invalid:6379/0")
    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["decode_responses"]) == ("nowhere.invalid", True)
    assert kwargs["socket_timeout"] == kwargs["socket_connect_timeout"] == 2
