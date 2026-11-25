"""Typed, transport-independent commands for the first capability slice."""

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Action = Literal[
    "submit_order",
    "reserve_stock",
    "draft_po",
    "approve_po",
    "propose_email",
    "approve_email",
    "transition_record",
]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=6)]
Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=6)]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderPayload(_Payload):
    external_id: str = Field(min_length=1, max_length=100)
    product: str = Field(min_length=1, max_length=120)
    quantity_kg: Quantity


class ReservePayload(_Payload):
    order_id: UUID
    lot_id: UUID
    quantity_kg: Quantity


class PurchasePayload(_Payload):
    order_id: UUID
    external_id: str = Field(min_length=1, max_length=100)
    supplier: str = Field(min_length=1, max_length=120)
    quantity_kg: Quantity
    unit_price_per_kg: Money
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class ApprovePurchasePayload(_Payload):
    po_id: UUID


class EmailPayload(_Payload):
    po_id: UUID
    external_id: str = Field(min_length=1, max_length=100)
    recipient: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)


class ApproveEmailPayload(_Payload):
    email_id: UUID


class TransitionPayload(_Payload):
    capability_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    transition_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    record_id: UUID | None = None
    external_id: str | None = Field(default=None, min_length=1, max_length=100)
    fields: dict[str, str] = Field(default_factory=dict)


PAYLOADS = {
    "submit_order": OrderPayload,
    "reserve_stock": ReservePayload,
    "draft_po": PurchasePayload,
    "approve_po": ApprovePurchasePayload,
    "propose_email": EmailPayload,
    "approve_email": ApproveEmailPayload,
    "transition_record": TransitionPayload,
}


class CapabilityCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    department_id: UUID
    build_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    correlation_id: UUID
    expected_revisions: dict[UUID, int] = Field(default_factory=dict)
    payload: (
        OrderPayload
        | ReservePayload
        | PurchasePayload
        | ApprovePurchasePayload
        | EmailPayload
        | ApproveEmailPayload
        | TransitionPayload
    )
    occurred_at: datetime | None = None

    @field_validator("expected_revisions")
    @classmethod
    def valid_revisions(cls, values: dict[UUID, int]) -> dict[UUID, int]:
        if any(value < 1 for value in values.values()):
            raise ValueError("Expected revisions must be positive")
        return values

    @model_validator(mode="before")
    @classmethod
    def select_payload(cls, values: object) -> object:
        if isinstance(values, dict) and isinstance(values.get("payload"), dict):
            payload_type = PAYLOADS.get(values.get("action"))
            if payload_type:
                return {**values, "payload": payload_type.model_validate(values["payload"])}
        return values

    @model_validator(mode="after")
    def matching_payload(self):
        if not isinstance(self.payload, PAYLOADS[self.action]):
            raise TypeError("Payload does not match action")
        return self

    def digest(self) -> str:
        excluded = {"correlation_id"} if self.build_id is not None else {"correlation_id", "build_id"}
        data = self.model_dump(mode="json", exclude=excluded)
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
