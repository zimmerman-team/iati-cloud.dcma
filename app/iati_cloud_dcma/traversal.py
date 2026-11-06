"""Recursive retrieval: up- and down-walks from a seed, the two modes, limits and relations (spec §6).

Everything learned during one request lives in a private, request-scoped cache (_Knowledge), so each document is
fetched at most once and each identifier's reverse lookup runs at most once. The walks return immutable summaries,
and the result is an immutable Chain.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from app.iati_cloud_dcma.budget import UNLIMITED, Budget, BudgetExhausted
from app.iati_cloud_dcma.chain import (
    MODES,
    ActivityNode,
    Chain,
    Mode,
    Node,
    OrganisationNode,
    Relation,
    distances_from,
    merge_associations,
    merge_edges,
    reachable_downward,
)
from app.iati_cloud_dcma.links import ActivityDoc, Edge, NodeId, is_org_node, is_structured_ref, org_ref_of
from app.iati_cloud_dcma.source import LinkSource

Step = Literal["up", "down", "co-funders"]


class SeedNotFound(LookupError):
    """The seed is not published and no published activity references it (spec §7.3: 404)."""


@dataclass(frozen=True)
class Limits:
    max_depth: int
    max_nodes: int

    def __post_init__(self) -> None:
        if self.max_depth < 1 or self.max_nodes < 1:
            raise ValueError("max_depth and max_nodes must be positive")


def build_chain(
    source: LinkSource,
    seed: str,
    mode: Mode,
    limits: Limits,
    *,
    budget: Budget = UNLIMITED,
    include_unstructured_orgs: bool = False,
) -> Chain:
    """The delivery chain around `seed` (spec §6.2).

    Raises SeedNotFound (unknown, unreferenced seed), BudgetExhausted (budget gone before the seed could be fetched)
    and the source's SourceError subclasses.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    knowledge = _Knowledge(source, budget, mention_limit=limits.max_nodes)
    if not knowledge.fetch([seed]):
        raise BudgetExhausted("the time budget ran out before the seed could be fetched")
    if not knowledge.is_published(seed):
        knowledge.mention([seed])
        if not knowledge.is_referenced(seed) and knowledge.mention_complete(seed):
            raise SeedNotFound(seed)
    context = _Context(seed, limits, _Admission(limits.max_nodes, seed), include_unstructured_orgs, knowledge)

    up = _walk(context, [seed], "up")
    if mode == "constrained":
        down = _walk(context, [seed], "down")
        co_funders = _Step.empty()
    else:
        down = _walk(context, sorted(up.activities), "down")
        co_funders = _co_funders(context, down.activities - up.activities, already=up.reached | down.reached)
    return _assemble(context, mode, up, down, co_funders)


# --- Request-scoped knowledge ---------------------------------------------------------------------------------------


class _Knowledge:
    """What one request has learned from the link source. Each document is fetched, and each identifier's reverse
    lookup run, at most once."""

    def __init__(self, source: LinkSource, budget: Budget, mention_limit: int):
        self._source = source
        self._budget = budget
        self._mention_limit = mention_limit
        self.docs: dict[str, ActivityDoc] = {}
        self.unpublished: set[str] = set()
        self._mentioned: dict[str, bool] = {}  # identifier → was its reverse lookup complete?
        self._edges: dict[NodeId, set[Edge]] = defaultdict(set)
        self.cut_short = False  # the budget or the mention limit stopped a query

    def has_status(self, node: NodeId) -> bool:
        return node in self.docs or node in self.unpublished

    def is_published(self, node: NodeId) -> bool:
        return node in self.docs

    def fetch(self, ids: Iterable[NodeId]) -> bool:
        """Fetch the documents not seen yet. False when the budget ran out first."""
        missing = sorted({i for i in ids if not is_org_node(i) and not self.has_status(i)})
        if not missing:
            return True
        try:
            found = self._source.fetch(missing, budget=self._budget)
        except BudgetExhausted:
            self.cut_short = True
            return False
        for doc in found.values():
            self._add(doc)
        self.unpublished.update(i for i in missing if i not in found)
        return True

    def mention(self, ids: Iterable[NodeId]) -> None:
        """Run the reverse lookup for identifiers that haven't had one."""
        todo = sorted({i for i in ids if i not in self._mentioned})
        if not todo:
            return
        result = self._source.mentioning(todo, budget=self._budget, limit=self._mention_limit)
        for doc in result.docs:
            self._add(doc)
        self._mentioned.update((i, result.complete) for i in todo)
        self.cut_short = self.cut_short or not result.complete

    def mention_complete(self, node: NodeId) -> bool:
        return self._mentioned.get(node, False)

    def is_referenced(self, node: NodeId) -> bool:
        return any(node in doc.linked_activity_ids for doc in self.docs.values())

    def neighbours(self, node: NodeId, direction: Literal["up", "down"]) -> list[NodeId]:
        """Known neighbours in one direction, activities and organisations, sorted."""
        if direction == "up":
            return sorted({e.upstream for e in self._edges[node] if e.downstream == node})
        return sorted({e.downstream for e in self._edges[node] if e.upstream == node})

    def _add(self, doc: ActivityDoc) -> None:
        if doc.iati_identifier in self.docs:
            return
        self.docs[doc.iati_identifier] = doc
        for edge in doc.edges:
            self._edges[edge.upstream].add(edge)
            self._edges[edge.downstream].add(edge)


