"""Builds the delivery chain around a seed: walks up to its funders and down to what they fund, within the limits.

Links are declared by either end (a funder may name its recipient, a recipient may name its funder), so each step
both fetches an activity's own links and looks up the activities that mention it. Everything learned during one
request is kept in a request-scoped cache (_Knowledge), so each identifier is fetched once and looked up once per
spelling. The walks work in node keys (see links.py); the result is an
immutable Chain in the spellings shown to callers.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from app.iati_cloud_dcma.budget import UNLIMITED, Budget, BudgetExhausted
from app.iati_cloud_dcma.chain import (
    MODES,
    TRUNCATION_REASONS,
    ActivityNode,
    Chain,
    Mode,
    Node,
    OrganisationNode,
    Relation,
    TruncationReason,
    distances_from,
    merge_associations,
    merge_edges,
    reachable_downward,
)
from app.iati_cloud_dcma.links import ActivityDoc, Association, Edge, Organisation
from app.iati_cloud_dcma.nodes import (
    ORG_PREFIX,
    Identification,
    NodeId,
    identification_of,
    is_org_node,
    node_key,
    normalise_id,
)
from app.iati_cloud_dcma.source import LinkSource

Step = Literal["up", "down", "co-funders"]
# A reverse lookup only finds an identifier under the spellings queried. When a walk turns up a new spelling of an
# identifier that was already looked up, the walks are repeated (from the request's cache, so only the new lookups
# cost queries). Spellings rarely differ, so this bound is generous.
MAX_WALK_ROUNDS = 3
OrgLevel = Literal["identified", "refs", "all"]
# Which organisation nodes each level of the `orgs` filter returns, from best identified to least.
ORG_LEVELS: dict[OrgLevel, frozenset[Identification]] = {
    "identified": frozenset({"identifier"}),
    "refs": frozenset({"identifier", "free-text"}),
    "all": frozenset({"identifier", "free-text", "name-only"}),
}


class SeedNotFound(LookupError):
    """The seed is not published and no published activity references it. Answered with 404."""


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
    orgs: OrgLevel = "identified",
) -> Chain:
    """The delivery chain around `seed`, which may be given in any letter case.

    In constrained mode: the seed, everything above it and everything below it. In full mode: everything above the seed,
    everything below any of those, and the direct funders of the activities found below (co-funders, not explored).

        Raises SeedNotFound (unknown, unreferenced seed), BudgetExhausted (budget gone before the seed could be fetched)
        and the source's SourceError subclasses.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    if orgs not in ORG_LEVELS:
        raise ValueError(f"unknown orgs level: {orgs!r}")
    spelling = normalise_id(seed)
    if spelling is None:
        raise ValueError("the seed is empty")
    seed_key = node_key(spelling)
    knowledge = _Knowledge(source, budget, mention_limit=limits.max_nodes)
    knowledge.note_spelling(seed_key, spelling)
    if not knowledge.fetch([seed_key]):
        raise BudgetExhausted("the time budget ran out before the seed could be fetched")
    if not knowledge.is_published(seed_key):
        knowledge.mention([seed_key])
        if not knowledge.is_referenced(seed_key) and knowledge.mention_complete(seed_key):
            raise SeedNotFound(spelling)
    for _round in range(MAX_WALK_ROUNDS):
        context = _Context(seed_key, limits, _Admission(limits.max_nodes, seed_key), ORG_LEVELS[orgs], knowledge)
        up = _walk(context, [seed_key], "up")
        if mode == "constrained":
            down = _walk(context, [seed_key], "down")
            co_funders = _Step.empty()
        else:
            down = _walk(context, sorted(up.activities), "down")
            co_funders = _co_funders(context, down.activities - up.activities, already=up.reached | down.reached)
        if not knowledge.look_up_new_spellings():
            break
    return _assemble(context, mode, up, down, co_funders)


