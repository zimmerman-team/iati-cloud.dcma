"""Tests for app.iati_cloud_dcma.traversal: walks, modes, limits, relations and distances."""

import pytest

from app.iati_cloud_dcma.budget import Budget, BudgetExhausted
from app.iati_cloud_dcma.chain import ActivityNode, Chain, ChainAssociation, ChainEdge, OrganisationNode
from app.iati_cloud_dcma.traversal import Limits, SeedNotFound, build_chain
from tests.conftest import FakeClock, FakeLinkSource, load_activity_copies, load_chain_fixture
from tests.conftest import raw_activity as act

LIMITS = Limits(max_depth=10, max_nodes=500)


def _chain(source: FakeLinkSource, seed: str, mode: str = "constrained", limits: Limits = LIMITS, **kwargs) -> Chain:
    return build_chain(source, seed, mode, limits, **kwargs)


def _nodes(chain: Chain) -> dict:
    return {node.id: node for node in chain.nodes}


def _relations(chain: Chain) -> dict[str, str]:
    return {node.id: node.relation for node in chain.nodes}


def _distances(chain: Chain) -> dict[str, int | None]:
    return {node.id: node.distance for node in chain.nodes}


def _expanded(chain: Chain) -> dict[str, bool]:
    return {node.id: node.expanded for node in chain.nodes}


def _edges(chain: Chain) -> set[tuple[str, str]]:
    return {(edge.upstream, edge.downstream) for edge in chain.edges}


def _assert_each_id_queried_once(source: FakeLinkSource) -> None:
    fetched, mentioned = source.fetched_ids(), source.mentioned_ids()
    assert len(fetched) == len(set(fetched))
    assert len(mentioned) == len(set(mentioned))


# --- Basic shapes -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["constrained", "full"])
def test_linear_chain(mode):
    source = FakeLinkSource.from_raw(
        act("A"), act("B", funded_by=["A"]), act("C", funded_by=["B"]), act("D", parent="C")
    )
    chain = _chain(source, "B", mode)
    assert (chain.seed, chain.mode, chain.truncated) == ("B", mode, False)
    assert _relations(chain) == {"B": "seed", "A": "ancestor", "C": "descendant", "D": "descendant"}
    assert _distances(chain) == {"B": 0, "A": 1, "C": 1, "D": 2}
    assert _edges(chain) == {("A", "B"), ("B", "C"), ("C", "D")}
    assert all(_expanded(chain).values())
    _assert_each_id_queried_once(source)


def test_seed_without_links():
    chain = _chain(FakeLinkSource.from_raw(act("X")), "X")
    assert [(n.id, n.relation, n.distance, n.expanded) for n in chain.nodes] == [("X", "seed", 0, True)]
    assert (chain.edges, chain.associations, chain.truncated) == ((), (), False)


def test_diamond_visits_the_shared_node_once_and_keeps_both_edges():
    source = FakeLinkSource.from_raw(
        act("A"), act("B", funded_by=["A"]), act("C", funded_by=["A"]), act("D", funded_by=["B", "C"])
    )
    chain = _chain(source, "A")
    assert [n.id for n in chain.nodes].count("D") == 1
    assert _edges(chain) == {("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")}
    assert _distances(chain)["D"] == 2
    _assert_each_id_queried_once(source)


@pytest.mark.parametrize("mode", ["constrained", "full"])
def test_cycle_terminates_and_ancestor_wins_over_descendant(mode):
    source = FakeLinkSource.from_raw(act("A", funded_by=["B"]), act("B", funded_by=["A"]))
    chain = _chain(source, "A", mode)
    assert _relations(chain) == {"A": "seed", "B": "ancestor"}
    assert _edges(chain) == {("A", "B"), ("B", "A")}
    assert chain.truncated is False


def test_links_declared_only_by_the_other_end_are_found():
    # P declares its child; C declares its funder. Neither is declared by the seed X itself.
    source = FakeLinkSource.from_raw(act("P", children=["X"]), act("X"), act("C", funding_role=["X"]))
    chain = _chain(source, "X")
    assert _relations(chain) == {"X": "seed", "P": "ancestor", "C": "descendant"}


# --- Modes --------------------------------------------------------------------------------------------------------


def _two_branches() -> FakeLinkSource:
    """P funds X (the seed) and Y; Y funds Z; X funds C."""
    return FakeLinkSource.from_raw(
        act("P"),
        act("X", funded_by=["P"]),
        act("Y", funded_by=["P"]),
        act("Z", funded_by=["Y"]),
        act("C", funded_by=["X"]),
    )


