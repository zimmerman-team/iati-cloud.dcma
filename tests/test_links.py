"""Tests for app.iati_cloud_dcma.links: link extraction from one Solr activity document."""

import json
import logging

import pytest

from app.iati_cloud_dcma.links import (
    ActivityDoc,
    Association,
    Edge,
    InvalidDocument,
    Links,
    Organisation,
    extract,
)
from app.iati_cloud_dcma.nodes import is_org_node, node_key, org_name_node_id, org_node_id

X = "XX-TEST-1"
REPORTER = "XX-ORG"
LOGGER = "app.iati_cloud_dcma.links"


def _json_values(values) -> list:
    return [v if isinstance(v, str) else json.dumps(v) for v in values]


def _doc(*, related=(), transactions=(), participating=(), **fields) -> dict:
    """Build a raw Solr document for X. Dict values are JSON-encoded; strings are passed through unchanged."""
    doc = {"iati-identifier": X, "reporting-org.ref": REPORTER}
    if related:
        doc["json.related-activity"] = _json_values(related)
    if transactions:
        doc["json.transaction"] = _json_values(transactions)
    if participating:
        doc["json.participating-org"] = _json_values(participating)
    doc.update(fields)
    return doc


def _key(node: str) -> str:
    """The node key of an activity identifier, or of an organisation written as "org:<ref>" / "org-name:<name>"."""
    return node.casefold() if node.startswith(("org:", "org-name:")) else node_key(node)


def _edge(upstream, downstream, kind, declared_by=X) -> Edge:
    """An edge written with the spellings of the test data; the extractor works in node keys."""
    return Edge(upstream=_key(upstream), downstream=_key(downstream), kind=kind, declared_by=_key(declared_by))


def _org(ref, name=None) -> Organisation:
    return Organisation(node_id=org_node_id(ref), ref=ref, name=name)


def _association(first, second, role, declared_by=X) -> Association:
    return Association(activities=(node_key(first), node_key(second)), role=role, declared_by=node_key(declared_by))


def _named_org(name) -> Organisation:
    return Organisation(node_id=org_name_node_id(name), ref=None, name=name)


def _tx(provider=None, receiver=None, code=3) -> dict:
    tx = {"transaction-type": {"code": code}, "value": 100}
    if provider is not None:
        tx["provider-org"] = provider
    if receiver is not None:
        tx["receiver-org"] = receiver
    return tx


# --- L1/L2: related-activity --------------------------------------------------------------------------------------


def test_l1_parent_gives_edge_from_parent():
    links = extract(_doc(related=[{"ref": "P-1", "type": 1}]))
    assert links.edges == {_edge("P-1", X, "hierarchy")}


def test_l2_child_gives_edge_to_child():
    links = extract(_doc(related=[{"ref": "C-1", "type": 2}]))
    assert links.edges == {_edge(X, "C-1", "hierarchy")}


def test_related_activity_type_given_as_string():
    links = extract(_doc(related=[{"ref": "P-1", "type": "1"}, {"ref": "C-1", "type": " 2 "}]))
    assert links.edges == {_edge("P-1", X, "hierarchy"), _edge(X, "C-1", "hierarchy")}


@pytest.mark.parametrize("rel_type", [3, 4, 5, 6, 0, None, "x", "3"])
def test_other_related_activity_types_are_ignored(rel_type):
    relation = {"ref": "R-1"} if rel_type is None else {"ref": "R-1", "type": rel_type}
    assert extract(_doc(related=[relation])) == Links()


def test_related_activity_without_ref_is_ignored():
    assert extract(_doc(related=[{"type": 1}, {"ref": "  ", "type": 2}])) == Links()


# --- L3/L4: transaction activity IDs ------------------------------------------------------------------------------


def test_l3_provider_activity_gives_edge_from_provider():
    links = extract(_doc(transactions=[_tx(provider={"ref": "GB-GOV-1", "provider-activity-id": "P-1"})]))
    assert links.edges == {_edge("P-1", X, "transaction")}
    assert links.organisations == frozenset()


def test_l4_receiver_activity_gives_edge_to_receiver():
    links = extract(_doc(transactions=[_tx(receiver={"ref": "GB-CHC-1", "receiver-activity-id": "R-1"})]))
    assert links.edges == {_edge(X, "R-1", "transaction")}
    assert links.organisations == frozenset()


def _typed_tx(code, **parties) -> dict:
    tx = _tx(code=code, **parties)
    if code is None:
        del tx["transaction-type"]
    return tx


ALL_TYPES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, "3", " 1 ", None, "x"]
OUTGOING_TYPES = [2, 3, 4, 12, "3", " 4 "]
OTHER_TYPES = [1, 5, 6, 7, 8, 9, 10, 11, 13, None, "x", {"code": 3}]


@pytest.mark.parametrize("code", ALL_TYPES)
def test_l3_applies_to_every_transaction_type(code):
    links = extract(_doc(transactions=[_typed_tx(code, provider={"provider-activity-id": "P-1"})]))
    assert links.edges == {_edge("P-1", X, "transaction")}


