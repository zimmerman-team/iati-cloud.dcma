"""Tests for app.iati_cloud_dcma.source: the Solr link source (spec §5.4, §5.5, §8). No test touches the network."""

import json
import logging

import pysolr
import pytest
import requests

from app.iati_cloud_dcma.links import SOLR_FIELDS, ActivityDoc
from app.iati_cloud_dcma.source import (
    MENTION_MIN_ROWS,
    MENTION_ROWS_PER_ID,
    LinkSource,
    SolrLinkSource,
    SourceError,
    SourceTimeout,
    SourceUnavailable,
    create_solr_client,
    quote_id,
)
from tests.conftest import FakeLinkSource, load_activity_fixture

LOGGER = "app.iati_cloud_dcma.source"
FL = ",".join(SOLR_FIELDS)
SORT = "iati-identifier asc"
PROXY_URL = "https://fcdo.iati.cloud/search/activity/"

PROGRAMME = "GB-GOV-1-300978"
PROJECT_408 = "GB-GOV-1-300978-408"
AAH = "GB-CHC-1047501-23SOELNF"
TROCAIRE = "IE-CHY-20009601-FCDO24SOM"
NRC = "NO-BRC-977538319-SOFM2343"


def _results(docs, hits=None) -> pysolr.Results:
    docs = list(docs)
    return pysolr.Results({"response": {"docs": docs, "numFound": len(docs) if hits is None else hits}})


def _raw(identifier: str, **fields) -> dict:
    return {"iati-identifier": identifier, **fields}


def _child_of(identifier: str, parent: str) -> dict:
    return _raw(identifier, **{"json.related-activity": [json.dumps({"ref": parent, "type": 1})]})


@pytest.fixture
def solr(mocker):
    client = mocker.create_autospec(pysolr.Solr, instance=True)
    client.search.return_value = _results([])
    return client


def _source(solr, batch_size=100) -> SolrLinkSource:
    return SolrLinkSource(client=solr, batch_size=batch_size)


def _queries(solr) -> list[tuple[str, dict]]:
    """(q, other params) for every search call made on the mocked client."""
    return [(call.args[0], call.kwargs) for call in solr.search.call_args_list]


def _mention_query(*ids: str) -> str:
    values = " OR ".join(f'"{i}"' for i in ids)
    fields = [
        "transaction_provider_org_provider_activity_id",
        "transaction_receiver_org_receiver_activity_id",
        "related_activity_ref",
        "participating-org.activity-id",
    ]
    return " OR ".join(f"{field}:({values})" for field in fields)


def _raise_wrapped_timeout(*_args, **_kwargs):
    """What pysolr does on a requests timeout: raise SolrError while handling the Timeout."""
    try:
        raise requests.exceptions.Timeout("read timed out")
    except requests.exceptions.Timeout:
        raise pysolr.SolrError("Connection to server 'https://solr.internal/select/?q=x' timed out") from None


# --- quote_id -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "quoted"),
    [
        ("GB-GOV-1-300978", '"GB-GOV-1-300978"'),
        ("XM-OCHA-CBPF-AFG-22/3481/SA1/FSAC/INGO/22203", '"XM-OCHA-CBPF-AFG-22/3481/SA1/FSAC/INGO/22203"'),
        ('A"B', '"A\\"B"'),
        ("A\\B", '"A\\\\B"'),
        ('\\"', '"\\\\\\""'),
        ("A:B (C) [D] * ? OR", '"A:B (C) [D] * ? OR"'),
    ],
)
def test_quote_id(identifier, quoted):
    assert quote_id(identifier) == quoted


# --- fetch --------------------------------------------------------------------------------------------------------


def test_fetch_nothing_makes_no_query(solr):
    assert _source(solr).fetch([]) == {}
    assert _source(solr).fetch(["", "  "]) == {}
    solr.search.assert_not_called()


def test_fetch_queries_quoted_identifiers_with_needed_fields_only(solr):
    _source(solr).fetch(["B-2", "A-1"])
    assert _queries(solr) == [('iati-identifier:("A-1" OR "B-2")', {"fl": FL, "rows": 2, "start": 0, "sort": SORT})]


def test_fetch_escapes_identifiers(solr):
    _source(solr).fetch(['we"ird\\id', "XM-OCHA-CBPF-AFG-22/3481/SA1"])
    assert _queries(solr)[0][0] == 'iati-identifier:("XM-OCHA-CBPF-AFG-22/3481/SA1" OR "we\\"ird\\\\id")'