def test_constrained_mode_leaves_out_the_funders_other_branches():
    chain = _chain(_two_branches(), "X", "constrained")
    assert _relations(chain) == {"X": "seed", "P": "ancestor", "C": "descendant"}


def test_full_mode_expands_down_from_every_ancestor():
    source = _two_branches()
    chain = _chain(source, "X", "full")
    assert _relations(chain) == {"X": "seed", "P": "ancestor", "C": "descendant", "Y": "related", "Z": "related"}
    assert _distances(chain) == {"X": 0, "P": 1, "C": 1, "Y": 2, "Z": 3}
    assert all(_expanded(chain).values())
    _assert_each_id_queried_once(source)


def test_full_mode_returns_co_funders_unexpanded():
    source = FakeLinkSource.from_raw(
        act("P"),
        act("X", funded_by=["P"]),
        act("Y", funded_by=["P", "Q"]),
        act("Q", title="Co-funder"),
        act("Z", funded_by=["Q"]),
    )
    chain = _chain(source, "X", "full")
    nodes = _nodes(chain)
    assert _relations(chain) == {"X": "seed", "P": "ancestor", "Y": "related", "Q": "co-funder"}
    assert (nodes["Q"].published, nodes["Q"].title, nodes["Q"].expanded) == (True, "Co-funder", False)
    assert ("Q", "Y") in _edges(chain)
    assert "Q" not in source.mentioned_ids()
    assert nodes["Y"].expanded is True
    _assert_each_id_queried_once(source)


def test_full_mode_co_funder_of_a_descendant_below_the_seed():
    source = FakeLinkSource.from_raw(act("X"), act("C", funded_by=["X", "Q"]), act("Q"))
    chain = _chain(source, "X", "full")
    assert _relations(chain) == {"X": "seed", "C": "descendant", "Q": "co-funder"}
    assert _chain(FakeLinkSource.from_raw(act("X"), act("C", funded_by=["X", "Q"]), act("Q")), "X").node_ids == {
        "X",
        "C",
    }


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="mode"):
        _chain(FakeLinkSource.from_raw(act("X")), "X", "sideways")


# --- Organisation nodes -------------------------------------------------------------------------------------------


def _org_graph() -> FakeLinkSource:
    """P funds X (seed) funds C. Each pays one organisation (O1) and is paid by one (O2)."""
    return FakeLinkSource.from_raw(
        act("P", paid_by_orgs=["G"], pays_orgs=["H"]),
        act("X", funded_by=["P"], paid_by_orgs=["F"], pays_orgs=["R"]),
        act("C", funded_by=["X"], paid_by_orgs=["L"], pays_orgs=["K"]),
    )


def test_constrained_mode_places_organisations_by_position():
    source = _org_graph()
    chain = _chain(source, "X", "constrained")
    relations = _relations(chain)
    assert {k: v for k, v in relations.items() if k.startswith("org:")} == {
        "org:F": "ancestor",
        "org:G": "ancestor",
        "org:R": "descendant",
        "org:K": "descendant",
    }
    assert _distances(chain)["org:G"] == 2
    org = _nodes(chain)["org:F"]
    assert org == OrganisationNode(
        id="org:F", relation="ancestor", distance=1, org_ref="F", name="Org F", identification="identifier"
    )
    assert org.expanded is False
    assert not [i for i in source.fetched_ids() + source.mentioned_ids() if i.startswith("org:")]


def test_full_mode_places_organisations_by_position():
    chain = _chain(_org_graph(), "X", "full")
    orgs = {k: v for k, v in _relations(chain).items() if k.startswith("org:")}
    assert orgs == {
        "org:F": "ancestor",
        "org:G": "ancestor",
        "org:R": "descendant",
        "org:K": "descendant",
        "org:H": "related",  # paid by an ancestor, not below the seed
        "org:L": "co-funder",  # pays a node of D
    }
    assert ("C", "org:K") in _edges(chain)
    assert ("org:L", "C") in _edges(chain)


def test_organisations_count_towards_max_nodes():
    chain = _chain(FakeLinkSource.from_raw(act("X", pays_orgs=["R1", "R2", "R3"])), "X", limits=Limits(10, 3))
    assert chain.node_ids == {"X", "org:R1", "org:R2"}
    assert chain.truncated is True
    assert _expanded(chain)["X"] is False