@pytest.mark.parametrize("code", OUTGOING_TYPES)
def test_l4_applies_to_outgoing_transaction_types(code):
    links = extract(_doc(transactions=[_typed_tx(code, receiver={"receiver-activity-id": "R-1"})]))
    assert links.edges == {_edge(X, "R-1", "transaction")}


@pytest.mark.parametrize("code", OTHER_TYPES)
def test_l4_and_o1_ignore_receivers_of_other_transaction_types(code):
    # On an incoming transaction the receiver is the activity itself or, often, its own parent (spec C1).
    transactions = [
        _typed_tx(code, receiver={"receiver-activity-id": "R-1"}),
        _typed_tx(code, receiver={"ref": "R-ORG", "narrative": "Receiver"}),
    ]
    assert extract(_doc(transactions=transactions)) == Links()


def test_transaction_type_given_as_plain_value():
    tx = {"transaction-type": "3", "receiver-org": {"receiver-activity-id": "R-1"}}
    assert extract(_doc(transactions=[tx])).edges == {_edge(X, "R-1", "transaction")}


@pytest.mark.parametrize("code", ALL_TYPES)
def test_o2_applies_to_every_transaction_type(code):
    links = extract(_doc(transactions=[_typed_tx(code, provider={"ref": "P-ORG"})]))
    assert links.edges == {_edge("org:P-ORG", X, "transaction")}


def test_transaction_activity_id_that_is_an_org_ref_is_kept_as_activity():
    # Spec §5.5: an organisation ref where an activity ID belongs becomes an (unpublished) activity node.
    links = extract(_doc(transactions=[_tx(receiver={"ref": "GB-CHC-1", "receiver-activity-id": "GB-CHC-1"})]))
    assert links.edges == {_edge(X, "GB-CHC-1", "transaction")}


# --- L5/L6/L7: participating-org activity IDs ---------------------------------------------------------------------


def test_l5_funding_org_activity_gives_edge_from_funder():
    links = extract(_doc(participating=[{"ref": "GB-GOV-1", "role": 1, "activity-id": "F-1"}]))
    assert links.edges == {_edge("F-1", X, "participating-org")}


def test_l6_implementing_org_activity_gives_edge_to_implementer():
    links = extract(_doc(participating=[{"ref": "GB-CHC-1", "role": 4, "activity-id": "I-1"}]))
    assert links.edges == {_edge(X, "I-1", "participating-org")}


def test_participating_org_role_given_as_string():
    links = extract(_doc(participating=[{"role": "1", "activity-id": "F-1"}, {"role": "4", "activity-id": "I-1"}]))
    assert links.edges == {_edge("F-1", X, "participating-org"), _edge(X, "I-1", "participating-org")}


@pytest.mark.parametrize("role", [2, 3])
def test_l7_accountable_or_extending_org_gives_sorted_association(role):
    links = extract(
        _doc(participating=[{"role": role, "activity-id": "AA-FIRST"}, {"role": role, "activity-id": "ZZ"}])
    )
    assert links.edges == frozenset()
    assert links.associations == {
        _association("AA-FIRST", X, role),
        _association(X, "ZZ", role),
    }


@pytest.mark.parametrize("role", [5, 0, None, "funding"])
def test_other_participating_org_roles_are_ignored(role):
    org = {"activity-id": "A-1"} if role is None else {"role": role, "activity-id": "A-1"}
    assert extract(_doc(participating=[org])) == Links()


@pytest.mark.parametrize("role", [2, 3, 4])
def test_other_roles_without_activity_id_are_ignored(role):
    # Role-4 organisations as downstream nodes are an extension (spec §8).
    participating = [{"ref": "GB-CHC-1", "role": role}, {"ref": "", "role": role, "activity-id": ""}]
    assert extract(_doc(participating=participating)) == Links()


def test_l6_ignores_the_reporting_org_as_implementer():
    # The publisher lists itself as implementer with a variant of its own activity ID, or its parent (spec C2).
    participating = [
        {"ref": REPORTER, "role": 4, "activity-id": "XX-TEST-PARENT"},
        {"ref": REPORTER.lower(), "role": 4, "activity-id": "XX-TEST-1-OLD"},
    ]
    assert extract(_doc(participating=participating)) == Links()


def test_l6_without_ref_compares_the_name_with_the_reporting_org():
    participating = [
        {"role": 4, "activity-id": "I-1", "narrative": "  the   REPORTER "},
        {"role": 4, "activity-id": "I-2", "narrative": "Someone else"},
        {"role": 4, "activity-id": "I-3"},
    ]
    links = extract(_doc(participating=participating, **{"reporting-org.narrative": ["The Reporter"]}))
    assert links.edges == {_edge(X, "I-2", "participating-org"), _edge(X, "I-3", "participating-org")}


def test_l5_still_applies_to_the_reporting_org_as_funder():
    # A role-1 entry for the own organisation mostly names the activity's own parent: the right direction.
    links = extract(_doc(participating=[{"ref": REPORTER, "role": 1, "activity-id": "XX-TEST-PARENT"}]))
    assert links.edges == {_edge("XX-TEST-PARENT", X, "participating-org")}


# --- O3: funder named without an activity -----------------------------------------------------------------------


