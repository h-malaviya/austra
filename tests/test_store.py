from austra.store import Store


def test_repeated_external_message_keeps_one_case(tmp_path):
    store = Store(tmp_path / "business.db")

    first = store.create_case("Need quartz", "slack", "sales", "event-42")
    repeated = store.create_case("Need quartz", "slack", "sales", "event-42")

    assert repeated["id"] == first["id"]
    assert len(store.list_cases()) == 1
    assert len(store.get_case(first["id"])["messages"]) == 1


def test_policy_versions_and_reanalysis_are_auditable(tmp_path):
    store = Store(tmp_path / "business.db")
    case = store.create_case("Need quartz", "ui", "owner")
    first_policy = store.add_policy("Purchase approval", "Manager approves every PO")
    store.activate_policy(first_policy["id"], "Manager A")
    second_policy = store.add_policy("Purchase approval", "Manager approves POs above the limit")
    store.activate_policy(second_policy["id"], "Manager A")
    plan = {
        "summary": "Request quartz quotation",
        "category": "purchasing",
        "missing_information": ["quantity"],
        "actions": [
            {
                "kind": "ask_question",
                "description": "Ask for quantity",
                "reason": "Missing quantity",
            }
        ],
        "record_types": [
            {
                "name": "QuartzRequirement",
                "description": "Requested quartz",
                "fields": [{"name": "quantity", "data_type": "number", "reason": "Order sizing"}],
            }
        ],
    }

    store.save_plan(case["id"], plan, [second_policy["id"]])
    store.save_plan(case["id"], plan, [second_policy["id"]])
    detail = store.get_case(case["id"])

    assert [policy["status"] for policy in store.list_policies()] == ["active", "superseded"]
    assert sum(action["status"] == "proposed" for action in detail["actions"]) == 1
    assert sum(action["status"] == "superseded" for action in detail["actions"]) == 1
    assert detail["events"][-1]["details"]["policy_ids"] == [second_policy["id"]]


def test_review_cannot_run_twice(tmp_path):
    store = Store(tmp_path / "business.db")
    case = store.create_case("Need quartz", "ui", "owner")
    store.save_plan(
        case["id"],
        {
            "summary": "Request quotation",
            "category": "purchasing",
            "missing_information": [],
            "actions": [
                {"kind": "draft_supplier_email", "description": "Draft RFQ", "reason": "Need price"}
            ],
            "record_types": [],
        },
        [],
    )
    proposal = store.get_case(case["id"])["actions"][0]

    store.review_proposal("action_proposals", proposal["id"], "approved", "Manager A")

    assert store.get_case(case["id"])["actions"][0]["status"] == "approved"
    try:
        store.review_proposal("action_proposals", proposal["id"], "approved", "Manager A")
    except ValueError as error:
        assert "already been reviewed" in str(error)
    else:
        raise AssertionError("Repeated review must be rejected")