def test_fetch_trims_and_deduplicates_input(solr):
    _source(solr).fetch([" A-1 ", "A-1", "", "B-2"])
    assert [q for q, _ in _queries(solr)] == ['iati-identifier:("A-1" OR "B-2")']


def test_fetch_batches_by_batch_size(solr):
    _source(solr, batch_size=2).fetch(["E", "D", "C", "B", "A"])
    assert _queries(solr) == [
        ('iati-identifier:("A" OR "B")', {"fl": FL, "rows": 2, "start": 0, "sort": SORT}),
        ('iati-identifier:("C" OR "D")', {"fl": FL, "rows": 2, "start": 0, "sort": SORT}),
        ('iati-identifier:("E")', {"fl": FL, "rows": 1, "start": 0, "sort": SORT}),
    ]


def test_fetch_returns_activity_docs_by_identifier(solr):
    raw = load_activity_fixture(AAH)
    solr.search.return_value = _results([raw])
    assert _source(solr).fetch([AAH]) == {AAH: ActivityDoc.from_solr(raw)}


def test_fetch_pages_when_solr_has_more_documents_than_rows(solr):
    solr.search.side_effect = [_results([_raw("A")], hits=3), _results([_raw("A"), _raw("B")], hits=3)]
    docs = _source(solr).fetch(["A", "B"])
    assert set(docs) == {"A", "B"}
    assert [params["start"] for _, params in _queries(solr)] == [0, 2]


def test_fetch_stops_paging_on_empty_page(solr):
    solr.search.side_effect = [_results([], hits=50)]
    assert _source(solr).fetch(["A"]) == {}
    assert solr.search.call_count == 1


def test_fetch_keeps_first_of_duplicate_identifiers(solr):
    first = _child_of("A", "P-1")
    solr.search.return_value = _results([first, _child_of("A", "P-2")])
    assert _source(solr).fetch(["A"]) == {"A": ActivityDoc.from_solr(first)}


def test_fetch_ignores_documents_that_were_not_requested(solr):
    solr.search.return_value = _results([_raw("A"), _raw("A-EXTRA")])
    assert set(_source(solr).fetch(["A"])) == {"A"}


def test_fetch_matches_padded_identifier_after_trimming(solr):
    solr.search.return_value = _results([_raw(" A ")])
    assert set(_source(solr).fetch(["A"])) == {"A"}


def test_fetch_skips_invalid_documents_and_logs(solr, caplog):
    solr.search.return_value = _results([{"title.narrative": ["No identifier"]}, _raw("A")])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert set(_source(solr).fetch(["A", "B"])) == {"A"}
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]


# --- mentioning ---------------------------------------------------------------------------------------------------


def test_mentioning_nothing_makes_no_query(solr):
    assert _source(solr).mentioning([" "]) == []
    solr.search.assert_not_called()


def test_mention_query_matches_spec(solr):
    _source(solr).mentioning(["B-2", "A-1"])
    assert _queries(solr) == [
        (_mention_query("A-1", "B-2"), {"fl": FL, "rows": MENTION_MIN_ROWS, "start": 0, "sort": SORT})
    ]