def test_o3_funding_org_without_activity_gives_org_edge():
    participating = [
        {"ref": "GB-GOV-1", "role": 1, "narrative": "FCDO"},
        {"ref": "SE-0", "role": "1", "activity-id": " "},
    ]
    links = extract(_doc(participating=participating))
    assert links.edges == {_edge("org:GB-GOV-1", X, "participating-org"), _edge("org:SE-0", X, "participating-org")}
    assert links.organisations == {_org("GB-GOV-1", "FCDO"), _org("SE-0")}


def test_o3_does_not_apply_when_the_funder_names_an_activity():
    links = extract(_doc(participating=[{"ref": "GB-GOV-1", "role": 1, "activity-id": "F-1"}]))
    assert links.edges == {_edge("F-1", X, "participating-org")}
    assert links.organisations == frozenset()


def test_o3_does_not_apply_when_the_activity_id_is_the_activity_itself():
    assert extract(_doc(participating=[{"ref": "GB-GOV-1", "role": 1, "activity-id": X}])) == Links()


def test_o3_skips_the_reporting_org_and_takes_name_only_funders():
    participating = [{"ref": REPORTER, "role": 1}, {"role": 1, "narrative": "Some  Donor"}, {"role": 1}]
    links = extract(_doc(participating=participating))
    assert links.edges == {_edge("org-name:some donor", X, "participating-org")}
    assert links.organisations == {_named_org("Some  Donor")}


def test_funder_in_transaction_and_participating_org_is_one_organisation_with_two_kinds():
    links = extract(
        _doc(
            participating=[{"ref": "GB-GOV-1", "role": 1}],
            transactions=[_tx(provider={"ref": "gb-gov-1", "narrative": "FCDO"}, code=1)],
        )
    )
    assert links.edges == {_edge("org:GB-GOV-1", X, "participating-org"), _edge("org:GB-GOV-1", X, "transaction")}
    # the first spelling in document order (transactions are read first), with the first name found
    assert links.organisations == {Organisation(node_id="org:gb-gov-1", ref="gb-gov-1", name="FCDO")}


# --- Self-references ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"related": [{"ref": X, "type": 1}, {"ref": f" {X} ", "type": 2}]},
        {"transactions": [_tx(provider={"ref": REPORTER, "provider-activity-id": X})]},
        {"transactions": [_tx(receiver={"ref": REPORTER, "receiver-activity-id": f"{X} "})]},
        {"participating": [{"role": role, "activity-id": X} for role in (1, 2, 3, 4)]},
    ],
    ids=["related-activity", "provider-activity-id", "receiver-activity-id", "participating-org"],
)
def test_self_references_are_ignored(fields):
    assert extract(_doc(**fields)) == Links()


def test_self_reference_in_another_letter_case_is_ignored():
    fields = {
        "related": [{"ref": X.lower(), "type": 1}],
        "transactions": [
            _tx(provider={"provider-activity-id": X.lower()}, receiver={"receiver-activity-id": "xx-Test-1"})
        ],
        "participating": [{"role": 1, "activity-id": X.lower()}],
    }
    assert extract(_doc(**fields)) == Links()


def test_spellings_of_one_identifier_give_one_edge_and_keep_their_labels():
    links = extract(
        _doc(transactions=[_tx(provider={"provider-activity-id": spelling}) for spelling in ("P-1", "p-1")])
    )
    assert links.edges == {_edge("P-1", X, "transaction")}
    assert links.labels == {(_key("P-1"), "P-1"), (_key("P-1"), "p-1")}


def test_labels_cover_edges_and_associations_but_not_organisations():
    links = extract(
        _doc(
            related=[{"ref": "Parent-1", "type": 1}],
            participating=[{"role": 2, "activity-id": "Assoc-1"}],
            transactions=[_tx(receiver={"ref": "R-ORG"})],
        )
    )
    assert links.labels == {(_key("Parent-1"), "Parent-1"), (_key("Assoc-1"), "Assoc-1")}


def test_self_referencing_activity_id_still_blocks_org_link():
    # The transaction names an activity (X itself), so O1/O2 do not apply even though the ref is another org.
    tx = _tx(
        provider={"ref": "OTHER-ORG", "provider-activity-id": X},
        receiver={"ref": "GB-CHC-1", "receiver-activity-id": X},
    )
    assert extract(_doc(transactions=[tx])) == Links()


# --- O1/O2: organisation links -------------------------------------------------------------------------------------


def test_o1_receiver_org_without_activity_gives_org_edge():
    links = extract(_doc(transactions=[_tx(receiver={"ref": "US-EIN-1", "narrative": "AAH USA"})]))
    assert links.edges == {_edge(X, "org:US-EIN-1", "transaction")}
    assert links.organisations == {_org("US-EIN-1", "AAH USA")}


def test_o2_provider_org_without_activity_gives_org_edge():
    links = extract(_doc(transactions=[_tx(provider={"ref": "GB-GOV-1", "narrative": "FCDO"}, code=1)]))
    assert links.edges == {_edge("org:GB-GOV-1", X, "transaction")}
    assert links.organisations == {_org("GB-GOV-1", "FCDO")}


