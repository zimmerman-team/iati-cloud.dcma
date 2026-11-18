"""The Redis response cache."""

import hashlib
import json
import logging
from typing import Any

import redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "iati-cloud-dcma:chain:v1:"
REDIS_TIMEOUT_SECONDS = 2


def create_redis_client(redis_url: str) -> redis.Redis:
    """A client for `redis_url`. It connects lazily, so this works while Redis is down."""
    return redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
    )


def cache_key(activity: str, mode: str, response_format: str, orgs: str, max_depth: int, max_nodes: int) -> str:
    """One key per distinct result: the activity trimmed and case-folded, and everything else that shapes the answer.

    The limits are the effective ones (after capping), so requests that are answered the same share an entry.
    """
    normalised = [activity.strip().casefold(), mode, response_format, orgs, max_depth, max_nodes]
    digest = hashlib.sha256(json.dumps(normalised, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"{KEY_PREFIX}{digest}"


class ResponseCache:
    """JSON payloads by key, with a time to live."""

    def __init__(self, client: redis.Redis, ttl: int):
        self._client = client
        self._ttl = ttl

    def get(self, key: str) -> dict[str, Any] | None:
        """The cached payload, or None when there is none, it is unreadable, or Redis does not answer."""
        try:
            raw = self._client.get(key)
        except redis.RedisError as exc:
            logger.warning("Cache read failed, serving uncached: %s", exc)
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Ignoring unreadable cache entry %s", key)
            return None
        return payload if isinstance(payload, dict) else None

    def set(self, key: str, payload: dict[str, Any]) -> None:
        try:
            self._client.set(key, json.dumps(payload, ensure_ascii=False), ex=self._ttl)
        except redis.RedisError as exc:
            logger.warning("Cache write failed: %s", exc)

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.RedisError as exc:
            logger.warning("Cache ping failed: %s", exc)
            return False
