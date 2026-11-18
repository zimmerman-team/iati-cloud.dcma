"""Where links come from: the LinkSource protocol and its implementation over the IATI.cloud Solr activity core.

Traversal only talks to a LinkSource, so moving to another store (say, a precomputed edge index) means writing a new
implementation, not changing the traversal.
"""

import logging
from collections import defaultdict
from collections.abc import Collection, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from http.client import RemoteDisconnected
from types import TracebackType
from typing import Literal, NoReturn, Protocol, Self, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

import pysolr
import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import MaxRetryError, ProtocolError
from urllib3.util.retry import Retry

from app.iati_cloud_dcma.budget import UNLIMITED, Budget, BudgetExhausted
from app.iati_cloud_dcma.links import (
    FIELD_DOC_ID,
    FIELD_IDENTIFIER,
    SOLR_FIELDS,
    ActivityDoc,
    InvalidDocument,
)
from app.iati_cloud_dcma.nodes import NodeId, node_key, normalise_id

logger = logging.getLogger(__name__)

# Solr query fields. The underscore fields are exact-match copies, and case-sensitive: an identifier is only found
# under the spelling it was published with. participating-org.activity-id is tokenised and has no exact copy, so its
# matches are only candidates and are checked against the parsed links. Kept in one place because IATI.cloud instances may differ.
MENTION_QUERY_FIELDS: tuple[str, ...] = (
    "transaction_provider_org_provider_activity_id",
    "transaction_receiver_org_receiver_activity_id",
    "related_activity_ref",
    "participating-org.activity-id",
)
# A stable order makes start-based paging safe. Identifiers repeat (several documents can share one), so the document
# id breaks ties. cursorMark is rejected by the reference instance.
SORT = f"{FIELD_IDENTIFIER} asc,{FIELD_DOC_ID} asc"
# Page size of a fetch by identifier. One identifier can have hundreds of documents; without a floor, a single such
# seed would cost one query per copy.
FETCH_MIN_ROWS = 100
# Page size of a mention query: per identifier in the batch, with a floor. One activity can be cited by many others,
# and many matches are false candidates (every FCDO project is cited by its ~24 siblings through related_activity_ref,
# type 3), so small frontiers would otherwise page in tiny steps.
MENTION_ROWS_PER_ID = 5
MENTION_MIN_ROWS = 100
# How often a Solr request is sent again after a connection-level failure (see ConnectionRetry).
CONNECTION_RETRIES = 2

_FL = ",".join(SOLR_FIELDS)
# The time budget of the request whose Solr query is in flight. The pysolr client and its retry policy are shared by
# all requests of a worker, so the budget reaches ConnectionRetry this way rather than as an argument.
_query_budget: ContextVar[Budget] = ContextVar("solr_query_budget", default=UNLIMITED)


class SourceError(Exception):
    """The link source could not answer. Messages are safe to show: no URLs or queries."""


class SourceUnavailable(SourceError):
    """Solr returned an error, could not be reached, or sent something that isn't a Solr response."""


class SourceTimeout(SourceError):
    """A Solr query timed out."""


StopReason = Literal["mention_limit", "time_budget"]


@dataclass(frozen=True)
class Mentions:
    """The answer to a reverse lookup: one merged document per identifier, and why it stopped early, if it did."""

    docs: tuple[ActivityDoc, ...]
    stopped_by: StopReason | None = None

    @property
    def complete(self) -> bool:
        return self.stopped_by is None