class _Admission:
    """The max_nodes limit, shared by every step of one request. The seed is admitted first."""

    def __init__(self, max_nodes: int, seed: NodeId):
        self._max_nodes = max_nodes
        self.admitted: dict[NodeId, None] = {seed: None}  # insertion-ordered set
        self.refused = False

    def admit(self, node: NodeId) -> bool:
        if node in self.admitted:
            return True
        if len(self.admitted) >= self._max_nodes:
            self.refused = True
            return False
        self.admitted[node] = None
        return True


@dataclass(frozen=True)
class _Context:
    seed: NodeId
    limits: Limits
    admission: _Admission
    include_unstructured_orgs: bool
    knowledge: _Knowledge

    def expandable(self, node: NodeId) -> bool:
        """Published activities, and the seed even when unpublished (decision Q8)."""
        return node == self.seed or self.knowledge.is_published(node)

    def keep(self, neighbours: Iterable[NodeId]) -> list[NodeId]:
        """Leave out organisations with free-text refs unless they were asked for (decision Q9)."""
        if self.include_unstructured_orgs:
            return list(neighbours)
        return [n for n in neighbours if not is_org_node(n) or is_structured_ref(org_ref_of(n))]


# --- Steps ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    """What one step reached. `walked` holds the nodes whose neighbours it enumerated completely; `wanted` maps each
    enumerated node to all its neighbours in that step, whether or not they made it into the result."""

    reached: frozenset[NodeId]
    walked: frozenset[NodeId]
    wanted: Mapping[NodeId, frozenset[NodeId]]
    truncated: bool

    @property
    def activities(self) -> frozenset[NodeId]:
        return frozenset(n for n in self.reached if not is_org_node(n))

    @classmethod
    def empty(cls) -> "_Step":
        return cls(reached=frozenset(), walked=frozenset(), wanted={}, truncated=False)


def _walk(context: _Context, starts: list[NodeId], direction: Literal["up", "down"]) -> _Step:
    """Breadth-first walk from `starts` in one direction, up to max_depth hops (spec §6.1)."""
    knowledge = context.knowledge
    seen: set[NodeId] = set(starts)
    walked: set[NodeId] = set()
    wanted: dict[NodeId, frozenset[NodeId]] = {}
    truncated = False
    frontier = sorted(starts)
    for depth in range(context.limits.max_depth + 1):
        if not frontier:
            break
        if not knowledge.fetch(frontier):
            truncated = True
            break
        expandable = [n for n in frontier if context.expandable(n)]
        if depth == context.limits.max_depth:
            truncated = truncated or bool(expandable)
            break
        knowledge.mention(expandable)
        next_frontier: list[NodeId] = []
        for node in expandable:
            neighbours = context.keep(knowledge.neighbours(node, direction))
            wanted[node] = frozenset(neighbours)
            if knowledge.mention_complete(node):
                walked.add(node)
            truncated = truncated or not knowledge.mention_complete(node)
            truncated = _admit_new(context, neighbours, seen, next_frontier) or truncated
        frontier = sorted(next_frontier)
    return _Step(reached=frozenset(seen), walked=frozenset(walked), wanted=wanted, truncated=truncated)


def _admit_new(context: _Context, neighbours: list[NodeId], seen: set[NodeId], frontier: list[NodeId]) -> bool:
    """Admit unseen neighbours; activities join the next frontier. True when max_nodes refused one."""
    refused = False
    for other in neighbours:
        if other in seen:
            continue
        if not context.admission.admit(other):
            refused = True
            continue
        seen.add(other)
        if not is_org_node(other):
            frontier.append(other)
    return refused


def _co_funders(context: _Context, below: frozenset[NodeId], already: frozenset[NodeId]) -> _Step:
    """Full mode: direct upstream neighbours of the nodes in D that are not in A ∪ D, unexpanded (spec §6.2)."""
    seen: set[NodeId] = set(already)
    activities: list[NodeId] = []
    wanted: dict[NodeId, frozenset[NodeId]] = {}
    truncated = False
    for node in sorted(below):
        upstream = context.keep(context.knowledge.neighbours(node, "up"))
        wanted[node] = frozenset(upstream)
        truncated = _admit_new(context, upstream, seen, activities) or truncated
    if not context.knowledge.fetch(activities):  # metadata only: co-funders are never expanded
        truncated = True
    return _Step(reached=frozenset(seen - already), walked=below, wanted=wanted, truncated=truncated)


# --- Assembly -------------------------------------------------------------------------------------------------------


def _assemble(context: _Context, mode: Mode, up: _Step, down: _Step, co_funders: _Step) -> Chain:
    knowledge = context.knowledge
    node_ids = frozenset(n for n in context.admission.admitted if is_org_node(n) or knowledge.has_status(n))
    edges = merge_edges(
        edge for doc in knowledge.docs.values() for edge in doc.edges if {edge.upstream, edge.downstream} <= node_ids
    )
    associations = merge_associations(
        a for doc in knowledge.docs.values() for a in doc.associations if set(a.activities) <= node_ids
    )
    distances = distances_from(context.seed, edges)
    relations = _relations(context.seed, mode, up, down, reachable_downward(context.seed, edges), node_ids)
    expanded = _expanded(mode, up, down, co_funders, node_ids)
    org_names = _organisation_names(knowledge.docs.values())
    nodes = tuple(
        sorted(
            (_node(n, relations[n], distances.get(n), expanded, knowledge, org_names) for n in node_ids),
            key=lambda node: (node.distance if node.distance is not None else len(node_ids), node.id),
        )
    )
    truncated = (
        up.truncated
        or down.truncated
        or co_funders.truncated
        or context.admission.refused
        or knowledge.cut_short
        or len(node_ids) < len(context.admission.admitted)
    )
    return Chain(context.seed, mode, truncated, nodes, edges, associations)


def _relations(
    seed: NodeId, mode: Mode, up: _Step, down: _Step, below_seed: frozenset[NodeId], node_ids: frozenset[NodeId]
) -> dict[NodeId, Relation]:
    """One relation per node, by precedence: seed, ancestor, descendant, related, co-funder (spec §6.4)."""

    def relation(node: NodeId) -> Relation:
        if node == seed:
            return "seed"
        if node in up.reached:
            return "ancestor"
        if mode == "constrained" or node in below_seed:
            return "descendant"
        if node in down.reached:
            return "related"
        return "co-funder"

    return {node: relation(node) for node in node_ids}


def _expanded(mode: Mode, up: _Step, down: _Step, co_funders: _Step, node_ids: frozenset[NodeId]) -> set[NodeId]:
    """Nodes that every step they belong to enumerated completely, with all their neighbours in the result."""
    required: dict[NodeId, set[Step]] = defaultdict(set)
    for node in up.activities:
        required[node].add("up")
    for node in down.activities:
        required[node].add("down")
    if mode == "full":
        for node in down.activities - up.activities:
            required[node].add("co-funders")
    walked: dict[Step, frozenset[NodeId]] = {"up": up.walked, "down": down.walked, "co-funders": co_funders.walked}
    wanted: dict[NodeId, set[NodeId]] = defaultdict(set)
    for step in (up, down, co_funders):
        for node, neighbours in step.wanted.items():
            wanted[node] |= neighbours
    return {
        node
        for node, steps in required.items()
        if node in node_ids and all(node in walked[s] for s in steps) and wanted[node] <= node_ids
    }


def _organisation_names(docs: Iterable[ActivityDoc]) -> dict[str, str | None]:
    """First non-empty name per organisation ref, reading documents in identifier order."""
    names: dict[str, str | None] = {}
    for doc in sorted(docs, key=lambda d: d.iati_identifier):
        for organisation in doc.organisations:
            if names.get(organisation.ref) is None:
                names[organisation.ref] = organisation.name
    return names


def _node(
    node_id: NodeId,
    relation: Relation,
    distance: int | None,
    expanded: set[NodeId],
    knowledge: _Knowledge,
    org_names: Mapping[str, str | None],
) -> Node:
    if is_org_node(node_id):
        ref = org_ref_of(node_id)
        return OrganisationNode(node_id, relation, distance, ref, org_names.get(ref), is_structured_ref(ref))
    doc = knowledge.docs.get(node_id)
    return ActivityNode(
        id=node_id,
        relation=relation,
        distance=distance,
        expanded=node_id in expanded,
        published=doc is not None,
        title=doc.title if doc else None,
        reporting_org_ref=doc.reporting_org_ref if doc else None,
        reporting_org_name=doc.reporting_org_name if doc else None,
        hierarchy=doc.hierarchy if doc else None,
        activity_status=doc.activity_status if doc else None,
    )
