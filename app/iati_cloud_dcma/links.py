"""Turns one IATI activity document from Solr into the links it declares.

Everything here is pure: no Solr calls, no state. Links are expressed in node keys (see `nodes`); the original
spellings travel along in `labels`, so callers can be shown what the publisher wrote.
"""

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import chain
from typing import Any, Literal

from app.iati_cloud_dcma.nodes import (
    Identification,
    NodeId,
    identification_of,
    is_org_node,
    node_key,
    normalise_id,
    normalise_name,
    org_name_node_id,
    org_node_id,
)

logger = logging.getLogger(__name__)

# Solr fields read from each activity document.
FIELD_DOC_ID = "id"
FIELD_LAST_UPDATED = "last-updated-datetime"
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
    FIELD_DOC_ID,
    FIELD_IDENTIFIER,
    FIELD_LAST_UPDATED,
    FIELD_TITLE,
    FIELD_REPORTING_ORG_REF,
    FIELD_REPORTING_ORG_NAME,
    FIELD_HIERARCHY,
    FIELD_ACTIVITY_STATUS,
    FIELD_RELATED_ACTIVITY,
    FIELD_TRANSACTION,
    FIELD_PARTICIPATING_ORG,
)

# Types
EdgeKind = Literal["hierarchy", "transaction", "participating-org"]
AssociationRole = Literal[2, 3]

_RELATED_PARENT = 1
_RELATED_CHILD = 2
_ROLE_FUNDING = 1
_ROLE_IMPLEMENTING = 4
_ASSOCIATION_ROLES: dict[int, AssociationRole] = {2: 2, 3: 3}  # accountable, extending
# Transactions that send money on: outgoing commitment, disbursement, expenditure and outgoing pledge. Only these say
# that the receiver is funded by the declaring activity; an incoming transaction's receiver is the activity itself.
OUTGOING_TRANSACTION_TYPES = frozenset({2, 3, 4, 12})


class InvalidDocument(ValueError):
    """A Solr document without a usable iati-identifier. No links can be attributed to it."""


@dataclass(frozen=True)
class Edge:
    """A directed link upstream to downstream (funder to funded), declared by one activity. All three are node keys."""

    upstream: NodeId
    downstream: NodeId
    kind: EdgeKind
    declared_by: NodeId


@dataclass(frozen=True)
class Association:
    """Two activities linked through a participating-org with role 2 (accountable) or 3 (extending).

    These say who is responsible for an activity, not who pays whom, so they have no direction. They are shown in the
    result but never walked.
    """

    activities: tuple[NodeId, NodeId]  # sorted
    role: AssociationRole
    declared_by: NodeId


@dataclass(frozen=True)
class Organisation:
    """An organisation named where no activity is named."""

    node_id: NodeId
    ref: str | None  # None for a name-only organisation
    name: str | None  # None for a ref-only organisation

    @property
    def identification(self) -> Identification:
        return identification_of(self.node_id)


@dataclass(frozen=True)
class Links:
    """Everything one activity declares about its links."""

    edges: frozenset[Edge] = frozenset()
    associations: frozenset[Association] = frozenset()
    organisations: frozenset[Organisation] = frozenset()
    labels: frozenset[tuple[NodeId, str]] = frozenset()


@dataclass(frozen=True)
class ActivityDoc:
    """An activity's metadata and the links it declares. One per identifier: see `merge`."""

    iati_identifier: str  # as published
    title: str | None
    reporting_org_ref: str | None
    reporting_org_name: str | None
    hierarchy: int | None
    activity_status: str | None
    edges: frozenset[Edge]
    associations: frozenset[Association]
    organisations: frozenset[Organisation]
    labels: frozenset[tuple[NodeId, str]] = frozenset()
    doc_id: str | None = None
    last_updated: str | None = None

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
            labels=links.labels,
            doc_id=normalise_id(_first(raw_doc.get(FIELD_DOC_ID))),
            last_updated=normalise_id(_first(raw_doc.get(FIELD_LAST_UPDATED))),
        )

    @classmethod
    def merge(cls, copies: Iterable["ActivityDoc"]) -> "ActivityDoc":
        """One document for several copies of the same identifier.

        Links are the union over all copies.
        Metadata comes from the copy updated last; a copy without a date counts as oldest,
        and ties go to the lowest document id.
        The result does not depend on the order of `copies`.
        """
        ordered = sorted(copies, key=lambda copy: (copy.doc_id or "", copy.iati_identifier))
        newest = max(ordered, key=lambda copy: _timestamp(copy.last_updated))  # max keeps the first of equals
        if len(ordered) == 1:
            return newest
        return replace(
            newest,
            edges=frozenset().union(*(copy.edges for copy in ordered)),
            associations=frozenset().union(*(copy.associations for copy in ordered)),
            organisations=frozenset().union(*(copy.organisations for copy in ordered)),
            labels=frozenset().union(*(copy.labels for copy in ordered)),
        )

    @property
    def key(self) -> NodeId:
        return node_key(self.iati_identifier)

    @property
    def linked_activity_ids(self) -> frozenset[NodeId]:
        """Keys of the activities at the other end of this document's edges and associations, without organisations."""
        own = self.key
        edge_ends = (edge.upstream if edge.downstream == own else edge.downstream for edge in self.edges)
        association_ends = (a for association in self.associations for a in association.activities if a != own)
        return frozenset(end for end in chain(edge_ends, association_ends) if not is_org_node(end))


