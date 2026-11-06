"""Link extraction: one Solr activity document → edges (L1–L6, O1–O2), associations (L7) and organisations.

Everything here is pure: no Solr access, no shared state. See spec §5.2, §5.3 and §5.5.
"""

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from itertools import chain
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Solr fields read from each activity document (spec §5.6).
FIELD_IDENTIFIER = "iati-identifier"
FIELD_TITLE = "title.narrative"
FIELD_REPORTING_ORG_REF = "reporting-org.ref"
FIELD_REPORTING_ORG_NAME = "reporting-org.narrative"
FIELD_HIERARCHY = "hierarchy"
FIELD_ACTIVITY_STATUS = "activity-status.code"
FIELD_RELATED_ACTIVITY = "json.related-activity"
FIELD_TRANSACTION = "json.transaction"
FIELD_PARTICIPATING_ORG = "json.participating-org"

SOLR_FIELDS: tuple[str, ...] = (
    FIELD_IDENTIFIER,
    FIELD_TITLE,
    FIELD_REPORTING_ORG_REF,
    FIELD_REPORTING_ORG_NAME,
    FIELD_HIERARCHY,
    FIELD_ACTIVITY_STATUS,
    FIELD_RELATED_ACTIVITY,
    FIELD_TRANSACTION,
    FIELD_PARTICIPATING_ORG,
)

NodeId = str  # activity: IATI identifier; organisation: "org:<ref>"
EdgeKind = Literal["hierarchy", "transaction", "participating-org"]
AssociationRole = Literal[2, 3]

ORG_PREFIX = "org:"

_RELATED_PARENT = 1
_RELATED_CHILD = 2
_ROLE_FUNDING = 1
_ROLE_IMPLEMENTING = 4
_ASSOCIATION_ROLES: dict[int, AssociationRole] = {2: 2, 3: 3}  # accountable, extending


class InvalidDocument(ValueError):
    """A Solr document without a usable iati-identifier. No links can be attributed to it."""


@dataclass(frozen=True)
class Edge:
    """A directed link upstream → downstream (funder → funded), declared by one activity."""

    upstream: NodeId
    downstream: NodeId
    kind: EdgeKind
    declared_by: str


@dataclass(frozen=True)
class Association:
    """A non-directional relation from participating-org role 2 or 3 (L7). Shown, never followed."""

    activities: tuple[str, str]  # sorted
    role: AssociationRole
    declared_by: str


@dataclass(frozen=True)
class Organisation:
    """An organisation named by ref in a transaction that gives no activity ID (O1, O2)."""

    ref: str
    name: str | None

    @property
    def node_id(self) -> NodeId:
        return org_node_id(self.ref)

    @property
    def structured_ref(self) -> bool:
        """False when the ref contains whitespace: in practice a name or a note typed into @ref, not an identifier."""
        return not any(char.isspace() for char in self.ref)


@dataclass(frozen=True)
class Links:
    """Everything one activity document declares about its links."""

    edges: frozenset[Edge] = frozenset()
    associations: frozenset[Association] = frozenset()
    organisations: frozenset[Organisation] = frozenset()


@dataclass(frozen=True)
class ActivityDoc:
    """An activity's metadata and the links its own document declares."""

    iati_identifier: str
    title: str | None
    reporting_org_ref: str | None
    reporting_org_name: str | None
    hierarchy: int | None
    activity_status: str | None
    edges: frozenset[Edge]
    associations: frozenset[Association]
    organisations: frozenset[Organisation]

    @classmethod
    def from_solr(cls, raw_doc: Mapping[str, Any]) -> "ActivityDoc":
        """Build from a raw Solr document. Raises InvalidDocument when it has no usable identifier."""
        links = extract(raw_doc)
        return cls(
            iati_identifier=_require_identifier(raw_doc),
            title=_first_text(raw_doc.get(FIELD_TITLE)),
            reporting_org_ref=_reporting_org_ref(raw_doc),
            reporting_org_name=_first_text(raw_doc.get(FIELD_REPORTING_ORG_NAME)),
            hierarchy=_as_int(_first(raw_doc.get(FIELD_HIERARCHY))),
            activity_status=normalise_id(_first(raw_doc.get(FIELD_ACTIVITY_STATUS))),
            edges=links.edges,
            associations=links.associations,
            organisations=links.organisations,
        )

    @property
    def linked_activity_ids(self) -> frozenset[str]:
        """The activities at the other end of this document's edges and associations. Organisations are left out."""
        own = self.iati_identifier
        edge_ends = (edge.upstream if edge.downstream == own else edge.downstream for edge in self.edges)
        association_ends = (a for association in self.associations for a in association.activities if a != own)
        return frozenset(end for end in chain(edge_ends, association_ends) if not end.startswith(ORG_PREFIX))


