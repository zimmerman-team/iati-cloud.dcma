"""Answering one /chain request: cache lookup, traversal, response model, log line.

Kept apart from the Flask route so it can be tested without HTTP, and from app.config so the blueprint can import it
(app.config imports the blueprint).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.cache import ResponseCache, cache_key
from app.iati_cloud_dcma.budget import Budget
from app.iati_cloud_dcma.chain import Chain
from app.iati_cloud_dcma.source import LinkSource
from app.iati_cloud_dcma.traversal import Limits, build_chain
from app.iati_cloud_dcma.tree import to_tree
from app.models import ChainQuery, graph_response, tree_response

logger = logging.getLogger(__name__)

EXTENSION_KEY = "iati_cloud_dcma"


@dataclass(frozen=True)
class Services:
    """What the routes need, created once per app and replaceable in tests."""

    new_source: Callable[[], LinkSource]  # one source per request, so its query count belongs to that request
    cache: ResponseCache
    limits: Limits  # the configured ceilings
    request_timeout: float  # seconds


def effective_limits(query: ChainQuery, ceilings: Limits) -> Limits:
    """The caller may lower the limits, not raise them: the configured values protect the server."""
    return Limits(
        max_depth=min(query.max_depth or ceilings.max_depth, ceilings.max_depth),
        max_nodes=min(query.max_nodes or ceilings.max_nodes, ceilings.max_nodes),
    )


def chain_payload(services: Services, query: ChainQuery) -> dict[str, Any]:
    """The JSON body for one /chain request, from the cache when it is there.

    Raises SeedNotFound, BudgetExhausted and the source's SourceError subclasses.
    """
    started = time.perf_counter()
    limits = effective_limits(query, services.limits)
    key = cache_key(query.activity, query.mode, query.format, query.orgs, limits.max_depth, limits.max_nodes)

    payload = services.cache.get(key)
    cached = payload is not None
    solr_queries = 0
    if payload is None:
        source = services.new_source()
        budget = Budget.of_seconds(services.request_timeout)
        chain = build_chain(source, query.activity, query.mode, limits, budget=budget, orgs=query.orgs)
        solr_queries = source.query_count
        duration_ms = _elapsed_ms(started)
        if query.format == "tree":
            response = tree_response(chain, to_tree(chain), query, solr_queries, duration_ms)
        else:
            response = graph_response(chain, query, solr_queries, duration_ms)
        payload = response.model_dump(mode="json", by_alias=True)
        if _cacheable(chain):
            services.cache.set(key, payload)
    else:
        payload["stats"] = payload["stats"] | {"cached": True, "solr_queries": 0, "duration_ms": _elapsed_ms(started)}

    stats = payload["stats"]
    logger.info(
        "chain seed=%r mode=%s format=%s orgs=%s nodes=%d queries=%d duration_ms=%d truncation_reasons=%s cached=%s",
        payload["seed"],
        query.mode,
        query.format,
        query.orgs,
        stats["nodes"],
        solr_queries,
        stats["duration_ms"],
        ",".join(payload["truncation_reasons"]) or "-",
        cached,
    )
    return payload


def _cacheable(chain: Chain) -> bool:
    """Whether every request with this cache key would get this same result.

    Not when the time budget cut it short: that depends on how fast Solr was at that moment. Not for an unpublished
    seed either: it is only found under the spelling the caller typed (the link fields match case-sensitively), while
    the cache key is shared by all spellings.
    """
    seed_published = any(node.relation == "seed" and getattr(node, "published", False) for node in chain.nodes)
    return seed_published and "time_budget" not in chain.truncation_reasons


def health(services: Services) -> tuple[str, dict[str, str]]:
    """(status, dependencies). Unhealthy without Solr, since no request can be answered. Degraded without the cache,
    since requests are still answered, only slower."""
    solr_ok = services.new_source().ping()
    cache_ok = services.cache.ping()
    dependencies = {"solr": "ok" if solr_ok else "unavailable", "cache": "ok" if cache_ok else "unavailable"}
    if not solr_ok:
        return "unhealthy", dependencies
    return ("healthy" if cache_ok else "degraded"), dependencies


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
