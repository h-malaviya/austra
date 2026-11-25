import pytest

from austra.capabilities.email_automation import evaluate_reply
from austra.semantics.workflow_definition import WorkflowShape, preflight_email_workflow


def test_document_roles_and_record_comparisons_hold_conflicting_attachments():
    config = {"rules": [{
        "state": "waiting", "documents": {"booking": ["Pickup booking"]},
        "checks": [{"source": "attachment", "name": "booking", "labels": ["Cartons", "Units"],
                    "expected_field": "cartons", "comparison": "decimal", "finding": "Carton count differs."}],
        "on_match": {"transition": "received"}, "on_mismatch": {"body": "Please correct the count."},
    }]}

    def attachment(name, text):
        return {"name": name, "status": "extracted", "excerpts": [{"text": text}]}

    table = attachment("v4.csv", "Type | Units\nPickup booking | 1,200.00")
    document = attachment("manifest.txt", "Pickup booking\nCartons: 1200")
    for item in (table, document):
        rule, findings = evaluate_reply(config, "waiting", {"cartons": "1200"}, {"body": "", "attachments": [item]})
        assert (rule, findings) == ({"transition": "received"}, [])
        rule, findings = evaluate_reply(config, "waiting", {"cartons": "1201"}, {"body": "", "attachments": [item]})
        assert (rule, findings) == ({"body": "Please correct the count."}, ["Carton count differs."])
    rule, findings = evaluate_reply(config, "waiting", {"cartons": "1200"}, {"body": "", "attachments": [table, document]})
    assert rule is None and "ambiguous booking" in findings[0]
    conflict = attachment("conflict.pdf", "Pickup booking\nCartons: 1200\nUnits: 1300")
    rule, findings = evaluate_reply(config, "waiting", {"cartons": "1200"}, {"body": "", "attachments": [conflict]})
    assert rule is None and "Conflicting" in findings[0]


@pytest.mark.parametrize(("body", "closes"), [
    ("That works!\nBooking ID: W1", True),
    ("Hi Willow,\nConfirmed.\nReference: W1", True),
    ("Confirmed, but we cannot accept.\nReference: W1", False),
    ("Pending.\n____________________\nFrom: Carrier\nConfirmed.\nReference: W1", False),
    ("Confirmed.\nFrom: Carrier\nReference: W1", False),
])
def test_acknowledgments_exclude_qualifications_and_outlook_quotes(body, closes):
    config = {"rules": [{
        "state": "waiting", "reply_openers": ["Confirmed", "That works"],
        "checks": [{"source": "body", "labels": ["Booking ID", "Reference"],
                    "expected_field": "reference", "blocking": True, "finding": "Booking reference does not match."}],
        "on_match": {"transition": "closed"}, "on_mismatch": {},
    }]}
    rule, findings = evaluate_reply(config, "waiting", {"reference": "W1"}, {"body": body, "attachments": []})
    assert (rule == {"transition": "closed"}) is closes
    assert bool(findings) is not closes


def test_planner_rejects_correction_hidden_behind_blocking_check():
    shape = WorkflowShape.model_validate({
        "fields": [{"key": "reference", "type": "text"}],
        "states": ["waiting"],
        "transitions": [{"key": "open", "from": None, "to": "waiting", "permission": "workflows.run",
                         "fields": ["external_id", "reference"]}],
        "failure_path": "Review missing evidence.",
        "automatic_email": {"rules": [{
            "state": "waiting", "documents": {"booking": ["Pickup booking"]},
            "checks": [{"source": "attachment", "name": "booking", "labels": ["Reference"],
                        "expected_field": "reference", "blocking": True, "finding": "Reference differs."}],
            "on_match": {}, "on_mismatch": {"body": "Please correct the reference."},
        }]},
    })
    with pytest.raises(ValueError, match="discrepancy branch has an action but all checks hold for review"):
        preflight_email_workflow(shape, [])
