"""Tests for the routes in app.blueprint: GET /chain and GET /health-check."""

import logging
from http.client import RemoteDisconnected

import fakeredis
import pytest
import redis
from urllib3.exceptions import ReadTimeoutError

from app.cache import ResponseCache
from app.iati_cloud_dcma.budget import Budget, BudgetExhausted
from app.iati_cloud_dcma.service import Services
from app.iati_cloud_dcma.source import (
    CONNECTION_RETRIES,
    SolrLinkSource,
    SourceTimeout,
    SourceUnavailable,
    create_solr_client,
)
from app.iati_cloud_dcma.traversal import Limits
from app.models import ErrorResponse, GraphResponse, TreeResponse
from tests.conftest import (
    TEST_LIMITS,
    FakeClock,
    FakeLinkSource,
    load_chain_fixture,
    make_client,
    make_services,
    solr_reply,
)
from tests.conftest import raw_activity as act

CHAIN = "/iati-cloud-dcma/chain"
HEALTH = "/iati-cloud-dcma/health-check"
AAH = "GB-CHC-1047501-23SOELNF"


def _source() -> FakeLinkSource:
    """P funds X (the seed) and Y; X funds C and pays organisations by ref, by free text and by name."""
    return FakeLinkSource.from_raw(
        act("P", title="Programme"),
        act("X", funded_by=["P"], pays_orgs=["GB-CHC-1", "BRAC UK"], pays_named=["Some Partner"], title="Seed"),
        act("Y", funded_by=["P"]),
        act("C", funded_by=["X"]),
    )


@pytest.fixture
def source() -> FakeLinkSource:
    return _source()


@pytest.fixture
def api(source, auth_headers):
    """Call GET /chain with the API key: api(activity="X", mode="constrained") → response."""
    with make_client(make_services(source)) as client:

        def get(**params):
            return client.get(CHAIN, query_string=params, headers=auth_headers)

        get.client = client
        yield get


def _ids(body: dict) -> set[str]:
    return {node["id"] for node in body["nodes"]}


# --- 200: modes, formats, orgs --------------------------------------------------------------------------------------


def test_graph_response_in_full_mode_by_default(api):
    response = api(activity="X")
    assert response.status_code == 200
    body = response.get_json()
    GraphResponse.model_validate(body)
    assert (body["seed"], body["mode"], body["format"], body["orgs"]) == ("X", "full", "graph", "identified")
    assert (body["truncated"], body["truncation_reasons"]) == (False, [])
    assert _ids(body) == {"X", "P", "Y", "C", "org:GB-CHC-1"}
    assert body["stats"] == {
        "nodes": 5,
        "edges": 4,
        "associations": 0,
        "solr_queries": body["stats"]["solr_queries"],
        "duration_ms": body["stats"]["duration_ms"],
        "cached": False,
    }
    assert body["stats"]["solr_queries"] > 0


def test_constrained_mode_leaves_out_the_other_branch(api):
    body = api(activity="X", mode="constrained").get_json()
    assert _ids(body) == {"X", "P", "C", "org:GB-CHC-1"}


def test_node_and_edge_fields(api):
    body = api(activity="X", mode="constrained").get_json()
    nodes = {node["id"]: node for node in body["nodes"]}
    assert nodes["X"] == {
        "id": "X",
        "type": "activity",
        "iati_identifier": "X",
        "title": "Seed",
        "reporting_org": {"ref": "XX-REPORTER", "name": None},
        "hierarchy": 1,
        "activity_status": None,
        "published": True,
        "relation": "seed",
        "distance": 0,
        "expanded": True,
    }
    assert nodes["org:GB-CHC-1"] == {
        "id": "org:GB-CHC-1",
        "type": "organisation",
        "org_ref": "GB-CHC-1",
        "name": "Org GB-CHC-1",
        "identification": "identifier",
        "relation": "descendant",
        "distance": 1,
        "expanded": False,
    }
    assert {"from": "P", "to": "X", "kinds": ["transaction"], "declared_by": ["X"]} in body["edges"]
    assert body["associations"] == []