@pytest.mark.parametrize("empty_activity_id", ["", "   ", None])
def test_blank_activity_id_counts_as_absent(empty_activity_id):
    tx = _tx(
        provider={"ref": "P-ORG", "provider-activity-id": empty_activity_id},
        receiver={"ref": "R-ORG", "receiver-activity-id": empty_activity_id},
    )
    links = extract(_doc(transactions=[tx]))
    assert links.edges == {_edge("org:P-ORG", X, "transaction"), _edge(X, "org:R-ORG", "transaction")}


@pytest.mark.parametrize("ref", [REPORTER, f" {REPORTER} "])
def test_reporting_org_is_never_an_org_node(ref):
    tx = _tx(provider={"ref": ref}, receiver={"ref": ref})
    assert extract(_doc(transactions=[tx], **{"reporting-org.ref": f"{REPORTER}\n"})) == Links()


def test_reporting_org_in_another_letter_case_is_never_an_org_node():
    tx = _tx(provider={"ref": REPORTER.lower()}, receiver={"ref": REPORTER.lower()})
    assert extract(_doc(transactions=[tx])) == Links()


@pytest.mark.parametrize("org", [{"ref": None}, {}, {"ref": " ", "narrative": "  "}, {"narrative": []}])
def test_org_without_ref_or_name_is_skipped(org):
    assert extract(_doc(transactions=[_tx(provider=org, receiver=org)])) == Links()


@pytest.mark.parametrize(
    "org", [{"narrative": " Norwegian  REFUGEE Council "}, {"ref": "", "narrative": "Norwegian Refugee Council"}]
)
def test_org_with_only_a_name_gives_a_name_only_node(org):
    links = extract(_doc(transactions=[_tx(receiver=org)]))
    assert links.edges == {_edge(X, "org-name:norwegian refugee council", "transaction")}
    (organisation,) = links.organisations
    assert (organisation.ref, organisation.identification) == (None, "name-only")
    assert organisation.name == " ".join(org["narrative"].split()) or organisation.name == org["narrative"].strip()


def test_name_only_org_equal_to_the_reporting_org_name_is_skipped():
    tx = _tx(provider={"narrative": "the  reporter"}, receiver={"narrative": "The Reporter"})
    assert extract(_doc(transactions=[tx], **{"reporting-org.narrative": "The Reporter"})) == Links()


def test_name_only_org_is_kept_when_the_reporting_org_has_no_name():
    links = extract(_doc(transactions=[_tx(receiver={"narrative": "The Reporter"})]))
    assert links.organisations == {_named_org("The Reporter")}


def test_name_only_node_is_separate_from_a_ref_node_with_the_same_name():
    transactions = [_tx(receiver={"ref": "22000", "narrative": "NRC"}), _tx(receiver={"narrative": "NRC"})]
    links = extract(_doc(transactions=transactions))
    assert links.edges == {_edge(X, "org:22000", "transaction"), _edge(X, "org-name:nrc", "transaction")}
    assert links.organisations == {_org("22000", "NRC"), _named_org("NRC")}


def test_org_link_without_reporting_org_on_document():
    doc = _doc(transactions=[_tx(receiver={"ref": "R-ORG"})])
    del doc["reporting-org.ref"]
    assert extract(doc).edges == {_edge(X, "org:R-ORG", "transaction")}


def test_org_name_is_first_non_empty_narrative():
    transactions = [
        _tx(receiver={"ref": "R-ORG"}),
        _tx(receiver={"ref": "R-ORG", "narrative": "   "}),
        _tx(receiver={"ref": "R-ORG", "narrative": "\n  First Name \n"}),
        _tx(receiver={"ref": "R-ORG", "narrative": "Second Name"}),
    ]
    assert extract(_doc(transactions=transactions)).organisations == {_org("R-ORG", "First Name")}


def test_org_name_from_list_narrative():
    tx = _tx(receiver={"ref": "R-ORG", "narrative": ["", " Nom ", "Name"]})
    assert extract(_doc(transactions=[tx])).organisations == {_org("R-ORG", "Nom")}


def test_org_name_is_none_when_no_narrative():
    tx = _tx(receiver={"ref": "R-ORG", "narrative": None})
    assert extract(_doc(transactions=[tx])).organisations == {_org("R-ORG", None)}


def test_org_name_only_from_transactions_that_create_the_org_link():
    transactions = [
        _tx(receiver={"ref": "R-ORG", "receiver-activity-id": "R-1", "narrative": "Not this one"}),
        _tx(receiver={"ref": "R-ORG", "narrative": "This one"}),
    ]
    assert extract(_doc(transactions=transactions)).organisations == {_org("R-ORG", "This one")}


def test_org_seen_as_provider_and_receiver_is_one_organisation():
    transactions = [_tx(provider={"ref": "BOTH", "narrative": "Both"}), _tx(receiver={"ref": "BOTH", "narrative": "B"})]
    links = extract(_doc(transactions=transactions))
    assert links.edges == {_edge("org:BOTH", X, "transaction"), _edge(X, "org:BOTH", "transaction")}
    assert links.organisations == {_org("BOTH", "Both")}