@runtime_checkable
class LinkSource(Protocol):
    @property
    def query_count(self) -> int:
        """Number of queries this source has sent, for stats.solr_queries. Connection retries are not counted."""
        ...

    def fetch(self, ids: Collection[str], budget: Budget = UNLIMITED) -> dict[NodeId, ActivityDoc]:
        """The published activities among `ids`, by node key. Unknown identifiers are absent.

        `ids` may be in any letter case. All documents that share an identifier are merged into one.
        Raises BudgetExhausted when the budget runs out first: a partial answer would make published activities look
        unpublished.
        """
        ...

    def mentioning(self, ids: Collection[str], budget: Budget = UNLIMITED, limit: int | None = None) -> Mentions:
        """Every activity whose own links name one of `ids`: the reverse lookup.

        Needed because many recipients declare their funding pointing up, and the funder never names them.

                `ids` are spellings: pass every known spelling of an identifier, because some link fields only match exactly.
                The documents returned hold the links of the matching copies only; fetch an activity to get all of them.
                Stops early when the budget runs out, or once `limit` activities are found and more remain.
        """
        ...

    def ping(self) -> bool:
        """Whether the source answers at all."""
        ...


def quote_id(identifier: str) -> str:
    """Quote an identifier for a Solr query. Inside quotes only `"` and `\\` are special; `/` is literal."""
    return '"' + identifier.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ConnectionRetry(Retry):
    """Sends a Solr request again only when it failed before any response arrived.

    Retried: connection failures (refused, name resolution, connect timeout) and a disconnect before the first byte of
    the response. The reference proxy (nginx) closes keep-alive connections after 75 s idle; a pooled connection
    reused at that moment fails with RemoteDisconnected, and a new connection succeeds.
    Not retried: read timeouts and HTTP error responses. They reach the caller as 504 and 502.
    Retrying is safe because every Solr call is a search: a GET, or a POST for long queries, which changes nothing.
    No retry starts once the request's time budget is spent; the failure is then raised as if retries had run out.
    """

    def _is_connection_error(self, err: Exception) -> bool:
        # urllib3 counts every ProtocolError as a read error, read timeouts included. A disconnect before any response
        # means the server sent nothing, so it is treated like a failed connect.
        return super()._is_connection_error(err) or _is_disconnect_before_response(err)

    def increment(
        self,
        method: str | None = None,
        url: str | None = None,
        response=None,
        error: Exception | None = None,
        _pool=None,
        _stacktrace: TracebackType | None = None,
    ) -> Self:
        # urllib3 passes the pool it got as _pool, possibly None, and makes the same call with a type ignore.
        if error is not None and self._is_connection_error(error) and _query_budget.get().expired():
            raise MaxRetryError(_pool, url, error) from error  # ty: ignore[invalid-argument-type]
        return super().increment(method, url, response, error, _pool, _stacktrace)


def _is_disconnect_before_response(err: Exception) -> bool:
    return isinstance(err, ProtocolError) and any(isinstance(arg, RemoteDisconnected) for arg in err.args)


