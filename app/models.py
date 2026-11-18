"""Request and response models of the API. app/docs.py builds the Swagger definitions from these."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PositiveInt

from app.iati_cloud_dcma.chain import ActivityNode, Chain, ChainAssociation, ChainEdge, Mode, Node, TruncationReason
from app.iati_cloud_dcma.links import AssociationRole, EdgeKind
from app.iati_cloud_dcma.nodes import MAX_IDENTIFIER_LENGTH, Identification
from app.iati_cloud_dcma.traversal import OrgLevel
from app.iati_cloud_dcma.tree import Tree, TreeNode

Format = Literal["graph", "tree"]
Relation = Literal["seed", "ancestor", "descendant", "related", "co-funder"]


def _printable(value: str) -> str:
    if not value.isprintable():
        raise ValueError("must not contain control characters")
    return value


class ChainQuery(BaseModel):
    """The query parameters of GET /chain."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True, extra="ignore")

    activity: Annotated[str, Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH), AfterValidator(_printable)]
    mode: Mode = "full"
    format: Format = "graph"
    orgs: OrgLevel = "identified"
    max_depth: PositiveInt | None = None
    max_nodes: PositiveInt | None = None


# Response Definitions
class _Response(BaseModel):
    model_config = ConfigDict(frozen=True)


class ReportingOrgOut(_Response):
    ref: str | None
    name: str | None


class ActivityNodeOut(_Response):
    id: str
    type: Literal["activity"] = "activity"
    iati_identifier: str
    title: str | None
    reporting_org: ReportingOrgOut | None
    hierarchy: int | None
    activity_status: str | None
    published: bool
    relation: Relation
    distance: int | None
    expanded: bool


class OrganisationNodeOut(_Response):
    id: str
    type: Literal["organisation"] = "organisation"
    org_ref: str | None
    name: str | None
    identification: Identification
    relation: Relation
    distance: int | None
    expanded: bool


NodeOut = ActivityNodeOut | OrganisationNodeOut


class EdgeOut(_Response):
    from_: str = Field(alias="from")
    to: str
    kinds: tuple[EdgeKind, ...]
    declared_by: tuple[str, ...]


class AssociationOut(_Response):
    activities: tuple[str, str]
    role: AssociationRole
    declared_by: tuple[str, ...]


class Stats(_Response):
    nodes: int
    edges: int
    associations: int
    solr_queries: int
    duration_ms: int
    cached: bool


class _ChainResponse(_Response):
    seed: str
    mode: Mode
    format: Format
    orgs: OrgLevel
    truncated: bool
    truncation_reasons: tuple[TruncationReason, ...]
    stats: Stats


class GraphResponse(_ChainResponse):
    nodes: tuple[NodeOut, ...]
    edges: tuple[EdgeOut, ...]
    associations: tuple[AssociationOut, ...]


class ActivityTreeNodeOut(ActivityNodeOut):
    upstream: tuple["TreeNodeOut", ...]
    downstream: tuple["TreeNodeOut", ...]


class OrganisationTreeNodeOut(OrganisationNodeOut):
    upstream: tuple["TreeNodeOut", ...]
    downstream: tuple["TreeNodeOut", ...]


TreeNodeOut = ActivityTreeNodeOut | OrganisationTreeNodeOut


class TreeResponse(_ChainResponse):
    tree: TreeNodeOut
    extra_edges: tuple[EdgeOut, ...]
    associations: tuple[AssociationOut, ...]


class ErrorResponse(_Response):
    error: str


# Chain response
def _node_fields(node: Node) -> dict:
    if isinstance(node, ActivityNode):
        reporting_org = None
        if node.published:
            reporting_org = ReportingOrgOut(ref=node.reporting_org_ref, name=node.reporting_org_name)
        return {
            "id": node.id,
            "iati_identifier": node.id,
            "title": node.title,
            "reporting_org": reporting_org,
            "hierarchy": node.hierarchy,
            "activity_status": node.activity_status,
            "published": node.published,
            "relation": node.relation,
            "distance": node.distance,
            "expanded": node.expanded,
        }
    return {
        "id": node.id,
        "org_ref": node.org_ref,
        "name": node.name,
        "identification": node.identification,
        "relation": node.relation,
        "distance": node.distance,
        "expanded": node.expanded,
    }


def _node_out(node: Node) -> NodeOut:
    fields = _node_fields(node)
    model = ActivityNodeOut if isinstance(node, ActivityNode) else OrganisationNodeOut
    return model.model_validate(fields)


def _tree_node_out(tree_node: TreeNode) -> TreeNodeOut:
    fields = _node_fields(tree_node.node) | {
        "upstream": tuple(_tree_node_out(child) for child in tree_node.upstream),
        "downstream": tuple(_tree_node_out(child) for child in tree_node.downstream),
    }
    model = ActivityTreeNodeOut if isinstance(tree_node.node, ActivityNode) else OrganisationTreeNodeOut
    return model.model_validate(fields)


def _edge_out(edge: ChainEdge) -> EdgeOut:
    return EdgeOut.model_validate(
        {"from": edge.upstream, "to": edge.downstream, "kinds": edge.kinds, "declared_by": edge.declared_by}
    )


def _association_out(association: ChainAssociation) -> AssociationOut:
    return AssociationOut(activities=association.activities, role=association.role, declared_by=association.declared_by)


def _header(chain: Chain, query: ChainQuery, solr_queries: int, duration_ms: int) -> dict:
    return {
        "seed": chain.seed,
        "mode": chain.mode,
        "format": query.format,
        "orgs": query.orgs,
        "truncated": chain.truncated,
        "truncation_reasons": chain.truncation_reasons,
        "stats": Stats(
            nodes=len(chain.nodes),
            edges=len(chain.edges),
            associations=len(chain.associations),
            solr_queries=solr_queries,
            duration_ms=duration_ms,
            cached=False,
        ),
    }


def graph_response(chain: Chain, query: ChainQuery, solr_queries: int, duration_ms: int) -> GraphResponse:
    return GraphResponse.model_validate(
        _header(chain, query, solr_queries, duration_ms)
        | {
            "nodes": tuple(_node_out(node) for node in chain.nodes),
            "edges": tuple(_edge_out(edge) for edge in chain.edges),
            "associations": tuple(_association_out(a) for a in chain.associations),
        }
    )


def tree_response(chain: Chain, tree: Tree, query: ChainQuery, solr_queries: int, duration_ms: int) -> TreeResponse:
    return TreeResponse.model_validate(
        _header(chain, query, solr_queries, duration_ms)
        | {
            "tree": _tree_node_out(tree.root),
            "extra_edges": tuple(_edge_out(edge) for edge in tree.extra_edges),
            "associations": tuple(_association_out(a) for a in chain.associations),
        }
    )