def test_numeric_org_ref_is_coerced():
    links = extract(_doc(transactions=[_tx(receiver={"ref": 41119, "narrative": "UNFPA"})]))
    assert links.edges == {_edge(X, "org:41119", "transaction")}
    assert links.organisations == {_org("41119", "UNFPA")}


@pytest.mark.parametrize("ref", ["GB-CHC-1047501", "US-EIN-13-3327220", "41119", "XM-OCHA-CBPF-AFG-22/3481"])
def test_identifier_like_ref_is_an_identifier(ref):
    assert _org(ref).identification == "identifier"


@pytest.mark.parametrize("ref", ["2025 Q7 Expenditure (KAALO)", "BRAC UK", "IATI Id GB-GOV-1", "NL-CCI-1 -GPAF"])
def test_ref_with_internal_whitespace_is_free_text(ref):
    assert _org(ref).identification == "free-text"


def test_free_text_ref_still_gives_org_node():
    links = extract(_doc(transactions=[_tx(receiver={"ref": "  BRAC UK "})]))
    assert links.edges == {_edge(X, "org:BRAC UK", "transaction")}
    assert links.organisations == {_org("BRAC UK")}


def test_organisation_node_ids():
    assert _org("US-EIN-1").node_id == "org:us-ein-1"
    assert _named_org(" Some\n Org ").node_id == "org-name:some org"


# --- Merging and duplicates ---------------------------------------------------------------------------------------


def test_duplicate_evidence_collapses_to_one_edge():
    transactions = [_tx(provider={"provider-activity-id": "P-1"}, code=code) for code in (1, 1, 2, 3)]
    transactions.append(_tx(provider={"provider-activity-id": " P-1 "}))
    assert extract(_doc(transactions=transactions)).edges == {_edge("P-1", X, "transaction")}


def test_same_pair_from_different_evidence_keeps_one_edge_per_kind():
    links = extract(
        _doc(
            related=[{"ref": "P-1", "type": 1}],
            transactions=[_tx(provider={"provider-activity-id": "P-1"})],
            participating=[{"role": 1, "activity-id": "P-1"}],
        )
    )
    assert links.edges == {
        _edge("P-1", X, "hierarchy"),
        _edge("P-1", X, "transaction"),
        _edge("P-1", X, "participating-org"),
    }


def test_duplicate_associations_collapse():
    participating = [{"role": 3, "activity-id": "A-1"}, {"role": "3", "activity-id": " A-1"}]
    assert extract(_doc(participating=participating)).associations == {_association("A-1", X, 3)}


def test_same_pair_with_both_roles_gives_two_associations():
    participating = [{"role": 2, "activity-id": "A-1"}, {"role": 3, "activity-id": "A-1"}]
    assert extract(_doc(participating=participating)).associations == {
        _association("A-1", X, 2),
        _association("A-1", X, 3),
    }


# --- Malformed and missing data -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_kwarg", ["related", "transactions", "participating"], ids=["related", "transaction", "participating"]
)
@pytest.mark.parametrize("bad_value", ["{not json", "[1, 2]", "null", "42", '"text"'])
def test_malformed_json_value_is_skipped_and_logged(caplog, field_kwarg, bad_value):
    good = {
        "related": {"ref": "P-1", "type": 1},
        "transactions": _tx(provider={"provider-activity-id": "P-1"}),
        "participating": {"role": 1, "activity-id": "P-1"},
    }[field_kwarg]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        links = extract(_doc(**{field_kwarg: [bad_value, good]}))
    assert len(links.edges) == 1
    assert next(iter(links.edges)).upstream == _key("P-1")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == LOGGER]
    assert len(warnings) == 1
    assert X in warnings[0].getMessage()


def test_non_string_json_field_value_is_skipped_and_logged(caplog):
    doc = _doc(**{"json.related-activity": [{"ref": "P-1", "type": 1}, 7, json.dumps({"ref": "C-1", "type": 2})]})
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        links = extract(doc)
    assert links.edges == {_edge(X, "C-1", "hierarchy")}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_single_valued_json_field_is_accepted():
    doc = _doc(**{"json.related-activity": json.dumps({"ref": "P-1", "type": 1})})
    assert extract(doc).edges == {_edge("P-1", X, "hierarchy")}


@pytest.mark.parametrize("side", ["not-a-dict", ["list"], None, 5])
def test_transaction_org_that_is_not_an_object_is_ignored(side):
    tx = {"transaction-type": {"code": 3}, "provider-org": side, "receiver-org": side}
    assert extract(_doc(transactions=[tx])) == Links()


def test_document_without_link_fields_has_no_links():
    assert extract({"iati-identifier": X}) == Links()


@pytest.mark.parametrize("identifier", [None, "", "   ", 5.5, ["XX-TEST-1"]])
def test_document_without_usable_identifier_is_rejected(identifier):
    doc = _doc(related=[{"ref": "P-1", "type": 1}])
    if identifier is None:
        del doc["iati-identifier"]
    else:
        doc["iati-identifier"] = identifier
    with pytest.raises(InvalidDocument):
        extract(doc)
    with pytest.raises(InvalidDocument):
        ActivityDoc.from_solr(doc)