def test_unstructured_organisations_are_left_out_by_default():
    source = FakeLinkSource.from_raw(act("X", pays_orgs=["BRAC UK", "GB-CHC-1"]))
    chain = _chain(source, "X")
    assert chain.node_ids == {"X", "org:GB-CHC-1"}
    assert chain.truncated is False
    assert _expanded(chain)["X"] is True


def _mixed_orgs() -> FakeLinkSource:
    return FakeLinkSource.from_raw(
        act("X", pays_orgs=["BRAC UK", "GB-CHC-1"], pays_named=["Norwegian  Refugee Council"])
    )


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("identified", {"org:GB-CHC-1"}),
        ("refs", {"org:GB-CHC-1", "org:BRAC UK"}),
        ("all", {"org:GB-CHC-1", "org:BRAC UK", "org-name:norwegian refugee council"}),
    ],
)
def test_orgs_level_selects_organisations_by_identification(level, expected):
    chain = _chain(_mixed_orgs(), "X", orgs=level)
    assert chain.node_ids == {"X"} | expected
    assert chain.truncated is False
    assert _expanded(chain)["X"] is True


def test_organisation_nodes_carry_their_identification():
    nodes = _nodes(_chain(_mixed_orgs(), "X", orgs="all"))
    assert nodes["org:BRAC UK"].identification == "free-text"
    assert nodes["org-name:norwegian refugee council"] == OrganisationNode(
        id="org-name:norwegian refugee council",
        relation="descendant",
        distance=1,
        org_ref=None,
        name="Norwegian  Refugee Council",
        identification="name-only",
    )


def test_unknown_orgs_level_is_rejected():
    with pytest.raises(ValueError, match="orgs"):
        _chain(_mixed_orgs(), "X", orgs="everything")


def test_funder_named_as_participating_org_only_is_an_upstream_organisation():
    # O3: the publisher guidance's interim state: the funder's organisation is known, its activity ID is not yet.
    source = FakeLinkSource.from_raw(act("X", funder_orgs=["GB-GOV-1"]))
    chain = _chain(source, "X")
    assert _relations(chain) == {"X": "seed", "org:GB-GOV-1": "ancestor"}
    assert chain.edges == (ChainEdge("org:GB-GOV-1", "X", ("participating-org",), ("X",)),)


def test_organisation_ref_is_shown_as_first_spelled_and_merged_across_letter_case():
    source = FakeLinkSource.from_raw(
        act("X", pays_orgs=["gb-chc-1"]), act("C", funded_by=["X"], pays_orgs=["GB-CHC-1"])
    )
    chain = _chain(source, "X")
    assert chain.node_ids == {"X", "C", "org:GB-CHC-1"}  # C sorts before X, so its spelling is read first
    assert _edges(chain) == {("X", "C"), ("X", "org:GB-CHC-1"), ("C", "org:GB-CHC-1")}


def test_organisation_name_comes_from_the_first_document_that_has_one():
    unnamed = act("C", funded_by=["X"], pays_orgs=["R"])
    unnamed["json.transaction"] = [value.replace(', "narrative": "Org R"', "") for value in unnamed["json.transaction"]]
    chain = _chain(FakeLinkSource.from_raw(unnamed, act("X", pays_orgs=["R"])), "X")
    assert _nodes(chain)["org:R"].name == "Org R"


def test_unstructured_organisations_do_not_use_up_max_nodes():
    source = FakeLinkSource.from_raw(act("X", pays_orgs=["A B", "C D", "GB-CHC-1"]))
    chain = _chain(source, "X", limits=Limits(10, 2))
    assert chain.node_ids == {"X", "org:GB-CHC-1"}
    assert chain.truncated is False


# --- Associations and edges ---------------------------------------------------------------------------------------


def test_associations_only_between_returned_activities():
    source = FakeLinkSource.from_raw(
        act("G", children=["P"], associated=[("X", 3)]),
        act("P"),
        act("X", funded_by=["P"], associated=[("G", 3), ("OUTSIDE", 2)]),
    )
    chain = _chain(source, "X")
    assert chain.associations == (ChainAssociation(activities=("G", "X"), role=3, declared_by=("G", "X")),)
    assert "OUTSIDE" not in chain.node_ids


@pytest.mark.parametrize("declarer", ["P", "X"])
def test_association_is_dropped_when_an_edge_joins_the_same_pair(declarer):
    # A funder listed under role 1 and under role 2 is one link, not two (spec §4.2, L7), whoever declares the edge.
    docs = {"P": act("P"), "X": act("X", associated=[("P", 2)])}
    docs[declarer] = (
        act("P", children=["X"]) if declarer == "P" else act("X", funding_role=["P"], associated=[("P", 2)])
    )
    chain = _chain(FakeLinkSource.from_raw(*docs.values()), "X")
    assert _edges(chain) == {("P", "X")}
    assert chain.associations == ()


