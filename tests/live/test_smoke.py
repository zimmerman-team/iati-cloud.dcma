"""Smoke tests against the live data source. Skipped by default.

Run with: uv run pytest -m live --no-cov

They assert inclusion only, because the published data changes over time.
"""

import pytest

from app.config import settings
from app.iati_cloud_dcma.source import SolrLinkSource, create_solr_client
from app.iati_cloud_dcma.traversal import Limits, build_chain

pytestmark = pytest.mark.live

AAH = "GB-CHC-1047501-23SOELNF"
PROGRAMME = "GB-GOV-1-300978"
PROJECTS = {f"{PROGRAMME}-{n}" for n in [*range(101, 113), *range(401, 414)]}
TROCAIRE = {f"IE-CHY-20009601-FCDO{suffix}" for suffix in ("23SOM", "24SOM", "24ESOM", "26SOM")}
NRC = "NO-BRC-977538319-SOFM2343"
CONSTRAINED = {f"{PROGRAMME}-408", PROGRAMME, "org:US-EIN-13-3327220"}


@pytest.fixture(scope="module")
def client():
    return create_solr_client(settings.solr_url, settings.solr_timeout)


def _chain(client, seed: str, mode: str, **kwargs):
    source = SolrLinkSource(client=client, batch_size=settings.id_batch_size)
    return build_chain(source, seed, mode, Limits(settings.max_depth, settings.max_nodes), **kwargs)


def test_source_answers(client):
    assert SolrLinkSource(client=client, batch_size=settings.id_batch_size).ping() is True


def test_constrained_chain_of_the_ngo_activity(client):
    chain = _chain(client, AAH, "constrained")
    assert CONSTRAINED <= chain.node_ids
    assert not chain.node_ids & ({f"{PROGRAMME}-405", NRC} | TROCAIRE)
    assert chain.truncated is False


def test_full_chain_of_the_ngo_activity(client):
    chain = _chain(client, AAH, "full")
    assert CONSTRAINED | PROJECTS | TROCAIRE | {NRC} <= chain.node_ids


def test_constrained_chain_of_the_programme(client):
    chain = _chain(client, PROGRAMME, "constrained")
    assert PROJECTS | TROCAIRE | {AAH, NRC} <= chain.node_ids


def test_seed_in_another_letter_case(client):
    chain = _chain(client, AAH.lower(), "constrained")
    assert chain.seed == AAH
    assert CONSTRAINED <= chain.node_ids


def test_identifier_with_hundreds_of_documents_is_one_node(client):
    source = SolrLinkSource(client=client, batch_size=settings.id_batch_size)
    chain = build_chain(source, "XM-DAC-903-SII-45517", "constrained", Limits(10, 500))
    assert [node.id.upper() for node in chain.nodes if node.relation == "seed"] == ["XM-DAC-903-SII-45517"]
    assert source.query_count < 20


def test_heavily_cited_funder_is_cut_off_not_paged_through(client):
    chain = _chain(client, "XM-OCHA-CERF", "constrained")
    assert "mention_limit" in chain.truncation_reasons
    assert len(chain.nodes) <= settings.max_nodes
