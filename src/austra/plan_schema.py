"""Strict JSON output schema for Claude case plans."""

ACTION_KINDS = [
    "ask_question",
    "draft_supplier_email",
    "draft_po",
    "manager_review",
    "record_inventory",
    "record_sales_order",
    "propose_policy",
]

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "category": {"type": "string", "enum": ["purchasing", "sales", "inventory", "other"]},
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ACTION_KINDS},
                    "description": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["kind", "description", "reason"],
                "additionalProperties": False,
            },
        },
        "record_types": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "data_type": {
                                    "type": "string",
                                    "enum": ["text", "number", "date", "boolean"],
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["name", "data_type", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "description", "fields"],
                "additionalProperties": False,
            },
        },
        "skill_drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "body": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "body", "reason"],
                "additionalProperties": False,
            },
        },
        "memory_drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "reason": {"type": "string"},
                    "source_document_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["content", "reason", "source_document_ids"],
                "additionalProperties": False,
            },
        },
        "system_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {
                        "type": "string",
                        "enum": ["frontend", "backend", "database", "tool", "workflow", "design"],
                    },
                    "title": {"type": "string"},
                    "specification": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["area", "title", "specification", "reason"],
                "additionalProperties": False,
            },
        },
        "policy_drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["title", "body", "reason"],
                "additionalProperties": False,
            },
        },
        "supplier_drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "material": {"type": "string"},
                    "source_document_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["name", "email", "material", "source_document_ids", "reason"],
                "additionalProperties": False,
            },
        },
        "po_drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "supplier_id": {"type": "string"},
                    "material": {"type": "string"},
                    "grade": {"type": "string"},
                    "quantity_tonnes": {"type": "number"},
                    "unit_price_usd": {"type": "number"},
                    "payment_terms": {"type": "string"},
                    "delivery_terms": {"type": "string"},
                    "freight_terms": {"type": "string"},
                    "tax_terms": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "supplier_id",
                    "material",
                    "grade",
                    "quantity_tonnes",
                    "unit_price_usd",
                    "payment_terms",
                    "delivery_terms",
                    "freight_terms",
                    "tax_terms",
                    "reason",
                ],
                "additionalProperties": False,
            },
        },
        "stock_receipts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "item_kind": {"type": "string", "enum": ["raw_material", "finished_product"]},
                    "lot_code": {"type": "string"},
                    "grade": {"type": "string"},
                    "location": {"type": "string"},
                    "quantity_tonnes": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "item",
                    "item_kind",
                    "lot_code",
                    "grade",
                    "location",
                    "quantity_tonnes",
                    "reason",
                ],
                "additionalProperties": False,
            },
        },
        "sales_orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "customer": {"type": "string"},
                    "item": {"type": "string"},
                    "quantity_tonnes": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["customer", "item", "quantity_tonnes", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "summary",
        "category",
        "missing_information",
        "actions",
        "record_types",
        "skill_drafts",
        "memory_drafts",
        "system_changes",
        "policy_drafts",
        "supplier_drafts",
        "po_drafts",
        "stock_receipts",
        "sales_orders",
    ],
    "additionalProperties": False,
}
