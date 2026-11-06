"""Pytest configuration and fixtures."""

import json
import re
from collections.abc import Callable, Collection, Iterable
from pathlib import Path

import pytest

from app.config import settings
from app.iati_cloud_dcma.links import ActivityDoc
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


class FakeLinkSource:
    """An in-memory LinkSource for traversal, tree and API tests.

    It answers like SolrLinkSource: fetch returns the known documents among the requested IDs, and mentioning
    returns, sorted by identifier, every document whose links name one of the IDs. Every call is logged in `calls`.
    """

    def __init__(self, docs: Iterable[ActivityDoc] = (), *, healthy: bool = True):
        self._docs = {doc.iati_identifier: doc for doc in docs}
        self.healthy = healthy
        self.calls: list[tuple[str, frozenset[str]]] = []

    @classmethod
    def from_fixtures(cls, *identifiers: str) -> "FakeLinkSource":
        return cls(ActivityDoc.from_solr(load_activity_fixture(identifier)) for identifier in identifiers)

    @property
    def query_count(self) -> int:
        return len(self.calls)

    def fetch(self, ids: Collection[str]) -> dict[str, ActivityDoc]:
        wanted = frozenset(ids)
        self.calls.append(("fetch", wanted))
        return {i: self._docs[i] for i in sorted(wanted) if i in self._docs}

    def mentioning(self, ids: Collection[str]) -> list[ActivityDoc]:
        wanted = frozenset(ids)
        self.calls.append(("mentioning", wanted))
        return [doc for _, doc in sorted(self._docs.items()) if doc.linked_activity_ids & wanted]

    def ping(self) -> bool:
        return self.healthy