def normalise_id(value: object) -> str | None:
    """Trim an identifier or ref; coerce numbers to str. Blank or non-scalar values give None."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip() or None


def org_node_id(ref: str) -> NodeId:
    return f"{ORG_PREFIX}{ref}"


def extract(raw_doc: Mapping[str, Any]) -> Links:
    """Extract L1–L7 and O1–O2 from one raw Solr activity document (spec §5.2, §5.3).

    Unparseable link values are logged and skipped. Raises InvalidDocument when the document has no identifier.
    """
    activity_id = _require_identifier(raw_doc)
    related = _json_objects(raw_doc, FIELD_RELATED_ACTIVITY, activity_id)
    transactions = _json_objects(raw_doc, FIELD_TRANSACTION, activity_id)
    participating = _json_objects(raw_doc, FIELD_PARTICIPATING_ORG, activity_id)

    org_mentions = list(_org_mentions(transactions, activity_id, _reporting_org_ref(raw_doc)))
    edges = chain(
        _related_edges(related, activity_id),
        _transaction_edges(transactions, activity_id),
        _participating_edges(participating, activity_id),
        (edge for edge, _ref, _name in org_mentions),
    )
    return Links(
        edges=frozenset(edges),
        associations=frozenset(_associations(participating, activity_id)),
        organisations=_organisations(org_mentions),
    )


# --- Document-level helpers ---------------------------------------------------------------------------------------


def _require_identifier(raw_doc: Mapping[str, Any]) -> str:
    identifier = normalise_id(raw_doc.get(FIELD_IDENTIFIER))
    if identifier is None:
        raise InvalidDocument(f"document has no usable {FIELD_IDENTIFIER}: {raw_doc.get(FIELD_IDENTIFIER)!r:.100}")
    return identifier


def _reporting_org_ref(raw_doc: Mapping[str, Any]) -> str | None:
    return normalise_id(_first(raw_doc.get(FIELD_REPORTING_ORG_REF)))


def _json_objects(raw_doc: Mapping[str, Any], field: str, activity_id: str) -> list[dict[str, Any]]:
    """Parse each JSON-string value of a (possibly single-valued) field; skip and log values that aren't objects."""
    values = raw_doc.get(field)
    if values is None:
        return []
    objects = []
    for value in values if isinstance(values, list) else [values]:
        parsed = _parse_json_object(value)
        if parsed is None:
            logger.warning("Skipping unparseable %s value in activity %s: %.200r", field, activity_id, value)
        else:
            objects.append(parsed)
    return objects


