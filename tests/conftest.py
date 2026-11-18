"""Pytest configuration and fixtures."""

import io
import json
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable
from pathlib import Path

import fakeredis
import pytest
from urllib3 import HTTPConnectionPool, HTTPResponse

from app.cache import ResponseCache
from app.config import settings
from app.iati_cloud_dcma.budget import UNLIMITED, Budget
from app.iati_cloud_dcma.links import ActivityDoc
from app.iati_cloud_dcma.nodes import node_key
from app.iati_cloud_dcma.service import Services
from app.iati_cloud_dcma.source import Mentions
from app.iati_cloud_dcma.traversal import Limits
from app.main import create_app

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "activities"


TEST_LIMITS = Limits(max_depth=10, max_nodes=500)


@pytest.fixture
def solr_wire(mocker):
    """Replaces the socket exchange of every urllib3 connection pool; tests set its side_effect or return_value.

    Everything above it runs for real: pysolr, the requests session, its adapter and retry policy, and the mapping of
    urllib3 errors to requests exceptions. Nothing touches the network.
    """
    return mocker.patch.object(HTTPConnectionPool, "_make_request", autospec=True)


def solr_reply(docs: Iterable[dict] = (), status: int = 200) -> HTTPResponse:
    """A Solr select response as urllib3 returns it."""
    docs = list(docs)
    body = json.dumps({"response": {"docs": docs, "numFound": len(docs)}}).encode()
    return HTTPResponse(
        body=io.BytesIO(body), headers={"Content-Type": "application/json"}, status=status, preload_content=False
    )


def make_services(
    source: "FakeLinkSource | None" = None,
    *,
    redis_client=None,
    limits: Limits = TEST_LIMITS,
    request_timeout: float = 30,
    ttl: int = 60,
) -> Services:
    """Services around a FakeLinkSource and an in-memory Redis: nothing touches the network."""
    source = source if source is not None else FakeLinkSource()
    redis_client = redis_client if redis_client is not None else fakeredis.FakeRedis(decode_responses=True)
    return Services(
        new_source=lambda: source,
        cache=ResponseCache(redis_client, ttl=ttl),
        limits=limits,
        request_timeout=request_timeout,
    )


def make_client(services: Services):
    app = create_app(services)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def client():
    with make_client(make_services()) as client:
        yield client


@pytest.fixture
def auth_headers():
    return {"Authorization": settings.secret_key}


def load_activity_fixture(identifier: str) -> dict:
    """Load a raw Solr activity document saved by scripts/fetch_fixture.py."""
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", identifier) + ".json"
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


def load_activity_copies(identifier: str) -> list[dict]:
    """Load every raw document of one identifier, saved by scripts/fetch_fixture.py --copies."""
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", identifier) + ".copies.json"
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


@pytest.fixture
def activity_fixture() -> Callable[[str], dict]:
    return load_activity_fixture


def load_chain_fixture(name: str) -> dict:
    """Load the raw documents and run log saved by scripts/fetch_chain_fixture.py."""
    path = Path(__file__).resolve().parent / "fixtures" / "chains" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class FakeClock:
    """A clock for Budget that only moves when a test moves it."""

    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeLinkSource:
    """An in-memory LinkSource for traversal, tree and API tests.

    It answers like SolrLinkSource. Identifiers match in any letter case. Several documents may share an identifier:
    fetch merges all of them, while mentioning returns, sorted by identifier, the merge of only those copies whose
    links name one of the IDs (Solr returns matching documents, not their siblings). It honours the budget (checked
    before each call) and the mention limit. Every call is logged in `calls`; `on_call` runs after each one, so a test
    can move a FakeClock to simulate time spent in Solr.

    With exact_mentions=True the reverse lookup only finds an identifier under the spelling asked for, like Solr's
    exact-match link fields. (The default matches any letter case, like its tokenised field.)
    """

    def __init__(
        self,
        docs: Iterable[ActivityDoc] = (),
        *,
        healthy: bool = True,
        on_call: Callable[[], None] | None = None,
        exact_mentions: bool = False,
    ):
        self.exact_mentions = exact_mentions
        self._copies: dict[str, list[ActivityDoc]] = defaultdict(list)
        for doc in docs:
            self._copies[doc.key].append(doc)
        self.healthy = healthy
        self.on_call = on_call
        self.calls: list[tuple[str, frozenset[str]]] = []

    @classmethod
    def from_fixtures(cls, *identifiers: str) -> "FakeLinkSource":
        return cls(ActivityDoc.from_solr(load_activity_fixture(identifier)) for identifier in identifiers)

    @classmethod
    def from_raw(cls, *raw_docs: dict, **kwargs) -> "FakeLinkSource":
        return cls((ActivityDoc.from_solr(raw) for raw in raw_docs), **kwargs)

    @property
    def query_count(self) -> int:
        return len(self.calls)

    def reset_calls(self) -> None:
        self.calls.clear()

    def fetch(self, ids: Collection[str], budget: Budget = UNLIMITED) -> dict[str, ActivityDoc]:
        budget.check()
        wanted = sorted(map(node_key, self._log("fetch", ids)))
        return {key: ActivityDoc.merge(self._copies[key]) for key in wanted if key in self._copies}

    def mentioning(self, ids: Collection[str], budget: Budget = UNLIMITED, limit: int | None = None) -> Mentions:
        if budget.expired():
            return Mentions(docs=(), stopped_by="time_budget")
        asked = self._log("mentioning", ids)
        wanted = frozenset(map(node_key, asked))
        matching = {
            key: [copy for copy in copies if self._mentions(copy, asked, wanted)]
            for key, copies in sorted(self._copies.items())
        }
        docs = tuple(ActivityDoc.merge(copies) for copies in matching.values() if copies)
        if limit is not None and len(docs) > limit:
            return Mentions(docs=docs[:limit], stopped_by="mention_limit")
        return Mentions(docs=docs)

    def _mentions(self, copy: ActivityDoc, asked: frozenset[str], wanted: frozenset[str]) -> bool:
        linked = copy.linked_activity_ids & wanted
        if not self.exact_mentions:
            return bool(linked)
        return any(key in linked and spelling in asked for key, spelling in copy.labels)

    def ping(self) -> bool:
        return self.healthy

    def _log(self, method: str, ids: Collection[str]) -> frozenset[str]:
        wanted = frozenset(ids)
        self.calls.append((method, wanted))
        if self.on_call is not None:
            self.on_call()
        return wanted

    def fetched_ids(self) -> list[str]:
        return [i for method, ids in self.calls if method == "fetch" for i in ids]

    def mentioned_ids(self) -> list[str]:
        return [i for method, ids in self.calls if method == "mentioning" for i in ids]


