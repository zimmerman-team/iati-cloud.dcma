"""Node keys: how activities and organisations are identified inside a chain.

Nodes are identified by keys rather than by the identifiers as published, so that spellings which differ only in
letter case end up as the same node:

- `act:<identifier, case-folded>` for an activity
- `org:<ref, case-folded>` for an organisation named by its ref
- `org-name:<name, normalised>` for an organisation named only by its name

Pure: no Solr calls, no state.
"""

from typing import Literal

NodeId = str  # act:, org: or org-name: key, see the module docstring
Identification = Literal["identifier", "free-text", "name-only"]

MAX_IDENTIFIER_LENGTH = 255

ACTIVITY_PREFIX = "act:"
ORG_PREFIX = "org:"
ORG_NAME_PREFIX = "org-name:"


def normalise_id(value: object) -> str | None:
    """Trim an identifier or ref; coerce numbers to str. Blank, overlong or non-scalar values give None.

    Identifiers found in documents are sent back to Solr in later queries, so they get the same length limit as the
    seed a caller may pass.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if 0 < len(text) <= MAX_IDENTIFIER_LENGTH else None


def node_key(identifier: str) -> NodeId:
    """The node key of an activity. Identifiers are case-folded because publishers don't always cite each other in the
    same letter case."""
    return f"{ACTIVITY_PREFIX}{identifier.casefold()}"


def normalise_name(name: str) -> str:
    """A name for comparison: trimmed, case-folded, inner whitespace collapsed."""
    return " ".join(name.split()).casefold()


def org_node_id(ref: str) -> NodeId:
    """The node key of an organisation named by its ref, case-folded like activity keys."""
    return f"{ORG_PREFIX}{ref.casefold()}"


def org_name_node_id(name: str) -> NodeId:
    return f"{ORG_NAME_PREFIX}{normalise_name(name)}"


def is_org_node(node_id: NodeId) -> bool:
    return node_id.startswith((ORG_PREFIX, ORG_NAME_PREFIX))


def identification_of(org_node: NodeId) -> Identification:
    """How well an organisation node is identified, which the `orgs` filter selects on.

    A ref containing whitespace is treated as free text: real organisation identifiers never contain spaces, so it is
    a name or a note that someone typed into the ref field.
    """
    if org_node.startswith(ORG_NAME_PREFIX):
        return "name-only"
    return "free-text" if any(char.isspace() for char in org_node[len(ORG_PREFIX) :]) else "identifier"