def test_unpublished_activity_node_has_null_metadata(auth_headers):
    with make_client(make_services(FakeLinkSource.from_raw(act("X", funded_by=["U"])))) as client:
        body = client.get(CHAIN, query_string={"activity": "X"}, headers=auth_headers).get_json()
    node = next(node for node in body["nodes"] if node["id"] == "U")
    assert (node["published"], node["title"], node["reporting_org"], node["expanded"]) == (False, None, None, False)


@pytest.mark.parametrize(
    ("orgs", "extra"),
    [("identified", set()), ("refs", {"org:BRAC UK"}), ("all", {"org:BRAC UK", "org-name:some partner"})],
)
def test_orgs_parameter_selects_organisation_nodes(api, orgs, extra):
    body = api(activity="X", mode="constrained", orgs=orgs).get_json()
    assert body["orgs"] == orgs
    assert _ids(body) == {"X", "P", "C", "org:GB-CHC-1"} | extra
    if orgs == "all":
        named = next(node for node in body["nodes"] if node["id"] == "org-name:some partner")
        assert (named["org_ref"], named["name"], named["identification"]) == (None, "Some Partner", "name-only")


def test_associations_are_returned(auth_headers):
    source = FakeLinkSource.from_raw(
        act("G", children=["P"]), act("P"), act("X", funded_by=["P"], associated=[("G", 3)])
    )
    with make_client(make_services(source)) as client:
        body = client.get(CHAIN, query_string={"activity": "X"}, headers=auth_headers).get_json()
    assert body["associations"] == [{"activities": ["G", "X"], "role": 3, "declared_by": ["X"]}]
    assert body["stats"]["associations"] == 1


def test_tree_format(api):
    response = api(activity="X", mode="constrained", format="tree")
    assert response.status_code == 200
    body = response.get_json()
    TreeResponse.model_validate(body)
    assert (body["format"], body["extra_edges"], body["associations"]) == ("tree", [], [])
    assert "nodes" not in body and "edges" not in body
    tree = body["tree"]
    assert (tree["id"], tree["relation"]) == ("X", "seed")
    assert [child["id"] for child in tree["upstream"]] == ["P"]
    assert [child["id"] for child in tree["downstream"]] == ["C", "org:GB-CHC-1"]
    assert tree["downstream"][1] | {"upstream": [], "downstream": []} == tree["downstream"][1]


def test_tree_and_graph_describe_the_same_chain(api):
    graph = api(activity="X").get_json()
    tree = api(activity="X", format="tree").get_json()

    def ids(node: dict) -> list[str]:
        return [node["id"], *(i for child in node["upstream"] + node["downstream"] for i in ids(child))]

    assert sorted(ids(tree["tree"])) == sorted(_ids(graph))
    assert {key: graph["stats"][key] for key in ("nodes", "edges")} == {
        key: tree["stats"][key] for key in ("nodes", "edges")
    }


def test_identifier_with_a_slash_and_in_another_letter_case(auth_headers):
    seed = "XM-OCHA-CBPF-AFG-22/3481/SA1/FSAC/INGO/22203"
    with make_client(make_services(FakeLinkSource.from_fixtures(seed))) as client:
        response = client.get(CHAIN, query_string={"activity": f" {seed.lower()} "}, headers=auth_headers)
    assert response.status_code == 200
    assert response.get_json()["seed"] == seed


def test_worked_example_through_the_api(auth_headers):
    source = FakeLinkSource.from_raw(*load_chain_fixture("worked_example")["docs"])
    with make_client(make_services(source)) as client:
        body = client.get(CHAIN, query_string={"activity": AAH, "mode": "constrained"}, headers=auth_headers).get_json()
    assert _ids(body) == {AAH, "GB-GOV-1-300978-408", "GB-GOV-1-300978", "org:US-EIN-13-3327220"}


