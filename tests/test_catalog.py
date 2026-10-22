import pytest

from austra.catalog import Catalog
from austra.store import Store


def test_approved_definition_accepts_only_typed_case_data(tmp_path):
    store = Store(tmp_path / "business.db")
    catalog = Catalog(store)
    case = store.create_case("Need 100 tonnes of quartz", "ui", "owner")
    store.save_plan(
        case["id"],
        {
            "summary": "Quartz requirement",
            "category": "purchasing",
            "missing_information": [],
            "actions": [],
            "record_types": [
                {
                    "name": "QuartzRequirement",
                    "description": "Requested quartz quantity",
                    "fields": [
                        {
                            "name": "quantity_tonnes",
                            "data_type": "number",
                            "reason": "Size the request",
                        }
                    ],
                }
            ],
        },
        [],
    )
    proposal_id = store.get_case(case["id"])["records"][0]["id"]
    reviewed = catalog.review_definition(proposal_id, "approved", "Manager A")
    definition_id = reviewed["definition_id"]

    with pytest.raises(ValueError, match="Invalid value"):
        catalog.create_record(case["id"], definition_id, {"quantity_tonnes": "100"}, "Manager A")
    with pytest.raises(ValueError, match="unknown fields"):
        catalog.create_record(case["id"], definition_id, {"price": 100}, "Manager A")

    record = catalog.create_record(case["id"], definition_id, {"quantity_tonnes": 100}, "Manager A")
    assert record["status"] == "unverified"
    assert catalog.records_for_case(case["id"])[0]["values"] == {"quantity_tonnes": 100}