def test_edges_are_merged_by_pair_with_kinds_in_order_and_all_declarers():
    source = FakeLinkSource.from_raw(
        act("P", children=["X"]), act("X", parent="P", funding_role=["P"], funded_by=["P"])
    )
    chain = _chain(source, "X")
    assert chain.edges == (
        ChainEdge(
            upstream="P",
            downstream="X",
            kinds=("hierarchy", "transaction", "participating-org"),
            declared_by=("P", "X"),
        ),
    )


def test_edges_between_returned_nodes_that_were_not_walked_are_included():
    # P funds both X (seed) and C directly, and X funds C: the P → C edge is returned too.
    source = FakeLinkSource.from_raw(act("P"), act("X", funded_by=["P"]), act("C", funded_by=["X", "P"]))
    assert _edges(_chain(source, "X")) == {("P", "X"), ("X", "C"), ("P", "C")}


def test_output_is_sorted():
    chain = _chain(_two_branches(), "X", "full")
    assert [(n.distance, n.id) for n in chain.nodes] == sorted((n.distance, n.id) for n in chain.nodes)
    assert [(e.upstream, e.downstream) for e in chain.edges] == sorted(_edges(chain))


def test_activity_node_carries_metadata():
    raw = act("X", title="A title", reporting_org="XX-ORG")
    raw.update({"reporting-org.narrative": ["The Org"], "activity-status.code": "2", "hierarchy": 2})
    node = _nodes(_chain(FakeLinkSource.from_raw(raw), "X"))["X"]
    assert node == ActivityNode(
        id="X",
        relation="seed",
        distance=0,
        expanded=True,
        published=True,
        title="A title",
        reporting_org_ref="XX-ORG",
        reporting_org_name="The Org",
        hierarchy=2,
        activity_status="2",
    )


# --- Unpublished nodes and the seed -------------------------------------------------------------------------------


def test_unpublished_node_is_returned_but_never_expanded():
    source = FakeLinkSource.from_raw(act("X", funds=["U"]), act("V", funded_by=["U"]))
    chain = _chain(source, "X")
    node = _nodes(chain)["U"]
    assert (node.published, node.title, node.reporting_org_ref, node.expanded) == (False, None, None, False)
    assert chain.node_ids == {"X", "U"}
    assert "U" not in source.mentioned_ids()
    assert chain.truncated is False


@pytest.mark.parametrize("mode", ["constrained", "full"])
def test_heavily_cited_unpublished_funder_is_not_expanded(mode):
    source = FakeLinkSource.from_raw(*(act(f"C{n}", funding_role=["XM-OCHA-CERF"]) for n in range(5)))
    chain = _chain(source, "C0", mode)
    assert chain.node_ids == {"C0", "XM-OCHA-CERF"}
    assert _relations(chain)["XM-OCHA-CERF"] == "ancestor"
    assert "XM-OCHA-CERF" not in source.mentioned_ids()


def test_unpublished_seed_that_is_referenced_is_expanded():
    # Decision Q8: the seed is expanded through the reverse lookup even though it isn't published.
    source = FakeLinkSource.from_raw(act("A", funds=["U"]), act("B", funded_by=["U"]), act("B2", funded_by=["B"]))
    chain = _chain(source, "U")
    seed = _nodes(chain)["U"]
    assert (seed.published, seed.expanded, seed.relation) == (False, True, "seed")
    assert _relations(chain) == {"U": "seed", "A": "ancestor", "B": "descendant", "B2": "descendant"}


def test_unknown_unreferenced_seed_is_not_found():
    with pytest.raises(SeedNotFound):
        _chain(FakeLinkSource.from_raw(act("X")), "NOWHERE")


# --- Limits -------------------------------------------------------------------------------------------------------


def _line(length: int) -> FakeLinkSource:
    """N0 → N1 → … each declared by the downstream activity."""
    return FakeLinkSource.from_raw(act("N0"), *(act(f"N{i}", funded_by=[f"N{i - 1}"]) for i in range(1, length)))


def test_max_depth_truncates_and_marks_the_boundary_unexpanded():
    source = _line(4)
    chain = _chain(source, "N0", limits=Limits(max_depth=1, max_nodes=500))
    assert chain.node_ids == {"N0", "N1"}
    assert chain.truncation_reasons == ("max_depth",)
    assert chain.truncated is True
    assert _expanded(chain) == {"N0": True, "N1": False}
    assert "N1" not in source.mentioned_ids()


