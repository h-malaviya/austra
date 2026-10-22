import pytest

from austra.agent import CasePlan
from austra.policy_manager import PolicyManager
from austra.store import Store


def plan(body):
    return CasePlan.model_validate(
        {
            "summary": "Clarified purchasing rule",
            "category": "purchasing",
            "missing_information": [],
            "actions": [],
            "record_types": [],
            "policy_drafts": [
                {"title": "Supplier review", "body": body, "reason": "Manager clarified rule"}
            ],
        }
    )


def test_policy_draft_requires_manager_review_and_versions(tmp_path):
    store = Store(tmp_path / "business.db")
    policies = PolicyManager(store)
    case = store.create_case("Remember this supplier rule", "ui", "Manager")
    store.save_plan(case["id"], plan("Manager verifies bank changes.").model_dump(), [])
    first = policies.for_case(case["id"])[0]

    assert store.list_policies(active_only=True) == []
    policies.review(first["id"], "approved", "Manager A")
    first_policy = store.list_policies(active_only=True)[0]
    assert first_policy["version"] == 1
    assert (
        "Manager verifies bank changes."
        in (tmp_path / "policies" / f"{first_policy['id']}.md").read_text()
    )

    store.save_plan(case["id"], plan("Manager verifies bank and address changes.").model_dump(), [])
    second = policies.for_case(case["id"])[-1]
    policies.review(second["id"], "approved", "Manager B")

    current = store.list_policies(active_only=True)
    assert len(current) == 1
    assert current[0]["version"] == 2
    assert current[0]["approved_by"] == "Manager B"
    with pytest.raises(ValueError, match="already been reviewed"):
        policies.review(second["id"], "approved", "Manager B")


def test_followup_supersedes_unapproved_policy(tmp_path):
    store = Store(tmp_path / "business.db")
    policies = PolicyManager(store)
    case = store.create_case("Draft a rule", "ui", "Manager")
    store.save_plan(case["id"], plan("Review all purchases.").model_dump(), [])
    draft = policies.for_case(case["id"])[0]

    store.add_message(case["id"], "Change the scope", "Manager")

    assert policies.for_case(case["id"])[0]["status"] == "superseded"
    with pytest.raises(ValueError, match="already been reviewed"):
        policies.review(draft["id"], "approved", "Manager")