# --- Limits -----------------------------------------------------------------------------------------------------------


def test_limits_can_be_lowered(api):
    body = api(activity="X", max_depth="1", max_nodes="2").get_json()
    assert body["stats"]["nodes"] == 2
    assert body["truncated"] is True
    assert "max_nodes" in body["truncation_reasons"]


def test_limits_above_the_ceiling_are_capped(auth_headers):
    services = make_services(_source(), limits=Limits(max_depth=10, max_nodes=2))
    with make_client(services) as client:
        body = client.get(CHAIN, query_string={"activity": "X", "max_nodes": "9999"}, headers=auth_headers).get_json()
    assert body["stats"]["nodes"] == 2
    assert body["truncation_reasons"] == ["max_nodes"]


# --- Errors -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({}, "activity"),
        ({"activity": "   "}, "activity"),
        ({"activity": "A" * 256}, "activity"),
        ({"activity": "A\x00B"}, "activity"),
        ({"activity": "A\nB"}, "activity"),
        ({"activity": "X", "mode": "sideways"}, "mode"),
        ({"activity": "X", "format": "xml"}, "format"),
        ({"activity": "X", "orgs": "everything"}, "orgs"),
        ({"activity": "X", "max_depth": "0"}, "max_depth"),
        ({"activity": "X", "max_nodes": "-5"}, "max_nodes"),
        ({"activity": "X", "max_nodes": "many"}, "max_nodes"),
        ({"activity": "X", "max_depth": "1.5"}, "max_depth"),
    ],
)
def test_invalid_query_is_a_400_naming_the_parameter(api, source, params, field):
    response = api(**params)
    assert response.status_code == 400
    body = response.get_json()
    ErrorResponse.model_validate(body)
    assert body["error"].startswith("Invalid query. ")
    assert field in body["error"]
    assert source.calls == []


def test_validation_message_does_not_echo_the_input(api):
    body = api(activity="X", mode="<script>alert(1)</script>").get_json()
    assert "script" not in body["error"]


def test_activity_of_255_characters_is_accepted(api):
    assert api(activity="A" * 255).status_code == 404


def test_unknown_parameters_are_ignored(api):
    assert api(activity="X", colour="blue").status_code == 200


def test_chain_requires_the_api_key(api):
    response = api.client.get(CHAIN, query_string={"activity": "X"})
    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}


def test_unknown_seed_is_a_404(api):
    response = api(activity="NOWHERE")
    assert response.status_code == 404
    assert "not published" in response.get_json()["error"]


@pytest.mark.parametrize(
    ("raised", "status", "message"),
    [
        (SourceUnavailable("The data source is unavailable."), 502, "The data source is unavailable."),
        (SourceTimeout("The data source timed out."), 504, "The data source timed out."),
        (BudgetExhausted("ran out"), 504, "The data source timed out."),
    ],
)
def test_source_errors_map_to_gateway_errors_without_internals(mocker, auth_headers, raised, status, message):
    source = _source()
    mocker.patch.object(source, "fetch", side_effect=raised)
    with make_client(make_services(source)) as client:
        response = client.get(CHAIN, query_string={"activity": "X"}, headers=auth_headers)
    assert response.status_code == status
    assert response.get_json() == {"error": message}


# --- Connection retries, through the real Solr client (spec §7, Resilience) ------------------------------------------


def _solr_services() -> Services:
    """Services around the real SolrLinkSource and client; tests replace only the socket exchange (solr_wire)."""
    client = create_solr_client("https://fcdo.iati.cloud/search/activity/", timeout=10)
    return Services(
        new_source=lambda: SolrLinkSource(client=client, batch_size=100),
        cache=ResponseCache(fakeredis.FakeRedis(decode_responses=True), ttl=60),
        limits=TEST_LIMITS,
        request_timeout=30,
    )


