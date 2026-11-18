"""Tests for app.iati_cloud_dcma.tree: the tree format, derived from a Chain."""

import random

import pytest

from app.iati_cloud_dcma.chain import ActivityNode, Chain, ChainEdge
from app.iati_cloud_dcma.traversal import Limits, build_chain
from app.iati_cloud_dcma.tree import Tree, TreeNode, to_tree
from tests.conftest import FakeLinkSource, load_chain_fixture
from tests.conftest import raw_activity as act

LIMITS = Limits(max_depth=10, max_nodes=500)


def _tree(*raw_docs: dict, seed: str = "X", mode: str = "constrained", limits: Limits = LIMITS, **kwargs) -> Tree:
    return to_tree(build_chain(FakeLinkSource.from_raw(*raw_docs), seed, mode, limits, **kwargs))


def _shape(node: TreeNode) -> dict:
    """A tree as nested dicts: {id: {"up": {...}, "down": {...}}}, leaving out empty lists."""
    children = {}
    if node.upstream:
        children["up"] = {key: value for child in node.upstream for key, value in _shape(child).items()}
    if node.downstream:
        children["down"] = {key: value for child in node.downstream for key, value in _shape(child).items()}
    return {node.node.id: children}


def _ids(node: TreeNode) -> list[str]:
    return [node.node.id, *(i for child in (*node.upstream, *node.downstream) for i in _ids(child))]


def _tree_edge_count(node: TreeNode) -> int:
    children = (*node.upstream, *node.downstream)
    return len(children) + sum(_tree_edge_count(child) for child in children)


def test_root_is_the_seed_and_children_follow_edge_direction():
    tree = _tree(act("G"), act("P", funded_by=["G"]), act("X", funded_by=["P"]), act("C", funded_by=["X"]))
    assert tree.root.node.relation == "seed"
    assert _shape(tree.root) == {"X": {"up": {"P": {"up": {"G": {}}}}, "down": {"C": {}}}}
    assert tree.extra_edges == ()


def test_seed_without_links_is_a_single_node():
    tree = _tree(act("X"))
    assert _shape(tree.root) == {"X": {}}
    assert (tree.root.upstream, tree.root.downstream, tree.extra_edges) == ((), (), ())


def test_other_branch_of_a_funder_hangs_downstream_of_that_funder():
    tree = _tree(act("P"), act("X", funded_by=["P"]), act("Y", funded_by=["P"]), mode="full")
    assert _shape(tree.root) == {"X": {"up": {"P": {"down": {"Y": {}}}}}}


def test_organisation_nodes_are_leaves():
    tree = _tree(act("X", paid_by_orgs=["F"], pays_orgs=["R"]))
    assert _shape(tree.root) == {"X": {"up": {"org:F": {}}, "down": {"org:R": {}}}}
    assert tree.root.upstream[0].node.expanded is False


def test_diamond_puts_the_second_path_in_extra_edges():
    tree = _tree(act("X"), act("B", funded_by=["X"]), act("C", funded_by=["X"]), act("D", funded_by=["B", "C"]))
    assert _shape(tree.root) == {"X": {"down": {"B": {"down": {"D": {}}}, "C": {}}}}
    assert tree.extra_edges == (ChainEdge("C", "D", ("transaction",), ("D",)),)


def test_cycle_keeps_the_first_edge_and_reports_the_other():
    tree = _tree(act("X", funded_by=["B"]), act("B", funded_by=["X"]))
    # Same kind and same neighbour: the downstream edge is taken first.
    assert _shape(tree.root) == {"X": {"down": {"B": {}}}}
    assert [(e.upstream, e.downstream) for e in tree.extra_edges] == [("B", "X")]


