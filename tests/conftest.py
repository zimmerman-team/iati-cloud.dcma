"""Pytest configuration and fixtures."""

import json
import re
from collections.abc import Callable, Collection, Iterable
from pathlib import Path

import pytest

from app.config import settings
from app.iati_cloud_dcma.budget import UNLIMITED, Budget
from app.iati_cloud_dcma.links import ActivityDoc
from app.iati_cloud_dcma.source import Mentions
from app.main import create_app

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "activities"


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def auth_headers():
    return {"Authorization": settings.secret_key}


def load_activity_fixture(identifier: str) -> dict:
    """Load a raw Solr activity document saved by scripts/fetch_fixture.py."""
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", identifier) + ".json"
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

    It answers like SolrLinkSource: fetch returns the known documents among the requested IDs, and mentioning
    returns, sorted by identifier, every document whose links name one of the IDs. It honours the budget (checked
    before each call) and the mention limit. Every call is logged in `calls`; `on_call` runs after each one, so a test
    can move a FakeClock to simulate time spent in Solr.
    """

    def __init__(
        self, docs: Iterable[ActivityDoc] = (), *, healthy: bool = True, on_call: Callable[[], None] | None = None
    ):
        self._docs = {doc.iati_identifier: doc for doc in docs}
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

    def fetch(self, ids: Collection[str], budget: Budget = UNLIMITED) -> dict[str, ActivityDoc]:
        budget.check()
        wanted = self._log("fetch", ids)
        return {i: self._docs[i] for i in sorted(wanted) if i in self._docs}

    def mentioning(self, ids: Collection[str], budget: Budget = UNLIMITED, limit: int | None = None) -> Mentions:
        if budget.expired():
            return Mentions(docs=(), complete=False)
        wanted = self._log("mentioning", ids)
        docs = tuple(doc for _, doc in sorted(self._docs.items()) if doc.linked_activity_ids & wanted)
        if limit is not None and len(docs) > limit:
            return Mentions(docs=docs[:limit], complete=False)
        return Mentions(docs=docs, complete=True)

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
    associated: Iterable[tuple[str, int]] = (),
    reporting_org: str = "XX-REPORTER",
    title: str | None = None,
) -> dict:
    """A hand-written Solr document. Each keyword declares links from this activity's side:

    parent/children: related-activity type 1/2 (hierarchy); funded_by/funds: transaction provider/receiver activity IDs;
    funding_role: participating-org role 1 with activity-id; paid_by_orgs/pays_orgs: transaction provider/receiver org
    refs without activity IDs (O2/O1); associated: (activity-id, role 2 or 3).
    """
    related = ([{"ref": parent, "type": 1}] if parent else []) + [{"ref": c, "type": 2} for c in children]
    transactions = (
        [{"provider-org": {"ref": "XX-FUNDER", "provider-activity-id": p}} for p in funded_by]
        + [{"receiver-org": {"ref": "XX-RECIPIENT", "receiver-activity-id": r}} for r in funds]
        + [{"provider-org": {"ref": ref, "narrative": f"Org {ref}"}} for ref in paid_by_orgs]
        + [{"receiver-org": {"ref": ref, "narrative": f"Org {ref}"}} for ref in pays_orgs]
    )
    participating = [{"role": 1, "activity-id": f} for f in funding_role] + [
        {"role": role, "activity-id": a} for a, role in associated
    ]
    doc: dict = {"iati-identifier": identifier, "reporting-org.ref": reporting_org, "hierarchy": 1}
    if title is not None:
        doc["title.narrative"] = [title]
    for field, values in (
        ("json.related-activity", related),
        ("json.transaction", transactions),
        ("json.participating-org", participating),
    ):
        if values:
            doc[field] = [json.dumps(v) for v in values]
    return doc