def raw_activity(
    identifier: str,
    *,
    parent: str | None = None,
    children: Iterable[str] = (),
    funded_by: Iterable[str] = (),
    funds: Iterable[str] = (),
    funding_role: Iterable[str] = (),
    paid_by_orgs: Iterable[str] = (),
    pays_orgs: Iterable[str] = (),
    funder_orgs: Iterable[str] = (),
    pays_named: Iterable[str] = (),
    associated: Iterable[tuple[str, int]] = (),
    reporting_org: str = "XX-REPORTER",
    title: str | None = None,
    doc_id: str | None = None,
    updated: str | None = None,
) -> dict:
    """A hand-written Solr document. Each keyword declares links from this activity's side:

    parent/children: related-activity type 1/2 (hierarchy); funded_by/funds: transaction provider/receiver activity IDs
    (incoming funds / disbursements); funding_role: participating-org role 1 with activity-id; paid_by_orgs/pays_orgs:
    transaction provider/receiver org refs without activity IDs (O2/O1); funder_orgs: participating-org role 1 refs
    without activity IDs (O3); pays_named: receivers with a name but no ref; associated: (activity-id, role 2 or 3).
    doc_id/updated tell copies of one identifier apart.
    """
    incoming, outgoing = {"code": 1}, {"code": 3}
    related = ([{"ref": parent, "type": 1}] if parent else []) + [{"ref": c, "type": 2} for c in children]
    transactions = (
        [
            {"transaction-type": incoming, "provider-org": {"ref": "XX-FUNDER", "provider-activity-id": p}}
            for p in funded_by
        ]
        + [
            {"transaction-type": outgoing, "receiver-org": {"ref": "XX-RECIPIENT", "receiver-activity-id": r}}
            for r in funds
        ]
        + [
            {"transaction-type": incoming, "provider-org": {"ref": ref, "narrative": f"Org {ref}"}}
            for ref in paid_by_orgs
        ]
        + [{"transaction-type": outgoing, "receiver-org": {"ref": ref, "narrative": f"Org {ref}"}} for ref in pays_orgs]
        + [{"transaction-type": outgoing, "receiver-org": {"narrative": name}} for name in pays_named]
    )
    participating = (
        [{"role": 1, "activity-id": f} for f in funding_role]
        + [{"role": 1, "ref": ref, "narrative": f"Org {ref}"} for ref in funder_orgs]
        + [{"role": role, "activity-id": a} for a, role in associated]
    )
    doc: dict = {"iati-identifier": identifier, "reporting-org.ref": reporting_org, "hierarchy": 1}
    if title is not None:
        doc["title.narrative"] = [title]
    if doc_id is not None:
        doc["id"] = doc_id
    if updated is not None:
        doc["last-updated-datetime"] = updated
    for field, values in (
        ("json.related-activity", related),
        ("json.transaction", transactions),
        ("json.participating-org", participating),
    ):
        if values:
            doc[field] = [json.dumps(v) for v in values]
    return doc