# --- ActivityDoc.from_solr ----------------------------------------------------------------------------------------


def test_activity_doc_from_solr_reads_metadata(activity_fixture):
    raw = activity_fixture("GB-CHC-1047501-23SOELNF")
    doc = ActivityDoc.from_solr(raw)
    assert doc.iati_identifier == "GB-CHC-1047501-23SOELNF"
    assert doc.title is not None
    assert doc.title.startswith("Integrated emergency life-saving Health, Nutrition WASH and Protection support")
    assert doc.reporting_org_ref == "GB-CHC-1047501"
    assert doc.reporting_org_name == "Action Against Hunger UK"
    assert doc.hierarchy == 1
    assert doc.activity_status == "4"
    links = extract(raw)
    assert (doc.edges, doc.associations, doc.organisations) == (links.edges, links.associations, links.organisations)
    assert doc.labels == links.labels
    assert doc.key == "act:gb-chc-1047501-23soelnf"


def test_activity_doc_with_only_identifier():
    doc = ActivityDoc.from_solr({"iati-identifier": f" {X} "})
    assert doc == ActivityDoc(
        iati_identifier=X,
        title=None,
        reporting_org_ref=None,
        reporting_org_name=None,
        hierarchy=None,
        activity_status=None,
        edges=frozenset(),
        associations=frozenset(),
        organisations=frozenset(),
        labels=frozenset(),
        doc_id=None,
        last_updated=None,
    )


@pytest.mark.parametrize(
    ("raw_fields", "expected"),
    [
        (
            {"title.narrative": ["  ", " Title "], "reporting-org.narrative": "Org", "hierarchy": "2"},
            ("Title", "Org", 2, None),
        ),
        (
            {"title.narrative": "T", "reporting-org.narrative": [], "hierarchy": [3], "activity-status.code": 4},
            ("T", None, 3, "4"),
        ),
        (
            {"hierarchy": "two", "activity-status.code": ["2"], "reporting-org.narrative": [" ", None]},
            (None, None, None, "2"),
        ),
        ({"hierarchy": True, "activity-status.code": " "}, (None, None, None, None)),
    ],
)
def test_activity_doc_coerces_metadata(raw_fields, expected):
    doc = ActivityDoc.from_solr({"iati-identifier": X, **raw_fields})
    assert (doc.title, doc.reporting_org_name, doc.hierarchy, doc.activity_status) == expected


def test_activity_doc_reporting_org_ref_from_list():
    assert (
        ActivityDoc.from_solr({"iati-identifier": X, "reporting-org.ref": [" XX-ORG "]}).reporting_org_ref == REPORTER
    )


def test_activity_doc_is_immutable():
    doc = ActivityDoc.from_solr({"iati-identifier": X})
    with pytest.raises(AttributeError):
        doc.title = "changed"


# --- Real documents from the reference instance (tests/fixtures/activities) ---------------------------------------

AAH = "GB-CHC-1047501-23SOELNF"
PROGRAMME = "GB-GOV-1-300978"
PROJECT_408 = "GB-GOV-1-300978-408"
PROJECT_405 = "GB-GOV-1-300978-405"
TROCAIRE = "IE-CHY-20009601-FCDO24SOM"
NRC = "NO-BRC-977538319-SOFM2343"
CBPF = "XM-OCHA-CBPF-AFG-22/3481/SA1/FSAC/INGO/22203"
CERF = "XM-OCHA-CERF-14-UFE-IOM-040"


def test_fixture_action_against_hunger(activity_fixture):
    links = extract(activity_fixture(AAH))
    assert links.edges == {
        _edge(PROJECT_408, AAH, "participating-org", AAH),
        _edge(PROJECT_408, AAH, "transaction", AAH),
        _edge(AAH, "org:US-EIN-13-3327220", "transaction", AAH),
    }
    assert links.organisations == {_org("US-EIN-13-3327220", "ACTION AGAINST HUNGER USA")}
    assert links.associations == frozenset()  # its role-2 activity-id is itself


def test_fixture_fcdo_project_ignores_siblings(activity_fixture):
    links = extract(activity_fixture(PROJECT_408))
    assert links.edges == {
        _edge(PROGRAMME, PROJECT_408, "hierarchy", PROJECT_408),
        _edge(PROJECT_408, "org:GB-CHC-1047501", "transaction", PROJECT_408),
    }
    assert links.organisations == {_org("GB-CHC-1047501", "Action Against Hunger (UK)")}


def test_fixture_fcdo_programme_lists_its_25_children(activity_fixture):
    links = extract(activity_fixture(PROGRAMME))
    children = [f"{PROGRAMME}-{n}" for n in [*range(101, 113), *range(401, 414)]]
    assert len(children) == 25
    assert links.edges == {_edge(PROGRAMME, child, "hierarchy", PROGRAMME) for child in children}
    assert links.organisations == frozenset()
    assert links.associations == frozenset()