def _parse_json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _first(value: object) -> object:
    """The first element of a multi-valued Solr field, or the value itself when it is single-valued."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _first_text(value: object) -> str | None:
    """The first non-blank string of a narrative, which may be a string or a list of strings."""
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _directed(other: NodeId, activity_id: str, other_is_upstream: bool, kind: EdgeKind) -> Edge:
    if other_is_upstream:
        return Edge(upstream=other, downstream=activity_id, kind=kind, declared_by=activity_id)
    return Edge(upstream=activity_id, downstream=other, kind=kind, declared_by=activity_id)


# --- L1/L2: related-activity --------------------------------------------------------------------------------------


def _related_edges(relations: Iterable[dict[str, Any]], activity_id: str) -> Iterator[Edge]:
    """Type 1 (parent) and 2 (child) only. Siblings (3) and types 4/5 are ignored in the baseline (spec §5.2)."""
    for relation in relations:
        ref = normalise_id(relation.get("ref"))
        if ref is None or ref == activity_id:
            continue
        rel_type = _as_int(relation.get("type"))
        if rel_type == _RELATED_PARENT:
            yield _directed(ref, activity_id, True, "hierarchy")
        elif rel_type == _RELATED_CHILD:
            yield _directed(ref, activity_id, False, "hierarchy")


# --- L3/L4 and O1/O2: transactions --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Party:
    """One side of a transaction. The provider is upstream of the declaring activity; the receiver downstream."""

    key: str
    activity_key: str
    upstream: bool


_PROVIDER = _Party("provider-org", "provider-activity-id", upstream=True)
_RECEIVER = _Party("receiver-org", "receiver-activity-id", upstream=False)


def _parties(transactions: Iterable[dict[str, Any]]) -> Iterator[tuple[_Party, dict[str, Any]]]:
    """Each transaction's provider-org then receiver-org, in document order, where they are JSON objects."""
    for transaction in transactions:
        for party in (_PROVIDER, _RECEIVER):
            org = transaction.get(party.key)
            if isinstance(org, dict):
                yield party, org


def _transaction_edges(transactions: Iterable[dict[str, Any]], activity_id: str) -> Iterator[Edge]:
    """L3/L4, whatever the transaction type."""
    for party, org in _parties(transactions):
        other = normalise_id(org.get(party.activity_key))
        if other is not None and other != activity_id:
            yield _directed(other, activity_id, party.upstream, "transaction")


def _org_mentions(
    transactions: Iterable[dict[str, Any]], activity_id: str, reporting_org_ref: str | None
) -> Iterator[tuple[Edge, str, str | None]]:
    """O1/O2: (edge, ref, narrative) for each party with a ref, no activity ID, and not the reporting org itself."""
    for party, org in _parties(transactions):
        if normalise_id(org.get(party.activity_key)) is not None:
            continue
        ref = normalise_id(org.get("ref"))
        if ref is None or ref == reporting_org_ref:
            continue
        edge = _directed(org_node_id(ref), activity_id, party.upstream, "transaction")
        yield edge, ref, _first_text(org.get("narrative"))


def _organisations(mentions: Iterable[tuple[Edge, str, str | None]]) -> frozenset[Organisation]:
    """One organisation per ref, named by the first non-blank narrative in document order."""
    names: dict[str, str | None] = {}
    for _edge, ref, name in mentions:
        if names.get(ref) is None:
            names[ref] = name
    return frozenset(Organisation(ref=ref, name=name) for ref, name in names.items())


# --- L5/L6/L7: participating-org ----------------------------------------------------------------------------------


def _participating_activities(orgs: Iterable[dict[str, Any]], activity_id: str) -> Iterator[tuple[int | None, str]]:
    """(role, activity-id) for each participating-org naming an activity other than this one."""
    for org in orgs:
        other = normalise_id(org.get("activity-id"))
        if other is not None and other != activity_id:
            yield _as_int(org.get("role")), other


def _participating_edges(orgs: Iterable[dict[str, Any]], activity_id: str) -> Iterator[Edge]:
    for role, other in _participating_activities(orgs, activity_id):
        if role == _ROLE_FUNDING:
            yield _directed(other, activity_id, True, "participating-org")
        elif role == _ROLE_IMPLEMENTING:
            yield _directed(other, activity_id, False, "participating-org")


def _associations(orgs: Iterable[dict[str, Any]], activity_id: str) -> Iterator[Association]:
    for role, other in _participating_activities(orgs, activity_id):
        association_role = _ASSOCIATION_ROLES.get(role) if role is not None else None
        if association_role is not None:
            first, second = sorted((activity_id, other))
            yield Association(activities=(first, second), role=association_role, declared_by=activity_id)