def test_max_depth_counts_hops_per_walk():
    chain = _chain(_line(5), "N2", limits=Limits(max_depth=2, max_nodes=500))
    assert chain.node_ids == {"N0", "N1", "N2", "N3", "N4"}
    assert _expanded(chain) == {"N0": False, "N1": True, "N2": True, "N3": True, "N4": False}


def test_max_nodes_truncates_and_marks_the_cut_node_unexpanded():
    source = FakeLinkSource.from_raw(act("P", children=[f"C{n}" for n in range(5)]), *(act(f"C{n}") for n in range(5)))
    chain = _chain(source, "P", limits=Limits(max_depth=10, max_nodes=3))
    assert chain.node_ids == {"P", "C0", "C1"}
    assert chain.truncation_reasons == ("max_nodes",)
    assert _expanded(chain) == {"P": False, "C0": True, "C1": True}


def test_max_nodes_in_full_mode_counts_co_funders():
    source = FakeLinkSource.from_raw(act("X"), act("C", funded_by=["X", "Q1", "Q2"]), act("Q1"), act("Q2"))
    chain = _chain(source, "X", "full", limits=Limits(max_depth=10, max_nodes=3))
    assert chain.node_ids == {"X", "C", "Q1"}
    assert chain.truncated is True
    assert _expanded(chain)["C"] is False


def test_mention_limit_stops_a_huge_fan_in():
    source = FakeLinkSource.from_raw(act("HUB"), *(act(f"K{n:02d}", funded_by=["HUB"]) for n in range(20)))
    chain = _chain(source, "HUB", limits=Limits(max_depth=10, max_nodes=5))
    assert len(chain.nodes) <= 5
    assert "mention_limit" in chain.truncation_reasons
    assert _expanded(chain)["HUB"] is False


def test_several_truncation_reasons_are_reported_in_a_fixed_order():
    source = FakeLinkSource.from_raw(
        act("N0", children=["Z1", "Z2", "Z3"]), act("N1", funded_by=["N0"]), act("N2", funded_by=["N1"])
    )
    chain = _chain(source, "N0", limits=Limits(max_depth=1, max_nodes=3))
    assert chain.truncation_reasons == ("max_depth", "max_nodes")


def test_max_nodes_keeps_ancestors_before_descendants():
    # Admission order (spec §5.3): the seed, then ancestors, then descendants.
    source = FakeLinkSource.from_raw(
        act("G"),
        act("P", funded_by=["G"]),
        act("X", funded_by=["P"]),
        act("C1", funded_by=["X"]),
        act("C2", funded_by=["X"]),
    )
    for mode in ("constrained", "full"):
        chain = _chain(source, "X", mode, limits=Limits(max_depth=10, max_nodes=4))
        assert chain.node_ids == {"X", "P", "G", "C1"}
        assert chain.truncation_reasons == ("max_nodes",)


def test_budget_running_out_returns_a_truncated_result():
    clock = FakeClock()
    source = _line(4)

    def one_second_per_call() -> None:
        clock.now += 1

    source.on_call = one_second_per_call
    chain = _chain(source, "N0", budget=Budget.of_seconds(2.5, clock=clock))
    # fetch N0, mention N0, fetch N1: three calls; the budget is gone before N1's reverse lookup.
    assert chain.truncation_reasons == ("time_budget",)
    assert chain.node_ids == {"N0", "N1"}
    assert _expanded(chain) == {"N0": True, "N1": False}


def test_nodes_whose_metadata_could_not_be_fetched_are_left_out():
    # X declares its children; when the budget runs out before they are fetched, they are dropped and X is
    # marked unexpanded.
    clock = FakeClock()
    source = FakeLinkSource.from_raw(act("X", children=["C1", "C2"]), act("C1"), act("C2"))

    def two_seconds_per_call() -> None:
        clock.now += 2

    source.on_call = two_seconds_per_call
    chain = _chain(source, "X", budget=Budget.of_seconds(3, clock=clock))
    assert chain.node_ids == {"X"}
    assert chain.truncated is True
    assert _expanded(chain) == {"X": False}


def test_unpublished_seed_when_budget_runs_out_before_the_reverse_lookup():
    clock = FakeClock()
    source = FakeLinkSource.from_raw(act("A", funds=["U"]))

    def five_seconds_per_call() -> None:
        clock.now += 5

    source.on_call = five_seconds_per_call
    chain = _chain(source, "U", budget=Budget.of_seconds(1, clock=clock))
    assert chain.node_ids == {"U"}
    assert chain.truncated is True
    assert _expanded(chain) == {"U": False}


