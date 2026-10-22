import pytest

from austra.store import Store
from austra.workflow import Workflow, load_workflow


def test_case_transitions_follow_versioned_definition(tmp_path):
    workflow = load_workflow()
    store = Store(tmp_path / "business.db")
    case = store.create_case("Need quartz", "ui", "Owner")

    store.save_plan(
        case["id"],
        {
            "summary": "Purchase review",
            "category": "purchasing",
            "missing_information": [],
            "actions": [],
            "record_types": [],
        },
        [],
    )
    assert store.get_case(case["id"])["status"] == "review"

    store.add_message(case["id"], "New quote arrived", "Owner")
    detail = store.get_case(case["id"])
    assert detail["status"] == "new"
    assert detail["events"][-1]["details"]["workflow_version"] == workflow.version


def test_workflow_rejects_ambiguous_transitions():
    with pytest.raises(ValueError, match="overlapping"):
        Workflow.model_validate(
            {
                "id": "case_review",
                "version": 1,
                "initial_state": "new",
                "transitions": [
                    {"event": "message_added", "from_states": ["new"], "to_state": "new"},
                    {"event": "message_added", "from_states": ["new"], "to_state": "review"},
                ],
            }
        )
