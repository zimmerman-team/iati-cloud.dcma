"""The result of a traversal: nodes, merged edges and associations.

The types are immutable and hold identifiers as they are shown to callers (published spellings), not node keys.
The helpers are pure functions over finished edge sets.
"""

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.iati_cloud_dcma.links import Association, AssociationRole, Edge, EdgeKind
from app.iati_cloud_dcma.nodes import Identification, NodeId

# Types and constants
Mode = Literal["full", "constrained"]
MODES: tuple[Mode, ...] = ("full", "constrained")
Relation = Literal["seed", "ancestor", "descendant", "related", "co-funder"]

TruncationReason = Literal["max_depth", "max_nodes", "mention_limit", "time_budget"]
TRUNCATION_REASONS: tuple[TruncationReason, ...] = ("max_depth", "max_nodes", "mention_limit", "time_budget")

# Order of kinds inside a merged edge, and the tie-break order of the tree format.
EDGE_KIND_ORDER: tuple[EdgeKind, ...] = ("hierarchy", "transaction", "participating-org")


@dataclass(frozen=True)
class ActivityNode:
    id: NodeId
    relation: Relation
    distance: int | None
    expanded: bool
    published: bool
    title: str | None
    reporting_org_ref: str | None
    reporting_org_name: str | None
    hierarchy: int | None
    activity_status: str | None


@dataclass(frozen=True)
class OrganisationNode:
    id: NodeId
    relation: Relation
    distance: int | None
    org_ref: str | None  # None for a name-only organisation
    name: str | None
    identification: Identification
    expanded: bool = False  # organisation nodes are never searched further


Node = ActivityNode | OrganisationNode


@dataclass(frozen=True)
class ChainEdge:
    """All evidence for one (upstream, downstream) pair."""

    upstream: NodeId
    downstream: NodeId
    kinds: tuple[EdgeKind, ...]
    declared_by: tuple[str, ...]


@dataclass(frozen=True)
class ChainAssociation:
    activities: tuple[str, str]
    role: AssociationRole
    declared_by: tuple[str, ...]


@dataclass(frozen=True)
class Chain:
    seed: str
    mode: Mode
    truncation_reasons: tuple[TruncationReason, ...]
    nodes: tuple[Node, ...]
    edges: tuple[ChainEdge, ...]
    associations: tuple[ChainAssociation, ...]

    @property
    def truncated(self) -> bool:
        return bool(self.truncation_reasons)

    @property
    def node_ids(self) -> frozenset[NodeId]:
        return frozenset(node.id for node in self.nodes)


def merge_edges(edges: Iterable[Edge]) -> tuple[ChainEdge, ...]:
    """Merge edges by (upstream, downstream): the union of kinds (in EDGE_KIND_ORDER) and of declarers (sorted)."""
    kinds: dict[tuple[NodeId, NodeId], set[EdgeKind]] = defaultdict(set)
    declarers: dict[tuple[NodeId, NodeId], set[str]] = defaultdict(set)
    for edge in edges:
        pair = (edge.upstream, edge.downstream)
        kinds[pair].add(edge.kind)
        declarers[pair].add(edge.declared_by)
    return tuple(
        ChainEdge(
            upstream=upstream,
            downstream=downstream,
            kinds=tuple(kind for kind in EDGE_KIND_ORDER if kind in kinds[(upstream, downstream)]),
            declared_by=tuple(sorted(declarers[(upstream, downstream)])),
        )
        for upstream, downstream in sorted(kinds)
    )


def merge_associations(associations: Iterable[Association]) -> tuple[ChainAssociation, ...]:
    """Merge associations by (pair, role), keeping the union of declarers (sorted)."""
    declarers: dict[tuple[tuple[str, str], AssociationRole], set[str]] = defaultdict(set)
    for association in associations:
        declarers[(association.activities, association.role)].add(association.declared_by)
    return tuple(
        ChainAssociation(activities=activities, role=role, declared_by=tuple(sorted(declarers[(activities, role)])))
        for activities, role in sorted(declarers)
    )


def distances_from(seed: NodeId, edges: Iterable[ChainEdge]) -> dict[NodeId, int]:
    """Shortest number of hops from the seed over the edges, ignoring direction."""
    neighbours: dict[NodeId, set[NodeId]] = defaultdict(set)
    for edge in edges:
        neighbours[edge.upstream].add(edge.downstream)
        neighbours[edge.downstream].add(edge.upstream)
    distances = {seed: 0}
    queue = deque([seed])
    while queue:
        node = queue.popleft()
        for other in neighbours[node]:
            if other not in distances:
                distances[other] = distances[node] + 1
                queue.append(other)
    return distances


def reachable_downward(start: NodeId, edges: Iterable[ChainEdge], dead_ends: frozenset[NodeId]) -> frozenset[NodeId]:
    """Every node reachable from `start` by following edges from funder to funded, excluding `start` itself.

    `dead_ends` (organisation nodes) can be reached but are not passed through: an organisation that one activity
    pays and another names as funder does not put the second below the first.
    """
    children: dict[NodeId, set[NodeId]] = defaultdict(set)
    for edge in edges:
        if edge.upstream not in dead_ends:
            children[edge.upstream].add(edge.downstream)
    reached: set[NodeId] = set()
    queue = deque([start])
    while queue:
        for child in children[queue.popleft()]:
            if child not in reached and child != start:
                reached.add(child)
                queue.append(child)
    return frozenset(reached)
