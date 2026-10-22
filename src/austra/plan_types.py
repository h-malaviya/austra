"""Typed case plans and proposals returned by the agent."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "ask_question",
        "draft_supplier_email",
        "draft_po",
        "manager_review",
        "record_inventory",
        "record_sales_order",
        "propose_policy",
    ]
    description: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=1000)


class ProposedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    data_type: Literal["text", "number", "date", "boolean"]
    reason: str = Field(min_length=1, max_length=500)


class ProposedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Z][A-Za-z0-9]{0,49}$")
    description: str = Field(min_length=1, max_length=500)
    fields: list[ProposedField] = Field(max_length=12)


class ProposedSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,49}$")
    body: str = Field(min_length=1, max_length=12000)
    reason: str = Field(min_length=1, max_length=500)


class ProposedMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)
    source_document_ids: list[str] = Field(max_length=10)


class ProposedSystemChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area: Literal["frontend", "backend", "database", "tool", "workflow", "design"]
    title: str = Field(min_length=1, max_length=200)
    specification: str = Field(min_length=1, max_length=6000)
    reason: str = Field(min_length=1, max_length=500)


class ProposedPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=5, max_length=10000)
    reason: str = Field(min_length=1, max_length=500)


class ProposedSupplier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=200)
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=320)
    material: str = Field(min_length=2, max_length=200)
    source_document_ids: list[str] = Field(max_length=10)
    reason: str = Field(min_length=1, max_length=500)


class ProposedPO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: str = Field(min_length=1, max_length=100)
    material: str = Field(min_length=2, max_length=200)
    grade: str = Field(min_length=1, max_length=200)
    quantity_tonnes: float = Field(gt=0)
    unit_price_usd: float = Field(gt=0)
    payment_terms: str = Field(min_length=1, max_length=500)
    delivery_terms: str = Field(min_length=1, max_length=500)
    freight_terms: str = Field(min_length=1, max_length=500)
    tax_terms: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)


class ProposedStockReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=2, max_length=200)
    item_kind: Literal["raw_material", "finished_product"]
    lot_code: str = Field(min_length=1, max_length=100)
    grade: str = Field(min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=200)
    quantity_tonnes: float = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class ProposedSalesOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer: str = Field(min_length=2, max_length=200)
    item: str = Field(min_length=2, max_length=200)
    quantity_tonnes: float = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class CasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    category: Literal["purchasing", "sales", "inventory", "other"]
    missing_information: list[str] = Field(max_length=12)
    actions: list[ProposedAction] = Field(max_length=8)
    record_types: list[ProposedRecord] = Field(max_length=5)
    skill_drafts: list[ProposedSkill] = Field(default_factory=list, max_length=2)
    memory_drafts: list[ProposedMemory] = Field(default_factory=list, max_length=5)
    system_changes: list[ProposedSystemChange] = Field(default_factory=list, max_length=3)
    policy_drafts: list[ProposedPolicy] = Field(default_factory=list, max_length=2)
    supplier_drafts: list[ProposedSupplier] = Field(default_factory=list, max_length=5)
    po_drafts: list[ProposedPO] = Field(default_factory=list, max_length=3)
    stock_receipts: list[ProposedStockReceipt] = Field(default_factory=list, max_length=5)
    sales_orders: list[ProposedSalesOrder] = Field(default_factory=list, max_length=5)