def test_budget_exhausted_before_the_seed_is_fetched_raises():
    clock = FakeClock()
    budget = Budget.of_seconds(1, clock=clock)
    clock.now += 2
    with pytest.raises(BudgetExhausted):
        _chain(FakeLinkSource.from_raw(act("X")), "X", budget=budget)


def test_each_document_is_fetched_and_each_id_mention_queried_once():
    source = FakeLinkSource.from_raw(
        act("P", paid_by_orgs=["G"]),
        act("X", funded_by=["P"], pays_orgs=["R"]),
        act("Y", funded_by=["P", "Q"]),
        act("Q"),
        act("C", funded_by=["X", "Y"]),
    )
    _chain(source, "X", "full")
    _assert_each_id_queried_once(source)


@pytest.mark.parametrize(("max_depth", "max_nodes"), [(0, 10), (10, 0), (-1, 5)])
def test_limits_must_be_positive(max_depth, max_nodes):
    with pytest.raises(ValueError, match="positive"):
        Limits(max_depth=max_depth, max_nodes=max_nodes)


def test_co_funder_whose_metadata_could_not_be_fetched_is_left_out():
    clock = FakeClock()
    source = FakeLinkSource.from_raw(act("P"), act("X", funded_by=["P"]), act("Y", funded_by=["P", "Q"]), act("Q"))

    def one_second_per_call() -> None:
        clock.now += 1

    source.on_call = one_second_per_call
    # fetch X, mention X, fetch P, mention P, mention Y: five calls; the budget is gone before Q can be fetched.
    chain = _chain(source, "X", "full", budget=Budget.of_seconds(5, clock=clock))
    assert chain.node_ids == {"X", "P", "Y"}
    assert chain.truncated is True
    assert _expanded(chain)["Y"] is False
    assert "Q" not in source.fetched_ids()


# --- Spec §4 worked example, replayed from documents captured live on 2026-11-06 ---------------------------------

AAH = "GB-CHC-1047501-23SOELNF"
PROGRAMME = "GB-GOV-1-300978"
PROJECTS = {f"{PROGRAMME}-{n}" for n in [*range(101, 113), *range(401, 414)]}
TROCAIRE = {f"IE-CHY-20009601-FCDO{suffix}" for suffix in ("23SOM", "24SOM", "24ESOM", "26SOM")}
NRC = "NO-BRC-977538319-SOFM2343"


@pytest.fixture(scope="module")
def worked_example() -> dict:
    return load_chain_fixture("worked_example")


def _replay(worked_example: dict, seed: str, mode: str, **kwargs) -> Chain:
    return _chain(FakeLinkSource.from_raw(*worked_example["docs"]), seed, mode, **kwargs)


def test_worked_example_constrained_from_the_ngo_activity(worked_example):
    chain = _replay(worked_example, AAH, "constrained")
    assert chain.node_ids == {AAH, f"{PROGRAMME}-408", PROGRAMME, "org:US-EIN-13-3327220"}
    assert _relations(chain) == {
        AAH: "seed",
        f"{PROGRAMME}-408": "ancestor",
        PROGRAMME: "ancestor",
        "org:US-EIN-13-3327220": "descendant",
    }
    assert _distances(chain) == {AAH: 0, f"{PROGRAMME}-408": 1, PROGRAMME: 2, "org:US-EIN-13-3327220": 1}
    edge = next(e for e in chain.edges if (e.upstream, e.downstream) == (f"{PROGRAMME}-408", AAH))
    assert (edge.kinds, edge.declared_by) == (("transaction", "participating-org"), (AAH,))
    assert not chain.node_ids & ({f"{PROGRAMME}-405", NRC} | TROCAIRE)
    assert chain.truncated is False


def test_worked_example_full_from_the_ngo_activity(worked_example):
    chain = _replay(worked_example, AAH, "full")
    must_include = {f"{PROGRAMME}-408", PROGRAMME, "org:US-EIN-13-3327220", NRC} | PROJECTS | TROCAIRE
    assert must_include <= chain.node_ids
    relations = _relations(chain)
    assert {relations[p] for p in PROJECTS - {f"{PROGRAMME}-408"}} == {"related"}
    assert relations[NRC] == "related"
    assert chain.truncated is False