def test_fixture_trocaire_has_role_3_association(activity_fixture):
    links = extract(activity_fixture(TROCAIRE))
    assert links.edges == {
        _edge(PROJECT_408, TROCAIRE, "transaction", TROCAIRE),
        _edge(PROJECT_408, TROCAIRE, "participating-org", TROCAIRE),
    }
    assert links.associations == {_association("GB-CHC-1047501-24SOELN2F", TROCAIRE, 3, TROCAIRE)}
    assert links.organisations == frozenset()


def test_fixture_norwegian_refugee_council_trims_and_flags(activity_fixture):
    links = extract(activity_fixture(NRC))
    partners = ["GB-CHC-1065972", "GB-COH-213890", "GB-CHC-1047501", "GB-CHC-1092236"]
    notes = [
        "2024 Q5 Expenditure (GREDO)",
        "2024 Q5 Expenditure (KAALO)",
        "2025 Q6 Expenditure (GREDO)",
        "2025 Q6 Expenditure (KAALO)",
        "2025 Q7 Expenditure (GREDO)",
        "2025 Q8 Expenditure (GREDO)",
        NRC,  # its own activity ID typed as a receiver name
    ]
    assert links.edges == {
        # " GB-GOV-1-300978-405" (leading space) and "GB-GOV-1-300978-405" collapse into one edge
        _edge(PROJECT_405, NRC, "transaction", NRC),
        _edge(PROJECT_405, NRC, "participating-org", NRC),
        # its own organisation ref, published as a provider-activity-id: kept as an (unpublished) activity
        _edge("NO-BRC-977538319", NRC, "transaction", NRC),
        # The partner refs it publishes as receiver-activity-ids sit on incoming (type 1) transactions, so they give no
        # activity edges (spec C1). Its expenditures name the same partners by ref only: organisation links.
        *(_edge(NRC, f"org:{partner}", "transaction", NRC) for partner in partners),
        _edge(NRC, "org:2025 Q7 Expenditure (KAALO)", "transaction", NRC),
        *(_edge(NRC, f"org-name:{note}", "transaction", NRC) for note in notes),
    }
    assert links.organisations == {
        _org("GB-CHC-1065972", "2025 Q8 Expenditure (IRC)"),
        _org("GB-COH-213890", "2025 Q8 Expenditure (SCI)"),
        _org("GB-CHC-1047501", "2025 Q8 Expenditure (ACF)"),
        _org("GB-CHC-1092236", "2025 Q8 Expenditure (CWW)"),
        _org("2025 Q7 Expenditure (KAALO)", "2025 Q7 Expenditure (KAALO)"),
        *(_named_org(note) for note in notes),
    }
    free_text = {org.ref for org in links.organisations if org.identification == "free-text"}
    assert free_text == {"2025 Q7 Expenditure (KAALO)"}


def test_fixture_ocha_pooled_fund_with_slash_in_identifier(activity_fixture):
    raw = activity_fixture(CBPF)
    links = extract(raw)
    parent = "XM-OCHA-CBPF-AFG23-2022"
    assert ActivityDoc.from_solr(raw).iati_identifier == CBPF
    assert links.edges == {
        _edge(parent, CBPF, "hierarchy", CBPF),
        _edge(parent, CBPF, "transaction", CBPF),
        _edge(parent, CBPF, "participating-org", CBPF),
        _edge(CBPF, "org-name:Norwegian Afghanistan Committee", "transaction", CBPF),
    }
    # empty-string refs and activity IDs give nothing; the receiver named only by narrative is a name-only node
    assert links.organisations == {_named_org("Norwegian Afghanistan Committee")}
    assert links.associations == frozenset()


def test_fixture_incoming_funds_naming_the_own_parent_as_receiver(activity_fixture):
    # Its incoming-funds transactions (type 1) have the donor as provider and its own parent fund as
    # receiver-activity-id. Read as "X funds R", the child would fund its parent (spec C1).
    child, parent = "XM-OCHA-CBPF-ERF/VEN_CBPF/9552/14817/2021", "XM-OCHA-CBPF-VEN83-2021"
    raw = activity_fixture(child)
    incoming = [json.loads(value) for value in raw["json.transaction"]]
    assert any(
        tx["transaction-type"]["code"] in (1, "1") and tx["receiver-org"].get("receiver-activity-id") == parent
        for tx in incoming
        if "receiver-org" in tx
    )
    links = extract(raw)
    assert _edge(child, parent, "transaction", child) not in links.edges
    assert links.edges == {
        _edge(parent, child, "hierarchy", child),
        _edge("org:NO-BRC-971277882", child, "transaction", child),
        _edge("org:NO-BRC-971277882", child, "participating-org", child),
    }


def test_fixture_cerf_org_ref_as_activity_id(activity_fixture):
    links = extract(activity_fixture(CERF))
    assert links.edges == {
        _edge("XM-OCHA-CERF", CERF, "participating-org", CERF),
        _edge(CERF, "org:XM-DAC-47066", "transaction", CERF),
    }
    assert links.organisations == {_org("XM-DAC-47066", "International Organization for Migration (IOM)")}


# --- ActivityDoc.linked_activity_ids ------------------------------------------------------------------------------