def test_neighbours_are_visited_by_kind_then_node_id():
    # D is reachable from A (transaction) and from Z (hierarchy). Both are found at the same depth; hierarchy wins.
    docs = [act("X"), act("A", funded_by=["X"]), act("Z", funded_by=["X"]), act("D", parent="Z", funded_by=["A"])]
    tree = _tree(*docs)
    assert _shape(tree.root) == {"X": {"down": {"A": {"down": {"D": {}}}, "Z": {}}}}
    # ...but breadth-first order comes first: A is expanded before Z because it was discovered first (by node ID),
    # so D hangs under A. With D one hop closer through a hierarchy edge from the seed, hierarchy decides:
    docs = [act("X", children=["H"]), act("H"), act("T", funded_by=["X"]), act("D", parent="H", funded_by=["T"])]
    assert _shape(_tree(*docs).root) == {"X": {"down": {"H": {"down": {"D": {}}}, "T": {}}}}


def test_children_are_ordered_by_kind_then_node_id():
    docs = [act("X", children=["M"]), act("M"), act("B", funded_by=["X"]), act("A", funding_role=["X"])]
    tree = _tree(*docs)
    assert [child.node.id for child in tree.root.downstream] == ["M", "B", "A"]


def test_every_node_once_and_no_edge_lost():
    chain = build_chain(
        FakeLinkSource.from_raw(
            act("P", paid_by_orgs=["G"]),
            act("X", funded_by=["P"], pays_orgs=["R"], parent="P"),
            act("Y", funded_by=["P", "Q"], pays_orgs=["R"]),
            act("Q"),
            act("C", funded_by=["X", "Y"]),
        ),
        "X",
        "full",
        LIMITS,
    )
    tree = to_tree(chain)
    assert sorted(_ids(tree.root)) == sorted(chain.node_ids)
    assert _tree_edge_count(tree.root) + len(tree.extra_edges) == len(chain.edges)
    assert _tree_edge_count(tree.root) == len(chain.nodes) - 1


def test_tree_does_not_depend_on_the_order_of_the_chain():
    chain = build_chain(
        FakeLinkSource.from_raw(
            act("P"), act("X", funded_by=["P"]), act("Y", funded_by=["P"]), act("C", funded_by=["X", "Y"], parent="X")
        ),
        "X",
        "full",
        LIMITS,
    )
    expected = to_tree(chain)
    rng = random.Random(7)
    for _ in range(10):
        shuffled = Chain(
            chain.seed,
            chain.mode,
            chain.truncation_reasons,
            tuple(rng.sample(chain.nodes, len(chain.nodes))),
            tuple(rng.sample(chain.edges, len(chain.edges))),
            chain.associations,
        )
        assert to_tree(shuffled) == expected


def test_truncated_chain_still_gives_a_valid_tree():
    docs = [act("N0"), *(act(f"N{i}", funded_by=[f"N{i - 1}"]) for i in range(1, 5))]
    tree = _tree(*docs, seed="N0", limits=Limits(max_depth=2, max_nodes=500))
    assert _shape(tree.root) == {"N0": {"down": {"N1": {"down": {"N2": {}}}}}}
    assert tree.root.downstream[0].downstream[0].node.expanded is False


def test_worked_example_tree():
    # Spec §6.1: the constrained chain of the Action Against Hunger activity.
    fixture = load_chain_fixture("worked_example")
    seed = "GB-CHC-1047501-23SOELNF"
    tree = to_tree(build_chain(FakeLinkSource.from_raw(*fixture["docs"]), seed, "constrained", LIMITS))
    assert _shape(tree.root) == {
        seed: {
            "up": {"GB-GOV-1-300978-408": {"up": {"GB-GOV-1-300978": {}}}},
            "down": {"org:US-EIN-13-3327220": {}},
        }
    }
    assert tree.extra_edges == ()


def test_chain_that_is_not_connected_is_rejected():
    def node(node_id: str, relation: str) -> ActivityNode:
        return ActivityNode(node_id, relation, None, False, True, None, None, None, None, None)

    chain = Chain("X", "constrained", (), (node("X", "seed"), node("LOOSE", "descendant")), (), ())
    with pytest.raises(ValueError, match="LOOSE"):
        to_tree(chain)