def extract(raw_doc: Mapping[str, Any]) -> Links:
    """Extract links from one raw Solr activity document.

    Unparseable link values are logged and skipped. Raises InvalidDocument when the document has no identifier.
    """
    identifier = _require_identifier(raw_doc)
    own = _Own(
        key=node_key(identifier),
        org_ref=_fold_or_none(_reporting_org_ref(raw_doc)),
        org_name=_name_or_none(_first_text(raw_doc.get(FIELD_REPORTING_ORG_NAME))),
    )
    related = _json_objects(raw_doc, FIELD_RELATED_ACTIVITY, identifier)
    transactions = _json_objects(raw_doc, FIELD_TRANSACTION, identifier)
    participating = _json_objects(raw_doc, FIELD_PARTICIPATING_ORG, identifier)

    activity_links = list(
        chain(
            _related_links(related, own),
            _transaction_links(transactions, own),
            _participating_links(participating, own),
        )
    )
    org_links = list(chain(_transaction_org_links(transactions, own), _participating_org_links(participating, own)))
    return Links(
        edges=frozenset(chain((edge for edge, _spelling in activity_links), (edge for edge, _org in org_links))),
        associations=frozenset(association for association, _spelling in _associations(participating, own)),
        organisations=_organisations(org for _edge, org in org_links),
        labels=frozenset(
            (node_key(spelling), spelling)
            for spelling in chain(
                (spelling for _edge, spelling in activity_links),
                (spelling for _association, spelling in _associations(participating, own)),
            )
        ),
    )


# Document-level helpers
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


@dataclass(frozen=True)
class _Own:
    """The declaring activity: its key, and its reporting organisation for the "not itself" checks."""

    key: NodeId
    org_ref: str | None  # case-folded
    org_name: str | None  # normalised

    def is_reporting_org(self, ref: str | None, name: str | None) -> bool:
        """Whether an element names the reporting organisation. Compared by ref when both sides have one, else by name."""
        if ref is not None and self.org_ref is not None:
            return ref.casefold() == self.org_ref
        return name is not None and self.org_name is not None and normalise_name(name) == self.org_name


def _fold_or_none(value: str | None) -> str | None:
    return value.casefold() if value is not None else None


_OLDEST = datetime.min.replace(tzinfo=UTC)


def _timestamp(value: str | None) -> datetime:
    """A last-updated value as a moment in time, used to pick the newest copy of a duplicated identifier. Missing or
    unreadable values count as oldest."""
    try:
        moment = datetime.fromisoformat(value) if value is not None else _OLDEST
    except ValueError:
        return _OLDEST
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _name_or_none(value: str | None) -> str | None:
    return normalise_name(value) if value is not None else None


def _directed(other: NodeId, own: _Own, other_is_upstream: bool, kind: EdgeKind) -> Edge:
    if other_is_upstream:
        return Edge(upstream=other, downstream=own.key, kind=kind, declared_by=own.key)
    return Edge(upstream=own.key, downstream=other, kind=kind, declared_by=own.key)


def _other_activity(value: object, own: _Own) -> str | None:
    """The spelling of an activity identifier that names another activity than the declaring one, else None."""
    spelling = normalise_id(value)
    return spelling if spelling is not None and node_key(spelling) != own.key else None


# Related activities


def _related_links(relations: Iterable[dict[str, Any]], own: _Own) -> Iterator[tuple[Edge, str]]:
    """Parent and child links (types 1 and 2): a parent funds its children.

    Siblings (type 3) share a parent but don't fund each other. Co-funded (4) and third-party (5) relations are left out
    until it is decided whether they mean funding.
    """
    for relation in relations:
        other = _other_activity(relation.get("ref"), own)
        rel_type = _as_int(relation.get("type"))
        if other is not None and rel_type in (_RELATED_PARENT, _RELATED_CHILD):
            yield _directed(node_key(other), own, rel_type == _RELATED_PARENT, "hierarchy"), other


# Transactions


@dataclass(frozen=True)
class _Party:
    """One side of a transaction. The provider is upstream of the declaring activity; the receiver downstream."""

    key: str
    activity_key: str
    upstream: bool


_PROVIDER = _Party("provider-org", "provider-activity-id", upstream=True)
_RECEIVER = _Party("receiver-org", "receiver-activity-id", upstream=False)