def test_worked_example_constrained_from_the_programme(worked_example):
    chain = _replay(worked_example, PROGRAMME, "constrained")
    assert PROJECTS | {AAH, NRC} | TROCAIRE <= chain.node_ids
    assert set(_relations(chain).values()) == {"seed", "descendant"}
    assert chain.truncated is False


def test_worked_example_replay_matches_the_live_runs(worked_example):
    for run in worked_example["runs"]:
        chain = _replay(worked_example, run["seed"], run["mode"], orgs=run["orgs"])
        assert (len(chain.nodes), len(chain.edges)) == (run["nodes"], run["edges"])
        assert list(chain.truncation_reasons) == run["truncation_reasons"]


def test_worked_example_from_a_seed_in_another_letter_case(worked_example):
    # Spec §2, row 4: same chain, shown with the published spelling.
    assert _replay(worked_example, AAH.lower(), "constrained") == _replay(worked_example, AAH, "constrained")


def test_worked_example_under_the_corrected_rules(worked_example):
    chain = _replay(worked_example, AAH, "full", orgs="all")
    # The NRC activity publishes partner organisation refs as receiver-activity-ids on incoming transactions:
    # no longer activity nodes below it (spec C1). The same partners remain as organisations.
    assert not chain.node_ids & {"GB-CHC-1065972", "GB-COH-213890", "GB-CHC-1092236"}
    assert {"org:GB-CHC-1065972", "org:GB-COH-213890", "org:GB-CHC-1092236"} <= chain.node_ids
    # A Trócaire activity lists its own organisation as implementer with another of its activities: not a link (C2).
    assert ("IE-CHY-20009601-FCDO24ESOM", "IE-CHY-20009601-FCDO25SOM") not in _edges(chain)
    # Recipients that name FCDO only as an organisation show it as their funder (O3).
    assert "org:GB-GOV-1" in chain.node_ids


# --- Several documents for one identifier (spec §4.5) -------------------------------------------------------------


def test_links_of_every_copy_of_an_identifier_are_followed():
    # One copy of Y cites the seed, the other declares Y → Z. The reverse lookup only returns the first.
    source = FakeLinkSource.from_raw(
        act("S"), act("Y", funded_by=["S"], doc_id="1"), act("Y", funds=["Z"], doc_id="2"), act("Z")
    )
    chain = _chain(source, "S")
    assert _relations(chain) == {"S": "seed", "Y": "descendant", "Z": "descendant"}
    assert all(_expanded(chain).values())
    _assert_each_id_queried_once(source)


def test_metadata_of_a_duplicated_identifier_comes_from_the_newest_copy():
    source = FakeLinkSource.from_raw(
        act("X", title="Old", doc_id="1", updated="2020-01-01T00:00:00Z"),
        act("X", title="New", doc_id="2", updated="2021-01-01T00:00:00Z"),
    )
    assert _nodes(_chain(source, "X"))["X"].title == "New"


def test_real_duplicated_identifier_is_one_node_with_the_links_of_all_copies():
    identifier = "ZA-ROC-1999/05072/08-GPAF-IMP-001"
    copies = load_activity_copies(identifier)
    assert len(copies) == 8
    assert {raw["iati-identifier"] for raw in copies} == {identifier, "ZA-ROC-1999/05072/08-GPAF-imp-001"}
    chain = _chain(FakeLinkSource.from_raw(*copies), identifier.lower(), orgs="all")
    seed = chain.nodes[0]
    assert (seed.id, seed.relation) == (identifier, "seed")  # the spelling of the copy updated last
    single = [_chain(FakeLinkSource.from_raw(raw), raw["iati-identifier"], orgs="all") for raw in copies]
    others = {n for c in single for n in c.node_ids if n.lower() != identifier.lower()}
    assert chain.node_ids == {identifier} | others
    assert len(chain.nodes) > max(len(c.nodes) for c in single)  # no single copy declares every link


# --- Identifiers in any letter case (spec §4.5) -------------------------------------------------------------------


def test_seed_is_found_in_any_letter_case_and_shown_as_published():
    source = FakeLinkSource.from_raw(act("GB-P"), act("GB-X", funded_by=["GB-P"]))
    chain = _chain(source, "  gb-x ")
    assert chain.seed == "GB-X"
    assert _relations(chain) == {"GB-X": "seed", "GB-P": "ancestor"}


def test_spellings_of_one_identifier_are_one_node():
    source = FakeLinkSource.from_raw(act("GB-P"), act("A", funded_by=["gb-p"]), act("B", funded_by=["Gb-P"]))
    chain = _chain(source, "A", "full")
    assert _relations(chain) == {"A": "seed", "GB-P": "ancestor", "B": "related"}
    assert _edges(chain) == {("GB-P", "A"), ("GB-P", "B")}