@pytest.mark.parametrize(
    ("batch", "rows"),
    [
        (1, MENTION_MIN_ROWS),
        (MENTION_MIN_ROWS // MENTION_ROWS_PER_ID, MENTION_MIN_ROWS),
        (100, 100 * MENTION_ROWS_PER_ID),
    ],
)
def test_mention_page_size_grows_with_the_batch_above_a_floor(solr, batch, rows):
    _source(solr, batch_size=250).mentioning([f"ID-{n:03d}" for n in range(batch)])
    assert _queries(solr)[0][1]["rows"] == rows


def test_mentioning_batches_by_batch_size(solr):
    _source(solr, batch_size=2).mentioning(["C", "B", "A"])
    assert [q for q, _ in _queries(solr)] == [_mention_query("A", "B"), _mention_query("C")]


def test_mentioning_drops_tokenised_false_positives(solr):
    # participating-org.activity-id is tokenised: a query for the programme also matches the AAH activity,
    # which only cites the programme's project -408 (seen live on 2026-11-06).
    solr.search.return_value = _results([load_activity_fixture(AAH), load_activity_fixture(PROJECT_408)])
    docs = _source(solr).mentioning([PROGRAMME])
    assert [doc.iati_identifier for doc in docs] == [PROJECT_408]


def test_mentioning_keeps_links_declared_at_either_end_and_associations(solr):
    raws = [load_activity_fixture(i) for i in (AAH, PROGRAMME, TROCAIRE)]
    solr.search.return_value = _results(raws)
    assert [d.iati_identifier for d in _source(solr).mentioning([PROJECT_408])] == [AAH, PROGRAMME, TROCAIRE]
    solr.search.return_value = _results([load_activity_fixture(TROCAIRE)])
    assert [d.iati_identifier for d in _source(solr).mentioning(["GB-CHC-1047501-24SOELN2F"])] == [TROCAIRE]


def test_mentioning_returns_each_document_once_across_batches(solr):
    both = _raw("BOTH", **{"json.related-activity": [json.dumps({"ref": r, "type": 1}) for r in ("A", "B")]})
    solr.search.side_effect = [_results([both]), _results([both])]
    docs = _source(solr, batch_size=1).mentioning(["A", "B"])
    assert [d.iati_identifier for d in docs] == ["BOTH"]


def test_mentioning_pages_through_all_matches(solr):
    page_size = MENTION_MIN_ROWS
    pages = [[_child_of(f"C-{p}-{n}", "A") for n in range(page_size)] for p in range(2)] + [[_child_of("C-last", "A")]]
    hits = 2 * page_size + 1
    solr.search.side_effect = [_results(page, hits=hits) for page in pages]
    docs = _source(solr).mentioning(["A"])
    assert len(docs) == hits
    assert [params["start"] for _, params in _queries(solr)] == [0, page_size, 2 * page_size]
    assert all(params["rows"] == page_size for _, params in _queries(solr))


def test_mentioning_skips_invalid_documents_and_logs(solr, caplog):
    solr.search.return_value = _results([{"iati-identifier": ""}, _child_of("C", "A")])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert [d.iati_identifier for d in _source(solr).mentioning(["A"])] == ["C"]
    assert len([r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]) == 1


# --- Errors -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["fetch", "mentioning"])
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (pysolr.SolrError("Solr responded with an error (HTTP 500): https://solr.internal/select"), SourceUnavailable),
        (requests.exceptions.ConnectionError("https://solr.internal refused"), SourceUnavailable),
        (requests.exceptions.ChunkedEncodingError("broken"), SourceUnavailable),
        (ValueError("Expecting value: line 1 column 1"), SourceUnavailable),
        (requests.exceptions.ReadTimeout("read timed out"), SourceTimeout),
    ],
)
def test_solr_failures_become_source_errors(solr, caplog, method, raised, expected):
    solr.search.side_effect = raised
    with caplog.at_level(logging.ERROR, logger=LOGGER), pytest.raises(expected) as excinfo:
        getattr(_source(solr), method)(["A"])
    assert "solr.internal" not in str(excinfo.value)
    assert excinfo.value.__cause__ is raised
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.ERROR]


@pytest.mark.parametrize("method", ["fetch", "mentioning"])
def test_timeout_wrapped_by_pysolr_becomes_source_timeout(solr, method):
    solr.search.side_effect = _raise_wrapped_timeout
    with pytest.raises(SourceTimeout):
        getattr(_source(solr), method)(["A"])


def test_source_errors_share_a_base_class():
    assert issubclass(SourceUnavailable, SourceError)
    assert issubclass(SourceTimeout, SourceError)


# --- query_count and ping -----------------------------------------------------------------------------------------


def test_query_count_counts_every_solr_query_including_pages(solr):
    source = _source(solr, batch_size=1)
    assert source.query_count == 0
    more_than_one_page = MENTION_MIN_ROWS + 1
    solr.search.side_effect = [_results([]), _results([]), _results([_raw("C")], hits=more_than_one_page), _results([])]
    source.fetch(["A", "B"])
    source.mentioning(["C"])
    assert source.query_count == 4


def test_failed_query_is_counted(solr):
    source = _source(solr)
    solr.search.side_effect = pysolr.SolrError("boom")
    with pytest.raises(SourceUnavailable):
        source.fetch(["A"])
    assert source.query_count == 1


def test_ping_runs_a_zero_row_query_and_is_not_counted(solr):
    source = _source(solr)
    assert source.ping() is True
    assert _queries(solr) == [("*:*", {"rows": 0})]
    assert source.query_count == 0


