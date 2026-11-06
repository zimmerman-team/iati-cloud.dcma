"""Tests for app.iati_cloud_dcma.links: link extraction from one Solr activity document (spec §5.2, §5.3, §5.5)."""

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
    normalise_id,
    org_node_id,
)

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


def _edge(upstream, downstream, kind, declared_by=X) -> Edge:
    return Edge(upstream=upstream, downstream=downstream, kind=kind, declared_by=declared_by)


def _tx(provider=None, receiver=None, code=3) -> dict:
    tx = {"transaction-type": {"code": code}, "value": 100}
    if provider is not None:
        tx["provider-org"] = provider
    if receiver is not None:
        tx["receiver-org"] = receiver
    return tx


# --- normalise_id -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GB-GOV-1-300978-405", "GB-GOV-1-300978-405"),
        (" GB-GOV-1-300978-405", "GB-GOV-1-300978-405"),
        ("\n  XM-OCHA-CERF \t", "XM-OCHA-CERF"),
        (41119, "41119"),
        ("", None),
        ("   ", None),
        (None, None),
        (True, None),
        (1.5, None),
        (["GB-1"], None),
        ({"ref": "GB-1"}, None),
    ],
)
def test_normalise_id(value, expected):
    assert normalise_id(value) == expected


def test_org_node_id_is_namespaced():
    assert org_node_id("XM-OCHA-CERF") == "org:XM-OCHA-CERF"


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


@pytest.mark.parametrize("code", [1, 2, 3, 4, 11, "3", None])
def test_transaction_links_apply_to_every_transaction_type(code):
    tx = _tx(provider={"provider-activity-id": "P-1"}, receiver={"receiver-activity-id": "R-1"}, code=code)
    if code is None:
        del tx["transaction-type"]
    links = extract(_doc(transactions=[tx]))
    assert links.edges == {_edge("P-1", X, "transaction"), _edge(X, "R-1", "transaction")}


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
        Association(activities=("AA-FIRST", X), role=role, declared_by=X),
        Association(activities=(X, "ZZ"), role=role, declared_by=X),
    }


@pytest.mark.parametrize("role", [5, 0, None, "funding"])
def test_other_participating_org_roles_are_ignored(role):
    org = {"activity-id": "A-1"} if role is None else {"role": role, "activity-id": "A-1"}
    assert extract(_doc(participating=[org])) == Links()


@pytest.mark.parametrize("role", [1, 2, 3, 4])
def test_participating_org_without_activity_id_is_ignored(role):
    # Organisation nodes from participating-org refs are a future extension (spec §5.2, §9).
    participating = [{"ref": "GB-CHC-1", "role": role}, {"ref": "", "role": role, "activity-id": ""}]
    assert extract(_doc(participating=participating)) == Links()


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
    assert links.organisations == {Organisation(ref="US-EIN-1", name="AAH USA")}


def test_o2_provider_org_without_activity_gives_org_edge():
    links = extract(_doc(transactions=[_tx(provider={"ref": "GB-GOV-1", "narrative": "FCDO"}, code=1)]))
    assert links.edges == {_edge("org:GB-GOV-1", X, "transaction")}
    assert links.organisations == {Organisation(ref="GB-GOV-1", name="FCDO")}


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


@pytest.mark.parametrize("org", [{"narrative": "Name only"}, {"ref": "", "narrative": "Name"}, {"ref": None}, {}])
def test_org_without_ref_is_skipped(org):
    assert extract(_doc(transactions=[_tx(provider=org, receiver=org)])) == Links()


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
    assert extract(_doc(transactions=transactions)).organisations == {Organisation(ref="R-ORG", name="First Name")}


def test_org_name_from_list_narrative():
    tx = _tx(receiver={"ref": "R-ORG", "narrative": ["", " Nom ", "Name"]})
    assert extract(_doc(transactions=[tx])).organisations == {Organisation(ref="R-ORG", name="Nom")}


