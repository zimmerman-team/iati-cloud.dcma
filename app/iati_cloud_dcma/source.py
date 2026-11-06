"""Where links come from: the LinkSource protocol and its IATI.cloud Solr implementation (spec §5.4, §5.5, §8).

Traversal only talks to a LinkSource, so a future graph store means a new implementation, not a rewrite (plan D3).
"""

import logging
from collections.abc import Collection, Iterator
from typing import NoReturn, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

import pysolr
import requests

from app.iati_cloud_dcma.links import FIELD_IDENTIFIER, SOLR_FIELDS, ActivityDoc, InvalidDocument, normalise_id

logger = logging.getLogger(__name__)

# Solr query fields. The underscore fields are exact-match copies; participating-org.activity-id is tokenised and has
# no exact copy, so its matches are only candidates (spec §5.5). Kept in one place because IATI.cloud instances may
# differ (plan §5, risks).
MENTION_QUERY_FIELDS: tuple[str, ...] = (
    "transaction_provider_org_provider_activity_id",
    "transaction_receiver_org_receiver_activity_id",
    "related_activity_ref",
    "participating-org.activity-id",
)
# A stable order makes start-based paging safe. cursorMark is not an option: it needs the uniqueKey as the sort.
SORT = f"{FIELD_IDENTIFIER} asc"
# Page size of a mention query: per identifier in the batch, with a floor. One activity can be cited by many others,
# and many matches are false candidates (every FCDO project is cited by its ~24 siblings through related_activity_ref,
# type 3), so small frontiers would otherwise page in tiny steps.
MENTION_ROWS_PER_ID = 5
MENTION_MIN_ROWS = 100

_FL = ",".join(SOLR_FIELDS)


class SourceError(Exception):
    """The link source could not answer. Messages are safe to show: no URLs or queries."""


class SourceUnavailable(SourceError):
    """Solr returned an error, could not be reached, or sent something that isn't a Solr response."""


class SourceTimeout(SourceError):
    """A Solr query timed out."""


@runtime_checkable
class LinkSource(Protocol):
    @property
    def query_count(self) -> int:
        """Number of queries this source has sent, for stats.solr_queries."""
        ...

    def fetch(self, ids: Collection[str]) -> dict[str, ActivityDoc]:
        """The published documents among `ids`, by identifier. Unknown identifiers are absent."""
        ...

    def mentioning(self, ids: Collection[str]) -> list[ActivityDoc]:
        """Every document whose own links name one of `ids` (spec §5.4)."""
        ...

    def ping(self) -> bool:
        """Whether the source answers at all."""
        ...


def quote_id(identifier: str) -> str:
    """Quote an identifier for a Solr query. Inside quotes only `"` and `\\` are special; `/` is literal."""
    return '"' + identifier.replace("\\", "\\\\").replace('"', '\\"') + '"'


def create_solr_client(solr_url: str, timeout: int) -> pysolr.Solr:
    """A pysolr client whose searches go to exactly `solr_url`.

    SOLR_URL names the endpoint that answers select queries: the reference instance's proxy answers at
    https://fcdo.iati.cloud/search/activity/ and returns 404 for .../select/. pysolr always appends a handler to its
    base URL, so the last path segment becomes the handler (for a plain Solr core, use .../solr/<core>/select).
    """
    parts = urlsplit(solr_url)
    path = parts.path.rstrip("/")
    if not path.strip("/"):
        raise ValueError("SOLR_URL must include the path of the select endpoint, e.g. /search/activity/")
    base_path, _, handler = path.rpartition("/")
    base_url = urlunsplit((parts.scheme, parts.netloc, base_path, "", ""))
    return pysolr.Solr(base_url, search_handler=handler, timeout=timeout)


class SolrLinkSource:
    """LinkSource over an IATI.cloud Solr activity core.

    Cheap to create: make one per request around a shared client, so query_count belongs to that request.
    """

    def __init__(self, client: pysolr.Solr, batch_size: int):
        self._client = client
        self._batch_size = batch_size
        self._query_count = 0

    @property
    def query_count(self) -> int:
        return self._query_count

    def fetch(self, ids: Collection[str]) -> dict[str, ActivityDoc]:
        docs: dict[str, ActivityDoc] = {}
        for batch in self._batches(ids):
            wanted = frozenset(batch)
            query = f"{FIELD_IDENTIFIER}:({' OR '.join(map(quote_id, batch))})"
            for doc in self._documents(query, rows=len(batch)):
                if doc.iati_identifier in wanted and doc.iati_identifier not in docs:
                    docs[doc.iati_identifier] = doc
        return docs

    def mentioning(self, ids: Collection[str]) -> list[ActivityDoc]:
        docs: dict[str, ActivityDoc] = {}
        for batch in self._batches(ids):
            wanted = frozenset(batch)
            values = " OR ".join(map(quote_id, batch))
            query = " OR ".join(f"{field}:({values})" for field in MENTION_QUERY_FIELDS)
            rows = max(MENTION_MIN_ROWS, len(batch) * MENTION_ROWS_PER_ID)
            for doc in self._documents(query, rows=rows):
                # Solr matches are candidates only: confirm an exact identifier (tokenised field, spec §5.5).
                if doc.linked_activity_ids & wanted and doc.iati_identifier not in docs:
                    docs[doc.iati_identifier] = doc
        return list(docs.values())

    def ping(self) -> bool:
        try:
            self._client.search("*:*", rows=0)
        except (pysolr.SolrError, requests.RequestException, ValueError) as exc:
            logger.warning("Solr ping failed: %s", exc)
            return False
        return True

    def _batches(self, ids: Collection[str]) -> Iterator[list[str]]:
        unique = sorted({clean for clean in map(normalise_id, ids) if clean is not None})
        for start in range(0, len(unique), self._batch_size):
            yield unique[start : start + self._batch_size]

    def _documents(self, query: str, rows: int) -> Iterator[ActivityDoc]:
        """Every matching document, paged with `start`. Invalid documents are logged and skipped."""
        start = 0
        while True:
            results = self._search(query, rows=rows, start=start)
            for raw in results.docs:
                try:
                    yield ActivityDoc.from_solr(raw)
                except InvalidDocument as exc:
                    logger.warning("Skipping Solr document: %s", exc)
            start += rows
            if not results.docs or start >= results.hits:
                return

    def _search(self, query: str, rows: int, start: int) -> pysolr.Results:
        self._query_count += 1
        try:
            return self._client.search(query, fl=_FL, rows=rows, start=start, sort=SORT)
        except (pysolr.SolrError, requests.RequestException, ValueError) as exc:
            _raise_source_error(exc, query)


def _raise_source_error(exc: Exception, query: str) -> NoReturn:
    """Log the details for operators and raise an error that is safe to show to callers."""
    timed_out = isinstance(exc, requests.Timeout) or isinstance(exc.__context__, requests.Timeout)
    logger.error("Solr query %s: %s (query: %.500s)", "timed out" if timed_out else "failed", exc, query)
    if timed_out:
        raise SourceTimeout("The data source timed out.") from exc
    raise SourceUnavailable("The data source is unavailable.") from exc