class _Knowledge:
    """What one request has learned from the link source.

    `docs` holds one merged document per activity seen. A document that arrived through a reverse lookup holds the
    links of the matching copies only (an identifier can have several documents), so an activity's own links count as
    complete only once it has been fetched by identifier.
    """

    def __init__(self, source: LinkSource, budget: Budget, mention_limit: int):
        self._source = source
        self._budget = budget
        self._mention_limit = mention_limit
        self.docs: dict[NodeId, ActivityDoc] = {}
        self.reasons: set[TruncationReason] = set()  # why queries or walks stopped early
        self._fetched: set[NodeId] = set()
        self._mentioned: dict[NodeId, bool] = {}  # whether all reverse lookups of the key were complete
        self._queried: dict[NodeId, set[str]] = defaultdict(set)  # spellings already looked up, per key
        self._spellings: dict[NodeId, set[str]] = defaultdict(set)
        self._edges: dict[NodeId, set[Edge]] = defaultdict(set)

    def has_status(self, node: NodeId) -> bool:
        """Whether it is known that the activity is published or not."""
        return node in self.docs or node in self._fetched

    def is_published(self, node: NodeId) -> bool:
        return node in self.docs

    def note_spelling(self, node: NodeId, spelling: str) -> None:
        self._spellings[node].add(spelling)

    def spelling(self, node: NodeId) -> str:
        """How an activity is shown: as its publisher spells it, else the first (sorted) spelling others used, so the
        result is the same however the caller typed it."""
        doc = self.docs.get(node)
        return doc.iati_identifier if doc is not None else min(self._spellings[node])

    def fetch(self, ids: Iterable[NodeId]) -> bool:
        """Fetch the activities not fetched yet. False when the budget ran out first."""
        missing = sorted({i for i in ids if not is_org_node(i) and i not in self._fetched})
        if not missing:
            return True
        try:
            found = self._source.fetch([min(self._spellings[i]) for i in missing], budget=self._budget)
        except BudgetExhausted:
            self.reasons.add("time_budget")
            return False
        for doc in found.values():
            self._add(doc)
        self._fetched.update(missing)
        return True

    def mention(self, ids: Iterable[NodeId]) -> None:
        """Run the reverse lookup for identifiers that haven't had one, under every spelling seen so far."""
        self._look_up({i for i in ids if i not in self._mentioned})

    def look_up_new_spellings(self) -> bool:
        """Look up the spellings learned since an identifier's reverse lookup. False when there were none."""
        return self._look_up(set(self._mentioned))

    def _look_up(self, ids: set[NodeId]) -> bool:
        new = {i: self._spellings[i] - self._queried[i] for i in sorted(ids)}
        spellings = sorted({spelling for unqueried in new.values() for spelling in unqueried})
        if not spellings:
            return False
        result = self._source.mentioning(spellings, budget=self._budget, limit=self._mention_limit)
        for doc in result.docs:
            self._add(doc)
        for i, unqueried in new.items():
            if unqueried:
                self._queried[i] |= unqueried
                self._mentioned[i] = self._mentioned.get(i, True) and result.complete
        if result.stopped_by is not None:
            self.reasons.add(result.stopped_by)
        return True

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
        known = self.docs.get(doc.key)
        merged = doc if known is None else ActivityDoc.merge([known, doc])
        self.docs[merged.key] = merged
        self._spellings[merged.key].add(merged.iati_identifier)
        for key, spelling in merged.labels:
            self._spellings[key].add(spelling)
        for edge in merged.edges:
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
    org_identifications: frozenset[Identification]
    knowledge: _Knowledge

    def expandable(self, node: NodeId) -> bool:
        """Published activities, and the seed even when unpublished.

        An unpublished activity has no links of its own, but others may name it; for the seed, those are worth showing.
        """
        return node == self.seed or self.knowledge.is_published(node)

    def keep(self, neighbours: Iterable[NodeId]) -> list[NodeId]:
        """Leave out the organisations that the `orgs` level does not ask for."""
        return [n for n in neighbours if not is_org_node(n) or identification_of(n) in self.org_identifications]


# Steps


@dataclass(frozen=True)
class _Step:
    """What one step reached. `walked` holds the nodes whose neighbours it enumerated completely; `wanted` maps each
    enumerated node to all its neighbours in that step, whether or not they made it into the result."""

    reached: frozenset[NodeId]
    walked: frozenset[NodeId]
    wanted: Mapping[NodeId, frozenset[NodeId]]

    @property
    def activities(self) -> frozenset[NodeId]:
        return frozenset(n for n in self.reached if not is_org_node(n))

    @classmethod
    def empty(cls) -> "_Step":
        return cls(reached=frozenset(), walked=frozenset(), wanted={})


def _walk(context: _Context, starts: list[NodeId], direction: Literal["up", "down"]) -> _Step:
    """Breadth-first walk from `starts` in one direction, up to max_depth hops.

    Organisations end the walk where they appear, as do unpublished activities other than the seed.
    """
    knowledge = context.knowledge
    seen: set[NodeId] = set(starts)
    walked: set[NodeId] = set()
    wanted: dict[NodeId, frozenset[NodeId]] = {}
    frontier = sorted(starts)
    for depth in range(context.limits.max_depth + 1):
        if not frontier or not knowledge.fetch(frontier):
            break
        expandable = [n for n in frontier if context.expandable(n)]
        if depth == context.limits.max_depth:
            if expandable:
                knowledge.reasons.add("max_depth")
            break
        knowledge.mention(expandable)
        next_frontier: list[NodeId] = []
        for node in expandable:
            neighbours = context.keep(knowledge.neighbours(node, direction))
            wanted[node] = frozenset(neighbours)
            if knowledge.mention_complete(node):
                walked.add(node)
            _admit_new(context, neighbours, seen, next_frontier)
        frontier = sorted(next_frontier)
    return _Step(reached=frozenset(seen), walked=frozenset(walked), wanted=wanted)


def _admit_new(context: _Context, neighbours: list[NodeId], seen: set[NodeId], frontier: list[NodeId]) -> None:
    """Admit unseen neighbours; activities join the next frontier. max_nodes may refuse some (see _Admission)."""
    for other in neighbours:
        if other not in seen and context.admission.admit(other):
            seen.add(other)
            if not is_org_node(other):
                frontier.append(other)