def test_org_name_is_none_when_no_narrative():
    tx = _tx(receiver={"ref": "R-ORG", "narrative": None})
    assert extract(_doc(transactions=[tx])).organisations == {Organisation(ref="R-ORG", name=None)}


def test_org_name_only_from_transactions_that_create_the_org_link():
    transactions = [
        _tx(receiver={"ref": "R-ORG", "receiver-activity-id": "R-1", "narrative": "Not this one"}),
        _tx(receiver={"ref": "R-ORG", "narrative": "This one"}),
    ]
    assert extract(_doc(transactions=transactions)).organisations == {Organisation(ref="R-ORG", name="This one")}


def test_org_seen_as_provider_and_receiver_is_one_organisation():
    transactions = [_tx(provider={"ref": "BOTH", "narrative": "Both"}), _tx(receiver={"ref": "BOTH", "narrative": "B"})]
    links = extract(_doc(transactions=transactions))
    assert links.edges == {_edge("org:BOTH", X, "transaction"), _edge(X, "org:BOTH", "transaction")}
    assert links.organisations == {Organisation(ref="BOTH", name="Both")}


def test_numeric_org_ref_is_coerced():
    links = extract(_doc(transactions=[_tx(receiver={"ref": 41119, "narrative": "UNFPA"})]))
    assert links.edges == {_edge(X, "org:41119", "transaction")}
    assert links.organisations == {Organisation(ref="41119", name="UNFPA")}


@pytest.mark.parametrize("ref", ["GB-CHC-1047501", "US-EIN-13-3327220", "41119", "XM-OCHA-CBPF-AFG-22/3481"])
def test_identifier_like_ref_is_structured(ref):
    assert Organisation(ref=ref, name=None).structured_ref is True


@pytest.mark.parametrize("ref", ["2025 Q7 Expenditure (KAALO)", "BRAC UK", "IATI Id GB-GOV-1", "NL-CCI-1 -GPAF"])
def test_ref_with_internal_whitespace_is_not_structured(ref):
    assert Organisation(ref=ref, name=None).structured_ref is False


def test_unstructured_ref_still_gives_org_node():
    # Decision 2026-11-06: follow spec §5.3 literally and flag the node; filtering happens at the API level.
    links = extract(_doc(transactions=[_tx(receiver={"ref": "  BRAC UK "})]))
    assert links.edges == {_edge(X, "org:BRAC UK", "transaction")}
    assert links.organisations == {Organisation(ref="BRAC UK", name=None)}
    assert next(iter(links.organisations)).structured_ref is False


def test_organisation_node_id():
    assert Organisation(ref="US-EIN-1", name=None).node_id == "org:US-EIN-1"


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
    assert extract(_doc(participating=participating)).associations == {Association(("A-1", X), 3, X)}