def _transaction_type(transaction: Mapping[str, Any]) -> int | None:
    value = transaction.get("transaction-type")
    return _as_int(value.get("code")) if isinstance(value, dict) else _as_int(value)


def _parties(transactions: Iterable[dict[str, Any]]) -> Iterator[tuple[_Party, dict[str, Any]]]:
    """Each transaction's provider-org then receiver-org, in document order, where they are JSON objects.

    The provider always funds the declaring activity, whatever the transaction type. The receiver only counts on an
    outgoing transaction; on an incoming one, the receiver is the declaring activity itself.
    """
    for transaction in transactions:
        outgoing = _transaction_type(transaction) in OUTGOING_TRANSACTION_TYPES
        for party in (_PROVIDER, _RECEIVER):
            org = transaction.get(party.key)
            if isinstance(org, dict) and (party.upstream or outgoing):
                yield party, org


def _transaction_links(transactions: Iterable[dict[str, Any]], own: _Own) -> Iterator[tuple[Edge, str]]:
    """Links to the provider and receiver activities named in transactions."""
    for party, org in _parties(transactions):
        other = _other_activity(org.get(party.activity_key), own)
        if other is not None:
            yield _directed(node_key(other), own, party.upstream, "transaction"), other


def _transaction_org_links(transactions: Iterable[dict[str, Any]], own: _Own) -> Iterator[tuple[Edge, Organisation]]:
    """Links to transaction parties that name an organisation but no activity, so the chain ends there.

    The reporting organisation itself is skipped: it is the declaring activity, not a separate funder or recipient.
    """
    for party, org in _parties(transactions):
        if normalise_id(org.get(party.activity_key)) is None:
            yield from _org_link(org, own, party.upstream, "transaction")


def _org_link(
    element: Mapping[str, Any], own: _Own, upstream: bool, kind: EdgeKind
) -> Iterator[tuple[Edge, Organisation]]:
    """The organisation link for one provider-org, receiver-org or participating-org element, if it gives one."""
    ref = normalise_id(element.get("ref"))
    name = _first_text(element.get("narrative"))
    if own.is_reporting_org(ref, name):
        return
    if ref is not None:
        node_id = org_node_id(ref)
    elif name is not None:
        node_id = org_name_node_id(name)
    else:
        return
    yield _directed(node_id, own, upstream, kind), Organisation(node_id=node_id, ref=ref, name=name)


def _organisations(organisations: Iterable[Organisation]) -> frozenset[Organisation]:
    """One organisation per node: the first spelling of its ref, and the first non-blank name, in document order."""
    merged: dict[NodeId, Organisation] = {}
    for organisation in organisations:
        known = merged.get(organisation.node_id)
        if known is None:
            merged[organisation.node_id] = organisation
        elif known.name is None and organisation.name is not None:
            merged[organisation.node_id] = replace(known, name=organisation.name)
    return frozenset(merged.values())


# Participating organisations


def _participating_activities(
    orgs: Iterable[dict[str, Any]], own: _Own
) -> Iterator[tuple[int | None, str, dict[str, Any]]]:
    """(role, activity-id spelling, element) for each participating-org naming an activity other than this one."""
    for org in orgs:
        other = _other_activity(org.get("activity-id"), own)
        if other is not None:
            yield _as_int(org.get("role")), other, org


def _participating_links(orgs: Iterable[dict[str, Any]], own: _Own) -> Iterator[tuple[Edge, str]]:
    """Funders (role 1) above, implementers (role 4) below.

    An implementer entry for the reporting organisation itself is skipped: it says that the publisher implements this
    activity, not that this activity funds the activity it points to.
    """
    for role, other, org in _participating_activities(orgs, own):
        if role == _ROLE_FUNDING:
            yield _directed(node_key(other), own, True, "participating-org"), other
        elif role == _ROLE_IMPLEMENTING and not own.is_reporting_org(
            normalise_id(org.get("ref")), _first_text(org.get("narrative"))
        ):
            yield _directed(node_key(other), own, False, "participating-org"), other


def _participating_org_links(orgs: Iterable[dict[str, Any]], own: _Own) -> Iterator[tuple[Edge, Organisation]]:
    """Links to funders (role 1) named as an organisation without an activity."""
    for org in orgs:
        if _as_int(org.get("role")) == _ROLE_FUNDING and normalise_id(org.get("activity-id")) is None:
            yield from _org_link(org, own, True, "participating-org")


def _associations(orgs: Iterable[dict[str, Any]], own: _Own) -> Iterator[tuple[Association, str]]:
    for role, other, _org in _participating_activities(orgs, own):
        association_role = _ASSOCIATION_ROLES.get(role) if role is not None else None
        if association_role is not None:
            first, second = sorted((own.key, node_key(other)))
            yield Association(activities=(first, second), role=association_role, declared_by=own.key), other
