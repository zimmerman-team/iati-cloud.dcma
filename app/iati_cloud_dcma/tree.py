"""The tree format: a spanning tree of a Chain, rooted at the seed.

A pure function of the Chain, so the graph and tree formats always describe the same result. Every node appears
once; edges the tree does not use are returned beside it, so no link is lost.
"""

from collections import defaultdict, deque
from dataclasses import dataclass

from app.iati_cloud_dcma.chain import EDGE_KIND_ORDER, Chain, ChainEdge, Node
from app.iati_cloud_dcma.nodes import NodeId


@dataclass(frozen=True)
class TreeNode:
    node: Node
    upstream: tuple["TreeNode", ...]  # its funders
    downstream: tuple["TreeNode", ...]  # what it funds


@dataclass(frozen=True)
class Tree:
    root: TreeNode
    extra_edges: tuple[ChainEdge, ...]


@dataclass(frozen=True)
class _Hop:
    """One edge as seen from one of its ends."""

    edge: ChainEdge
    other: NodeId
    other_is_upstream: bool

    @property
    def order(self) -> tuple[int, NodeId, bool]:
        """Visiting order of a node's neighbours: strongest kind of evidence first, then node ID, so the same chain
        always gives the same tree."""
        return (EDGE_KIND_ORDER.index(self.edge.kinds[0]), self.other, self.other_is_upstream)


def to_tree(chain: Chain) -> Tree:
    """The spanning tree found by a breadth-first search from the seed over the chain's edges, ignoring direction.

    Each node hangs under the node that reached it first, in `upstream` or `downstream` according to the direction of
    the edge between them. Raises ValueError when a node cannot be reached from the seed: a chain is always connected.
    """
    hops: dict[NodeId, list[_Hop]] = defaultdict(list)
    for edge in chain.edges:
        hops[edge.upstream].append(_Hop(edge, edge.downstream, other_is_upstream=False))
        hops[edge.downstream].append(_Hop(edge, edge.upstream, other_is_upstream=True))

    children: dict[NodeId, list[_Hop]] = defaultdict(list)
    placed = {chain.seed}
    queue = deque([chain.seed])
    while queue:
        current = queue.popleft()
        for hop in sorted(hops[current], key=lambda h: h.order):
            if hop.other not in placed:
                placed.add(hop.other)
                children[current].append(hop)
                queue.append(hop.other)

    nodes = {node.id: node for node in chain.nodes}
    loose = sorted(set(nodes) - placed)
    if loose:
        raise ValueError(f"nodes not connected to the seed: {', '.join(loose)}")
    used = {id(hop.edge) for placed_hops in children.values() for hop in placed_hops}
    extra = sorted((e for e in chain.edges if id(e) not in used), key=lambda e: (e.upstream, e.downstream))
    return Tree(root=_build(chain.seed, nodes, children), extra_edges=tuple(extra))


def _build(node_id: NodeId, nodes: dict[NodeId, Node], children: dict[NodeId, list[_Hop]]) -> TreeNode:
    """The subtree under one node. Depth is bounded by the traversal's max_depth, so recursion is safe."""
    hops = children.get(node_id, [])
    return TreeNode(
        node=nodes[node_id],
        upstream=tuple(_build(hop.other, nodes, children) for hop in hops if hop.other_is_upstream),
        downstream=tuple(_build(hop.other, nodes, children) for hop in hops if not hop.other_is_upstream),
    )