def test_same_pair_with_both_roles_gives_two_associations():
    participating = [{"role": 2, "activity-id": "A-1"}, {"role": 3, "activity-id": "A-1"}]
    assert extract(_doc(participating=participating)).associations == {
        Association(("A-1", X), 2, X),
        Association(("A-1", X), 3, X),
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
    assert next(iter(links.edges)).upstream == "P-1"
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
    assert links.organisations == {Organisation("US-EIN-13-3327220", "ACTION AGAINST HUNGER USA")}
    assert links.associations == frozenset()  # its role-2 activity-id is itself


def test_fixture_fcdo_project_ignores_siblings(activity_fixture):
    links = extract(activity_fixture(PROJECT_408))
    assert links.edges == {
        _edge(PROGRAMME, PROJECT_408, "hierarchy", PROJECT_408),
        _edge(PROJECT_408, "org:GB-CHC-1047501", "transaction", PROJECT_408),
    }
    assert links.organisations == {Organisation("GB-CHC-1047501", "Action Against Hunger (UK)")}


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
    assert links.associations == {Association(("GB-CHC-1047501-24SOELN2F", TROCAIRE), 3, TROCAIRE)}
    assert links.organisations == frozenset()


def test_fixture_norwegian_refugee_council_trims_and_flags(activity_fixture):
    links = extract(activity_fixture(NRC))
    partners = ["GB-CHC-1065972", "GB-COH-213890", "GB-CHC-1047501", "GB-CHC-1092236"]
    assert links.edges == {
        # " GB-GOV-1-300978-405" (leading space) and "GB-GOV-1-300978-405" collapse into one edge
        _edge(PROJECT_405, NRC, "transaction", NRC),
        _edge(PROJECT_405, NRC, "participating-org", NRC),
        # its own organisation ref, published as a provider-activity-id: kept as an (unpublished) activity
        _edge("NO-BRC-977538319", NRC, "transaction", NRC),
        # partner organisation refs published as receiver-activity-ids: also kept as activities
        *(_edge(NRC, partner, "transaction", NRC) for partner in partners),
        *(_edge(NRC, f"org:{partner}", "transaction", NRC) for partner in partners),
        _edge(NRC, "org:2025 Q7 Expenditure (KAALO)", "transaction", NRC),
    }
    assert links.organisations == {
        Organisation("GB-CHC-1065972", "2025 Q8 Expenditure (IRC)"),
        Organisation("GB-COH-213890", "2025 Q8 Expenditure (SCI)"),
        Organisation("GB-CHC-1047501", "2025 Q8 Expenditure (ACF)"),
        Organisation("GB-CHC-1092236", "2025 Q8 Expenditure (CWW)"),
        Organisation("2025 Q7 Expenditure (KAALO)", "2025 Q7 Expenditure (KAALO)"),
    }
    unstructured = {org.ref for org in links.organisations if not org.structured_ref}
    assert unstructured == {"2025 Q7 Expenditure (KAALO)"}


def test_fixture_ocha_pooled_fund_with_slash_in_identifier(activity_fixture):
    raw = activity_fixture(CBPF)
    links = extract(raw)
    parent = "XM-OCHA-CBPF-AFG23-2022"
    assert ActivityDoc.from_solr(raw).iati_identifier == CBPF
    assert links.edges == {
        _edge(parent, CBPF, "hierarchy", CBPF),
        _edge(parent, CBPF, "transaction", CBPF),
        _edge(parent, CBPF, "participating-org", CBPF),
    }
    # empty-string refs and activity IDs, and a receiver named only by narrative, give nothing
    assert links.organisations == frozenset()
    assert links.associations == frozenset()


def test_fixture_cerf_org_ref_as_activity_id(activity_fixture):
    links = extract(activity_fixture(CERF))
    assert links.edges == {
        _edge("XM-OCHA-CERF", CERF, "participating-org", CERF),
        _edge(CERF, "org:XM-DAC-47066", "transaction", CERF),
    }
    assert links.organisations == {Organisation("XM-DAC-47066", "International Organization for Migration (IOM)")}


# --- ActivityDoc.linked_activity_ids ------------------------------------------------------------------------------


def test_linked_activity_ids_are_the_other_ends_of_edges_and_associations():
    doc = ActivityDoc.from_solr(
        _doc(
            related=[{"ref": "P-1", "type": 1}, {"ref": "C-1", "type": 2}],
            participating=[{"role": 3, "activity-id": "A-1"}, {"role": 2, "activity-id": "ZZ-A-2"}],
            transactions=[_tx(receiver={"ref": "R-ORG"}, provider={"ref": "P-ORG"})],
        )
    )
    assert doc.linked_activity_ids == {"P-1", "C-1", "A-1", "ZZ-A-2"}


def test_linked_activity_ids_exclude_organisation_nodes(activity_fixture):
    doc = ActivityDoc.from_solr(activity_fixture(AAH))
    assert doc.linked_activity_ids == {PROJECT_408}


def test_linked_activity_ids_of_document_without_links():
    assert ActivityDoc.from_solr({"iati-identifier": X}).linked_activity_ids == frozenset()
