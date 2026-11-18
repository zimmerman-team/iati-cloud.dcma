import pytest

from app.iati_cloud_dcma.nodes import identification_of, is_org_node, normalise_id, org_name_node_id, org_node_id

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
        ("A" * 255, "A" * 255),
        ("A" * 256, None),
    ],
)
def test_normalise_id(value, expected):
    assert normalise_id(value) == expected


def test_org_node_id_is_namespaced():
    assert org_node_id("XM-OCHA-CERF") == "org:xm-ocha-cerf"


# --- Organisation node keys --------------------------------------------------------------------------------------


def test_org_node_helpers():
    assert is_org_node("org:GB-CHC-1") is True
    assert is_org_node("GB-CHC-1") is False
    assert is_org_node("org-name:some org") is True
    assert identification_of("org:gb-chc-1") == "identifier"
    assert identification_of("org:brac uk") == "free-text"
    assert identification_of("org-name:nrc") == "name-only"
    assert org_name_node_id("NRC") == "org-name:nrc"