def test_unpublished_node_is_shown_with_the_first_spelling_seen():
    source = FakeLinkSource.from_raw(act("X", funded_by=["u-1", "U-1"]))
    assert _chain(source, "X").node_ids == {"X", "U-1"}


def test_unpublished_seed_is_shown_as_typed_or_as_cited():
    source = FakeLinkSource.from_raw(act("A", funds=["Unpub"]))
    assert _chain(source, "UNPUB").seed == "UNPUB"  # "UNPUB" sorts before "Unpub"
    assert _chain(source, "unpub").seed == "Unpub"


def test_reverse_lookup_uses_every_spelling_seen():
    source = FakeLinkSource.from_raw(act("GB-P"), act("A", funded_by=["gb-p"]))
    _chain(source, "A", "full")
    assert {"GB-P", "gb-p"} <= set(source.mentioned_ids())


def test_empty_seed_is_rejected():
    with pytest.raises(ValueError, match="seed"):
        _chain(FakeLinkSource.from_raw(act("X")), "   ")


def test_link_cited_under_a_spelling_learned_later_is_still_found():
    # The exact-match link fields only find "xm-1" when asked for "xm-1". That spelling is first seen in P-1's
    # document, after XM-1's own reverse lookup. The walks are repeated with it, and Y-1 is found.
    docs = [act("XM-1", parent="P-1"), act("P-1", children=["xm-1"]), act("Y-1", funded_by=["xm-1"])]
    source = FakeLinkSource.from_raw(*docs, exact_mentions=True)
    chain = _chain(source, "XM-1")
    assert _relations(chain) == {"XM-1": "seed", "P-1": "ancestor", "Y-1": "descendant"}
    assert (chain.truncated, all(_expanded(chain).values())) == (False, True)
    assert sorted(i for i in source.mentioned_ids() if i.lower() == "xm-1") == ["XM-1", "xm-1"]
    _assert_each_id_queried_once(source)


def test_no_extra_round_when_no_new_spelling_turns_up():
    source = _two_branches()
    _chain(source, "X", "full")
    lookups = [sorted(ids) for method, ids in source.calls if method == "mentioning"]
    assert lookups == [["X"], ["P"], ["C", "Y"], ["Z"]]  # one per hop, none repeated


def test_incomplete_lookup_of_a_later_spelling_marks_the_node_unexpanded():
    docs = [
        act("XM-1", parent="P-1"),
        act("P-1", children=["xm-1"]),
        *(act(f"Y-{n}", funded_by=["xm-1"]) for n in range(9)),
    ]
    chain = _chain(
        FakeLinkSource.from_raw(*docs, exact_mentions=True), "XM-1", limits=Limits(max_depth=10, max_nodes=5)
    )
    assert "mention_limit" in chain.truncation_reasons
    assert _expanded(chain)["XM-1"] is False


# --- Identifiers that look like organisation nodes ----------------------------------------------------------------


@pytest.mark.parametrize("odd", ["org:x", "org-name:x", "Org: see parent"])
def test_activity_identifier_that_looks_like_an_organisation_node(odd):
    # Free text in an identifier field must not be mistaken for an organisation node, whatever it looks like.
    for level in ("identified", "all"):
        chain = _chain(FakeLinkSource.from_raw(act("A-1", children=[odd])), "A-1", orgs=level)
        node = _nodes(chain)[odd]
        assert isinstance(node, ActivityNode)
        assert (node.published, node.relation) == (False, "descendant")


@pytest.mark.parametrize("seed", ["org:x", "org-name:x"])
def test_published_activity_with_an_organisation_like_identifier_can_be_the_seed(seed):
    chain = _chain(FakeLinkSource.from_raw(act(seed, funded_by=["P"]), act("P")), seed)
    assert _relations(chain) == {seed: "seed", "P": "ancestor"}


def test_organisation_is_a_dead_end_for_relations():
    # A pays organisation O; B names O as its funder. That does not put B below A: O is never searched further.
    source = FakeLinkSource.from_raw(
        act("A", pays_orgs=["O"]), act("C", funded_by=["A", "B"]), act("B", paid_by_orgs=["O"])
    )
    chain = _chain(source, "A", "full")
    assert _relations(chain) == {"A": "seed", "C": "descendant", "org:O": "descendant", "B": "co-funder"}