def create_solr_session(retries: int = CONNECTION_RETRIES) -> requests.Session:
    """A requests session that retries connection-level failures (ConnectionRetry) and nothing else."""
    policy = ConnectionRetry(total=retries, connect=retries, read=False, redirect=False, status=0, other=0)
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=policy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def create_solr_client(solr_url: str, timeout: int) -> pysolr.Solr:
    """A pysolr client whose searches go to exactly `solr_url`, over a session from create_solr_session.

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
    return pysolr.Solr(base_url, search_handler=handler, timeout=timeout, session=create_solr_session())


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

    def fetch(self, ids: Collection[str], budget: Budget = UNLIMITED) -> dict[NodeId, ActivityDoc]:
        copies: dict[NodeId, list[ActivityDoc]] = defaultdict(list)
        for batch in self._batches(ids):
            wanted = frozenset(map(node_key, batch))
            query = f"{FIELD_IDENTIFIER}:({' OR '.join(map(quote_id, batch))})"
            for page, _more in self._pages(query, max(FETCH_MIN_ROWS, len(batch)), budget):
                for doc in page:
                    if doc.key in wanted:
                        copies[doc.key].append(doc)
        return _merged(copies)

    def mentioning(self, ids: Collection[str], budget: Budget = UNLIMITED, limit: int | None = None) -> Mentions:
        copies: dict[NodeId, list[ActivityDoc]] = defaultdict(list)
        stopped_by: StopReason | None = None
        try:
            for batch in self._batches(ids):
                at_limit = limit is not None and len(copies) >= limit
                if at_limit or not self._collect_mentions(batch, budget, limit, copies):
                    stopped_by = "mention_limit"
                    break
        except BudgetExhausted:
            stopped_by = "time_budget"
        return Mentions(docs=tuple(_merged(copies).values()), stopped_by=stopped_by)

    def ping(self) -> bool:
        try:
            self._client.search("*:*", rows=0)
        except (pysolr.SolrError, requests.RequestException, ValueError) as exc:
            logger.warning("Solr ping failed: %s", exc)
            return False
        return True

    def _batches(self, ids: Collection[str]) -> Iterator[list[str]]:
        """Batches of distinct, trimmed identifiers. Spellings that differ only in case stay separate."""
        unique = sorted({clean for clean in map(normalise_id, ids) if clean is not None})
        for start in range(0, len(unique), self._batch_size):
            yield unique[start : start + self._batch_size]

    def _collect_mentions(
        self, batch: list[str], budget: Budget, limit: int | None, copies: dict[NodeId, list[ActivityDoc]]
    ) -> bool:
        """Add one batch's confirmed mentions to `copies`. False when it stopped at the limit with pages left."""
        wanted = frozenset(map(node_key, batch))
        values = " OR ".join(map(quote_id, batch))
        query = " OR ".join(f"{field}:({values})" for field in MENTION_QUERY_FIELDS)
        rows = max(MENTION_MIN_ROWS, len(batch) * MENTION_ROWS_PER_ID)
        for page, more in self._pages(query, rows, budget):
            for doc in page:
                # Solr matches are candidates only: the tokenised field also matches longer identifiers.
                if doc.linked_activity_ids & wanted:
                    copies[doc.key].append(doc)
            if more and limit is not None and len(copies) >= limit:
                return False
        return True

    def _pages(self, query: str, rows: int, budget: Budget) -> Iterator[tuple[list[ActivityDoc], bool]]:
        """Each page of matching documents, paged with `start`, and whether more pages follow.

        The budget is checked before every query. Invalid documents are logged and skipped.
        """
        start = 0
        while True:
            budget.check()
            results = self._search(query, rows=rows, start=start, budget=budget)
            start += rows
            more = bool(results.docs) and start < results.hits
            yield _parse(results.docs), more
            if not more:
                return

    def _search(self, query: str, rows: int, start: int, budget: Budget) -> pysolr.Results:
        # One query, however many connection retries it took: stats.solr_queries counts the traversal's work, which
        # stays the same for the same chain. Each retry is logged by urllib3 at WARNING.
        self._query_count += 1
        token = _query_budget.set(budget)
        try:
            return self._client.search(query, fl=_FL, rows=rows, start=start, sort=SORT)
        except (pysolr.SolrError, requests.RequestException, ValueError) as exc:
            _raise_source_error(exc, query)
        finally:
            _query_budget.reset(token)


def _merged(copies: dict[NodeId, list[ActivityDoc]]) -> dict[NodeId, ActivityDoc]:
    """One document per identifier, in key order. Some identifiers are published in several documents."""
    return {key: ActivityDoc.merge(copies[key]) for key in sorted(copies)}


def _parse(raw_docs: Iterable[dict]) -> list[ActivityDoc]:
    docs = []
    for raw in raw_docs:
        try:
            docs.append(ActivityDoc.from_solr(raw))
        except InvalidDocument as exc:
            logger.warning("Skipping Solr document: %s", exc)
    return docs


def _raise_source_error(exc: Exception, query: str) -> NoReturn:
    """Log the details for operators and raise an error that is safe to show to callers."""
    timed_out = isinstance(exc, requests.Timeout) or isinstance(exc.__context__, requests.Timeout)
    logger.error("Solr query %s: %s (query: %.500s)", "timed out" if timed_out else "failed", exc, query)
    if timed_out:
        raise SourceTimeout("The data source timed out.") from exc
    raise SourceUnavailable("The data source is unavailable.") from exc