def _solr_chain(auth_headers):
    with make_client(_solr_services()) as client:
        return client.get(CHAIN, query_string={"activity": "X"}, headers=auth_headers)


def _seed_or_nothing(_pool, _conn, _method, url, *_args, **_kwargs):
    """The seed X for a fetch by identifier; no activities for the reverse lookups."""
    return solr_reply([act("X")] if "q=iati-identifier" in url else [])


def test_disconnect_once_then_success_is_a_200(solr_wire, auth_headers):
    disconnects = iter([RemoteDisconnected("Remote end closed connection without response")])

    def answer(*args, **kwargs):
        if (error := next(disconnects, None)) is not None:
            raise error
        return _seed_or_nothing(*args, **kwargs)

    solr_wire.side_effect = answer
    response = _solr_chain(auth_headers)
    assert response.status_code == 200
    body = response.get_json()
    assert _ids(body) == {"X"}
    # Two queries (fetch the seed, look up what cites it); the retried attempt is not counted.
    assert body["stats"]["solr_queries"] == 2
    assert solr_wire.call_count == 3


def test_disconnect_on_every_attempt_is_a_502(solr_wire, auth_headers):
    solr_wire.side_effect = RemoteDisconnected("Remote end closed connection without response")
    response = _solr_chain(auth_headers)
    assert response.status_code == 502
    assert response.get_json() == {"error": "The data source is unavailable."}
    assert solr_wire.call_count == CONNECTION_RETRIES + 1


def test_read_timeout_is_not_retried_and_is_a_504(solr_wire, auth_headers):
    def read_timeout(pool, _conn, _method, url, *_args, **_kwargs):
        raise ReadTimeoutError(pool, url, "Read timed out. (read timeout=10)")

    solr_wire.side_effect = read_timeout
    response = _solr_chain(auth_headers)
    assert response.status_code == 504
    assert response.get_json() == {"error": "The data source timed out."}
    assert solr_wire.call_count == 1


def test_http_5xx_from_solr_is_not_retried_and_is_a_502(solr_wire, auth_headers):
    solr_wire.return_value = solr_reply(status=503)
    assert _solr_chain(auth_headers).status_code == 502
    assert solr_wire.call_count == 1


# --- Cache ------------------------------------------------------------------------------------------------------------


def test_second_identical_request_is_served_from_the_cache(api, source):
    first = api(activity="X").get_json()
    source.reset_calls()
    second = api(activity="X").get_json()
    assert source.calls == []
    assert second["stats"] | {"duration_ms": 0} == first["stats"] | {
        "cached": True,
        "solr_queries": 0,
        "duration_ms": 0,
    }
    assert {k: v for k, v in second.items() if k != "stats"} == {k: v for k, v in first.items() if k != "stats"}


def test_equivalent_requests_share_a_cache_entry(api, source):
    api(activity="X", max_nodes="9999")
    source.reset_calls()
    assert api(activity=" x ").get_json()["stats"]["cached"] is True
    assert source.calls == []


@pytest.mark.parametrize("change", [{"mode": "constrained"}, {"format": "tree"}, {"orgs": "all"}, {"max_nodes": "3"}])
def test_different_requests_do_not_share_a_cache_entry(api, change):
    api(activity="X")
    assert api(activity="X", **change).get_json()["stats"]["cached"] is False


def test_result_truncated_by_a_limit_is_cached(api):
    assert api(activity="X", max_nodes="2").get_json()["truncated"] is True
    assert api(activity="X", max_nodes="2").get_json()["stats"]["cached"] is True


def test_result_truncated_by_the_time_budget_is_not_cached(mocker, auth_headers):
    # Every call to the source takes one second of a 2.5-second budget, on every request.
    source = _source()
    clock = FakeClock()

    def one_second_per_call() -> None:
        clock.now += 1

    source.on_call = one_second_per_call
    mocker.patch(
        "app.iati_cloud_dcma.service.Budget.of_seconds", side_effect=lambda seconds: Budget(clock.now + seconds, clock)
    )
    with make_client(make_services(source, request_timeout=2.5)) as client:
        for _ in range(2):
            body = client.get(CHAIN, query_string={"activity": "X"}, headers=auth_headers).get_json()
            assert body["truncation_reasons"] == ["time_budget"]
            assert body["stats"]["cached"] is False