def test_linked_activity_ids_are_the_other_ends_of_edges_and_associations():
    doc = ActivityDoc.from_solr(
        _doc(
            related=[{"ref": "P-1", "type": 1}, {"ref": "C-1", "type": 2}],
            participating=[{"role": 3, "activity-id": "A-1"}, {"role": 2, "activity-id": "ZZ-A-2"}],
            transactions=[_tx(receiver={"ref": "R-ORG"}, provider={"ref": "P-ORG"})],
        )
    )
    assert doc.linked_activity_ids == {_key(i) for i in ("P-1", "C-1", "A-1", "ZZ-A-2")}


def test_linked_activity_ids_exclude_organisation_nodes(activity_fixture):
    doc = ActivityDoc.from_solr(activity_fixture(AAH))
    assert doc.linked_activity_ids == {node_key(PROJECT_408)}


def test_linked_activity_ids_of_document_without_links():
    assert ActivityDoc.from_solr({"iati-identifier": X}).linked_activity_ids == frozenset()


# --- ActivityDoc.merge: several copies of one identifier (spec §4.5) ------------------------------------------------


def _copy(doc_id, updated, *, identifier=X, title=None, parent=None) -> ActivityDoc:
    raw = _doc(related=[{"ref": parent, "type": 1}] if parent else [], id=doc_id)
    raw["iati-identifier"] = identifier
    if updated is not None:
        raw["last-updated-datetime"] = updated
    if title is not None:
        raw["title.narrative"] = title
    return ActivityDoc.from_solr(raw)


def test_activity_doc_reads_document_id_and_update_time():
    doc = _copy("uuid-1", "2015-09-23T11:07:28Z")
    assert (doc.doc_id, doc.last_updated) == ("uuid-1", "2015-09-23T11:07:28Z")


def test_merge_of_one_copy_is_that_copy():
    doc = _copy("a", "2020-01-01T00:00:00Z", parent="P-1")
    assert ActivityDoc.merge([doc]) is doc


def test_merge_keeps_the_links_of_every_copy():
    copies = [_copy("a", "2020-01-01T00:00:00Z", parent="P-1"), _copy("b", "2021-01-01T00:00:00Z", parent="P-2")]
    merged = ActivityDoc.merge(copies)
    assert merged.edges == {_edge("P-1", X, "hierarchy"), _edge("P-2", X, "hierarchy")}
    assert merged.labels == {(_key("P-1"), "P-1"), (_key("P-2"), "P-2")}


def test_merge_takes_metadata_and_spelling_from_the_copy_updated_last():
    copies = [
        _copy("a", "2020-01-01T00:00:00Z", title="Old"),
        _copy("b", "2021-01-01T00:00:00Z", title="New", identifier="xx-Test-1"),
        _copy("c", None, title="Undated"),
    ]
    for order in (copies, copies[::-1]):
        merged = ActivityDoc.merge(order)
        assert (merged.title, merged.iati_identifier, merged.doc_id) == ("New", "xx-Test-1", "b")


def test_merge_breaks_ties_by_lowest_document_id():
    copies = [_copy("b", "2020-01-01T00:00:00Z", title="B"), _copy("a", "2020-01-01T00:00:00Z", title="A")]
    assert ActivityDoc.merge(copies).title == "A"
    assert ActivityDoc.merge(copies[::-1]).title == "A"


def test_merge_of_undated_copies_without_ids_is_deterministic():
    copies = [
        _copy(None, None, title="B", identifier="XX-TEST-1"),
        _copy(None, None, title="A", identifier="xx-test-1"),
    ]
    assert ActivityDoc.merge(copies).title == ActivityDoc.merge(copies[::-1]).title


def test_merge_compares_update_times_as_moments_not_as_text():
    copies = [
        _copy("a", "2024-01-01T01:00:00+02:00", title="Earlier"),  # 23:00 UTC the day before
        _copy("b", "2024-01-01T00:00:00Z", title="Later"),
        _copy("c", "not a date", title="Unreadable"),
        _copy("d", "2023-12-31T23:30:00", title="No zone"),  # read as UTC
    ]
    assert ActivityDoc.merge(copies).title == "Later"
    assert ActivityDoc.merge(copies[2:]).title == "No zone"


# --- Node keys keep activities and organisations apart ------------------------------------------------------------


@pytest.mark.parametrize("identifier", ["org:x", "org-name:x", "Org: see parent"])
def test_activity_identifier_that_looks_like_an_organisation_node_is_still_an_activity(identifier):
    links = extract(_doc(related=[{"ref": identifier, "type": 2}]))
    (edge,) = links.edges
    assert edge.downstream == node_key(identifier)
    assert is_org_node(edge.downstream) is False
    assert ActivityDoc.from_solr(_doc(related=[{"ref": identifier, "type": 2}])).linked_activity_ids == {
        edge.downstream
    }


def test_reporting_org_without_a_ref_is_recognised_by_name():
    doc = _doc(transactions=[_tx(receiver={"ref": "XX-OTHER-REF", "narrative": "The Reporter"})])
    del doc["reporting-org.ref"]
    doc["reporting-org.narrative"] = "the reporter"
    assert extract(doc) == Links()