def _co_funders(context: _Context, below: frozenset[NodeId], already: frozenset[NodeId]) -> _Step:
    """Full mode: the other funders of the activities found below, one hop up and not explored further.

    `below` are the activities found going down that are not ancestors; `already` is everything the walks reached.
    """
    seen: set[NodeId] = set(already)
    activities: list[NodeId] = []
    wanted: dict[NodeId, frozenset[NodeId]] = {}
    for node in sorted(below):
        upstream = context.keep(context.knowledge.neighbours(node, "up"))
        wanted[node] = frozenset(upstream)
        _admit_new(context, upstream, seen, activities)
    context.knowledge.fetch(activities)  # metadata only: co-funders are never expanded
    return _Step(reached=frozenset(seen - already), walked=below, wanted=wanted)


# Assembly


def _assemble(context: _Context, mode: Mode, up: _Step, down: _Step, co_funders: _Step) -> Chain:
    """Turn what the walks reached into a Chain: choose the nodes, then translate keys into shown identifiers."""
    knowledge = context.knowledge
    node_ids = frozenset(n for n in context.admission.admitted if is_org_node(n) or knowledge.has_status(n))
    organisations = _organisations(knowledge.docs.values())
    shown = {n: _shown_id(n, knowledge, organisations) for n in node_ids}
    edges = merge_edges(
        Edge(shown[edge.upstream], shown[edge.downstream], edge.kind, shown[edge.declared_by])
        for doc in knowledge.docs.values()
        for edge in doc.edges
        if {edge.upstream, edge.downstream} <= node_ids
    )
    linked = {frozenset((edge.upstream, edge.downstream)) for edge in edges}
    associations = merge_associations(
        Association(pair, a.role, shown[a.declared_by])
        for doc in knowledge.docs.values()
        for a in doc.associations
        if set(a.activities) <= node_ids
        # skip associations between activities that an edge already joins, so one link is not shown twice
        for pair in [_sorted_pair(shown[a.activities[0]], shown[a.activities[1]])]
        if frozenset(pair) not in linked
    )
    seed = shown[context.seed]
    distances = distances_from(seed, edges)
    below_seed = reachable_downward(seed, edges, frozenset(shown[n] for n in node_ids if is_org_node(n)))
    expanded = _expanded(mode, up, down, co_funders, node_ids)
    nodes = tuple(
        sorted(
            (
                _node(
                    n,
                    shown[n],
                    _relation(n, context.seed, mode, up, down, shown[n] in below_seed),
                    distances,
                    expanded,
                    knowledge,
                    organisations,
                )
                for n in node_ids
            ),
            key=lambda node: (node.distance if node.distance is not None else len(node_ids), node.id),
        )
    )
    reasons = set(knowledge.reasons)
    if context.admission.refused:
        reasons.add("max_nodes")
    return Chain(seed, mode, tuple(r for r in TRUNCATION_REASONS if r in reasons), nodes, edges, associations)


def _sorted_pair(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first <= second else (second, first)


def _shown_id(node: NodeId, knowledge: _Knowledge, organisations: Mapping[NodeId, Organisation]) -> str:
    """The identifier a node is shown with: published spellings for activities and refs, the key for name-only."""
    if not is_org_node(node):
        return knowledge.spelling(node)
    ref = organisations[node].ref
    return f"{ORG_PREFIX}{ref}" if ref is not None else node


def _relation(node: NodeId, seed: NodeId, mode: Mode, up: _Step, down: _Step, below_seed: bool) -> Relation:
    """How a node relates to the seed. A node reached several ways gets the first that applies: seed, ancestor,
    descendant, related, co-funder."""
    if node == seed:
        return "seed"
    if node in up.reached:
        return "ancestor"
    if mode == "constrained" or below_seed:
        return "descendant"
    if node in down.reached:
        return "related"
    return "co-funder"


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


def _organisations(docs: Iterable[ActivityDoc]) -> dict[NodeId, Organisation]:
    """One record per organisation node: the first ref spelling and the first non-empty name, reading documents in
    identifier order."""
    records: dict[NodeId, Organisation] = {}
    for doc in sorted(docs, key=lambda d: d.key):
        for organisation in sorted(doc.organisations, key=lambda o: (o.node_id, o.ref or "", o.name or "")):
            known = records.get(organisation.node_id)
            if known is None:
                records[organisation.node_id] = organisation
            elif known.name is None and organisation.name is not None:
                records[organisation.node_id] = Organisation(known.node_id, known.ref, organisation.name)
    return records


def _node(
    node_id: NodeId,
    shown_id: str,
    relation: Relation,
    distances: Mapping[str, int],
    expanded: set[NodeId],
    knowledge: _Knowledge,
    organisations: Mapping[NodeId, Organisation],
) -> Node:
    distance = distances.get(shown_id)
    if is_org_node(node_id):
        record = organisations[node_id]
        return OrganisationNode(shown_id, relation, distance, record.ref, record.name, record.identification)
    doc = knowledge.docs.get(node_id)
    return ActivityNode(
        id=shown_id,
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
