import base64

import pytest

from austra.agent import CasePlan
from austra.email_outbox import (
    EmailOutbox,
    GmailConfig,
    SentMessage,
    TokenUnavailable,
    _plain_body,
)
from austra.purchasing import Purchasing
from austra.store import Store


def _plan(supplier=None, po=None):
    return CasePlan.model_validate(
        {
            "summary": "PO email",
            "category": "purchasing",
            "missing_information": [],
            "actions": [],
            "record_types": [],
            "supplier_drafts": [supplier] if supplier else [],
            "po_drafts": [po] if po else [],
        }
    )


def _approved_po(tmp_path):
    store = Store(tmp_path / "business.db")
    purchasing = Purchasing(store)
    case = store.create_case("Buy quartz", "ui", "Owner")
    store.save_plan(
        case["id"],
        _plan(
            supplier={
                "name": "Quartz Supplier",
                "email": "sales@example.com",
                "material": "quartz",
                "source_document_ids": [],
                "reason": "Owner request",
            }
        ).model_dump(),
        [],
    )
    supplier_draft = purchasing.for_case(case["id"])["supplier_drafts"][0]
    purchasing.review_supplier(supplier_draft["id"], "approved", "Manager")
    supplier_id = purchasing.verified_suppliers()[0]["id"]
    store.save_plan(
        case["id"],
        _plan(
            po={
                "supplier_id": supplier_id,
                "material": "quartz",
                "grade": "high purity",
                "quantity_tonnes": 2,
                "unit_price_usd": 6000,
                "payment_terms": "Net 30",
                "delivery_terms": "Ahmedabad",
                "freight_terms": "Buyer pays",
                "tax_terms": "GST excluded",
                "reason": "Owner request",
            }
        ).model_dump(),
        [],
    )
    po_id = purchasing.for_case(case["id"])["purchase_orders"][0]["id"]
    purchasing.review_po(po_id, "approved", "Manager")
    return store, case["id"], po_id


def _config():
    return GmailConfig("client", "secret", "refresh", "purchasing@example.com")


def test_po_email_is_prepared_and_sent_once(tmp_path):
    store, case_id, po_id = _approved_po(tmp_path)
    sent = []

    def sender(draft, _config):
        sent.append(draft)
        return SentMessage("gmail-1", "thread-1")

    outbox = EmailOutbox(store, sender=sender)
    draft = outbox.for_case(case_id)[0]
    assert draft["status"] == "ready"
    assert "Grade: high purity" in draft["body"]

    result = outbox.send(po_id, _config())

    assert result["status"] == "sent"
    assert result["gmail_thread_id"] == "thread-1"
    assert len(sent) == 1
    with pytest.raises(ValueError, match="already sent"):
        outbox.send(po_id, _config())
    assert len(sent) == 1


def test_ambiguous_send_is_not_retried(tmp_path):
    store, case_id, po_id = _approved_po(tmp_path)

    def sender(_draft, _config):
        raise RuntimeError("connection lost after request")

    outbox = EmailOutbox(store, sender=sender)
    with pytest.raises(ValueError, match="uncertain"):
        outbox.send(po_id, _config())

    assert outbox.for_case(case_id)[0]["status"] == "uncertain"
    with pytest.raises(ValueError, match="reconciliation"):
        outbox.send(po_id, _config())


def test_token_failure_before_send_keeps_draft_ready(tmp_path):
    store, case_id, po_id = _approved_po(tmp_path)

    def sender(_draft, _config):
        raise TokenUnavailable("Gmail token refresh failed")

    outbox = EmailOutbox(store, sender=sender)
    with pytest.raises(ValueError, match="token refresh"):
        outbox.send(po_id, _config())
    assert outbox.for_case(case_id)[0]["status"] == "ready"


def test_thread_sync_adds_supplier_reply_once(tmp_path):
    store, case_id, po_id = _approved_po(tmp_path)

    def fetcher(_thread_id, _config):
        return [
            {"id": "gmail-1", "sender": "purchasing@example.com", "body": "Sent PO"},
            {"id": "reply-1", "sender": "sales@example.com", "body": "Can we discuss freight?"},
        ]

    outbox = EmailOutbox(
        store,
        sender=lambda _draft, _config: SentMessage("gmail-1", "thread-1"),
        thread_fetcher=fetcher,
    )
    outbox.send(po_id, _config())

    assert outbox.sync_thread(po_id, _config())["new_messages"] == 1
    assert outbox.sync_thread(po_id, _config())["new_messages"] == 0
    case = store.get_case(case_id)
    assert case["status"] == "new"
    assert [message["body"] for message in case["messages"] if message["channel"] == "gmail"] == [
        "Can we discuss freight?"
    ]


def test_plain_mime_body_is_decoded():
    encoded = base64.urlsafe_b64encode(b"Please confirm delivery.").decode().rstrip("=")
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/plain", "body": {"data": encoded}}],
    }

    assert _plain_body(payload) == "Please confirm delivery."