@pytest.mark.parametrize("raised", [pysolr.SolrError("down"), requests.exceptions.ConnectTimeout("slow"), ValueError()])
def test_ping_reports_false_when_solr_fails(solr, caplog, raised):
    solr.search.side_effect = raised
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert _source(solr).ping() is False
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]


def test_solr_link_source_is_a_link_source(solr):
    assert isinstance(_source(solr), LinkSource)


# --- create_solr_client and the real pysolr request path ---------------------------------------------------------


@pytest.mark.parametrize(
    ("solr_url", "base", "handler"),
    [
        (PROXY_URL, "https://fcdo.iati.cloud/search", "activity"),
        ("https://fcdo.iati.cloud/search/activity", "https://fcdo.iati.cloud/search", "activity"),
        ("http://solr:8983/solr/activity/select", "http://solr:8983/solr/activity", "select"),
    ],
)
def test_create_solr_client_splits_endpoint_into_base_and_handler(solr_url, base, handler):
    client = create_solr_client(solr_url, timeout=7)
    assert (client.url, client.search_handler, client.timeout) == (base, handler, 7)


@pytest.mark.parametrize(
    "solr_url", ["https://fcdo.iati.cloud", "https://fcdo.iati.cloud/", "https://fcdo.iati.cloud//"]
)
def test_create_solr_client_needs_an_endpoint_path(solr_url):
    with pytest.raises(ValueError, match="path"):
        create_solr_client(solr_url, timeout=7)


def _http_response(mocker, status: int, payload: dict | str):
    """A stand-in for requests.Response with what pysolr reads: status_code, content, headers and json()."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    response = mocker.Mock(status_code=status, content=body.encode(), headers={"content-type": "application/json"})
    response.json.side_effect = lambda: json.loads(body)
    return response


@pytest.fixture
def session(mocker):
    session = mocker.Mock()
    ok = _http_response(mocker, 200, {"response": {"docs": [], "numFound": 0}})
    session.get.return_value = ok
    session.post.return_value = ok
    return session


def _real_source(session, batch_size=100) -> SolrLinkSource:
    client = create_solr_client(PROXY_URL, timeout=10)
    client.session = session
    return SolrLinkSource(client=client, batch_size=batch_size)


def test_short_query_is_a_get_on_the_proxy_endpoint(session):
    _real_source(session).fetch([PROGRAMME])
    url = session.get.call_args.args[0]
    assert url.startswith(f"{PROXY_URL}?")
    assert "select" not in url
    assert session.get.call_args.kwargs["timeout"] == 10


def test_long_query_is_a_post_to_the_proxy_endpoint(session):
    _real_source(session).mentioning([f"{PROGRAMME}-{n}" for n in range(100)])
    session.get.assert_not_called()
    assert session.post.call_args.args[0] == PROXY_URL


def test_real_pysolr_timeout_becomes_source_timeout(session):
    session.get.side_effect = requests.exceptions.ReadTimeout("read timed out")
    with pytest.raises(SourceTimeout):
        _real_source(session).fetch([PROGRAMME])


def test_real_pysolr_http_error_becomes_source_unavailable(mocker, session):
    session.get.return_value = _http_response(mocker, 500, {"error": {"msg": "boom"}})
    with pytest.raises(SourceUnavailable):
        _real_source(session).fetch([PROGRAMME])


def test_non_json_response_becomes_source_unavailable(mocker, session):
    session.get.return_value = _http_response(mocker, 200, "<p>Searching for Solr?</p>")
    with pytest.raises(SourceUnavailable):
        _real_source(session).fetch([PROGRAMME])


# --- FakeLinkSource (tests/conftest.py), used by the traversal, tree and API tests --------------------------------


def test_fake_link_source_from_fixtures():
    fake = FakeLinkSource.from_fixtures(PROGRAMME, PROJECT_408, AAH, TROCAIRE, NRC)
    assert isinstance(fake, LinkSource)
    assert set(fake.fetch([PROJECT_408, "UNPUBLISHED"])) == {PROJECT_408}
    assert [d.iati_identifier for d in fake.mentioning([PROJECT_408])] == [AAH, PROGRAMME, TROCAIRE]
    assert fake.calls == [("fetch", frozenset({PROJECT_408, "UNPUBLISHED"})), ("mentioning", frozenset({PROJECT_408}))]
    assert fake.query_count == 2
    assert fake.ping() is True