def test_no_time_at_all_is_a_504(auth_headers):
    with make_client(make_services(_source(), request_timeout=0.0)) as client:
        assert client.get(CHAIN, query_string={"activity": "X"}, headers=auth_headers).status_code == 504


def test_errors_are_not_cached(api, source):
    assert api(activity="NOWHERE").status_code == 404
    source.reset_calls()
    assert api(activity="NOWHERE").status_code == 404
    assert source.calls != []


def test_cache_outage_does_not_fail_the_request(mocker, auth_headers, caplog):
    broken = mocker.create_autospec(redis.Redis, instance=True)
    broken.get.side_effect = broken.set.side_effect = redis.ConnectionError("connection refused")
    with make_client(make_services(_source(), redis_client=broken)) as client, caplog.at_level(logging.WARNING):
        response = client.get(CHAIN, query_string={"activity": "X"}, headers=auth_headers)
    assert response.status_code == 200
    assert response.get_json()["stats"]["cached"] is False
    assert len([r for r in caplog.records if r.name == "app.cache"]) == 2


# --- Logging ----------------------------------------------------------------------------------------------------------


def test_one_info_line_per_request(api, caplog):
    with caplog.at_level(logging.INFO, logger="app.iati_cloud_dcma.service"):
        api(activity="X", mode="constrained", orgs="refs", max_nodes="2")
        api(activity="X", mode="constrained", orgs="refs", max_nodes="2")
    first, second = [r.getMessage() for r in caplog.records if r.name == "app.iati_cloud_dcma.service"]
    for part in (
        "seed='X'",
        "mode=constrained",
        "format=graph",
        "orgs=refs",
        "nodes=2",
        "truncation_reasons=max_nodes",
    ):
        assert part in first
    assert "cached=False" in first and "cached=True" in second
    assert "queries=0" in second


# --- Health check -----------------------------------------------------------------------------------------------------


def _health(source: FakeLinkSource, redis_client=None):
    with make_client(make_services(source, redis_client=redis_client)) as client:
        return client.get(HEALTH)


def test_health_check_healthy_needs_no_key():
    response = _health(FakeLinkSource())
    assert response.status_code == 200
    body = response.get_json()
    assert (body["status"], body["dependencies"]) == ("healthy", {"solr": "ok", "cache": "ok"})


def test_health_check_degraded_without_the_cache(mocker):
    broken = mocker.create_autospec(redis.Redis, instance=True)
    broken.ping.side_effect = redis.ConnectionError("connection refused")
    response = _health(FakeLinkSource(), redis_client=broken)
    assert response.status_code == 200
    assert response.get_json()["status"] == "degraded"
    assert response.get_json()["dependencies"] == {"solr": "ok", "cache": "unavailable"}


def test_health_check_unhealthy_without_solr():
    response = _health(FakeLinkSource(healthy=False))
    assert response.status_code == 503
    body = response.get_json()
    assert (body["status"], body["dependencies"]) == ("unhealthy", {"solr": "unavailable", "cache": "ok"})
    assert "timestamp" in body


def test_result_for_an_unpublished_seed_is_not_cached(auth_headers):
    # An unpublished seed is only found under the spelling typed, but all spellings share one cache key.
    source = FakeLinkSource.from_raw(act("A", funds=["U-Ref"]))
    with make_client(make_services(source)) as client:
        for _ in range(2):
            body = client.get(CHAIN, query_string={"activity": "U-Ref"}, headers=auth_headers).get_json()
            assert (body["seed"], body["stats"]["cached"]) == ("U-Ref", False)
