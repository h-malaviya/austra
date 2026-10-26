"""Complete, reproducible reports over saved business records."""

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

STOCK_REQUEST = re.compile(
    r"(?:please\s+)?(?:show|shoe|list|display|view|give)\s+(?:me\s+)?"
    r"(?:(?:all|the|our|current|complete|full)\s+)*"
    r"(?:stock|tock|inventory)(?:\s+(?:lots|records|on\s+record))?[?.!\s]*",
    re.IGNORECASE,
)
PREFERRED_COLUMNS = (
    "lot_id",
    "verification_status",
    "material_name",
    "sku",
    "item_code",
    "product_name",
    "product_grade",
    "qc_status",
    "qa_status",
    "on_hand_kg",
    "reserved_kg",
    "qc_hold_kg",
    "available_kg",
    "quantity_on_hand",
    "on_hand",
    "quantity",
    "quantity_available",
    "available_quantity",
    "reserved_quantity",
    "unit",
    "unit_of_measure",
    "uom",
    "warehouse_bin",
    "warehouse_location",
    "location",
    "notes",
)
LABELS = {
    "lot_id": "Lot ID",
    "verification_status": "Verification",
    "material_name": "Material",
    "sku": "SKU",
    "item_code": "Item code",
    "product_name": "Product",
    "product_grade": "Grade",
    "qc_status": "QC status",
    "qa_status": "QA status",
    "on_hand_kg": "On hand (kg)",
    "reserved_kg": "Reserved (kg)",
    "qc_hold_kg": "QC hold (kg)",
    "available_kg": "Available (kg)",
    "quantity_on_hand": "On hand",
    "on_hand": "On hand",
    "quantity": "Quantity",
    "quantity_available": "Available",
    "available_quantity": "Available",
    "reserved_quantity": "Reserved",
    "unit": "Unit",
    "unit_of_measure": "Unit",
    "uom": "Unit",
    "warehouse_bin": "Bin",
    "warehouse_location": "Location",
    "location": "Location",
    "notes": "Notes",
}


def is_stock_request(message: str) -> bool:
    return STOCK_REQUEST.fullmatch(message.strip()) is not None


def requested_stock_unit(message: str) -> str | None:
    text = message.casefold()
    if not re.search(r"\b(?:from now on|going forward|always|underlying)\b", text):
        return None
    if not re.search(r"\b(?:show|display|present|see|calculate)\b", text):
        return None
    if re.search(r"\b(?:metric\s+tons?|tonnes?|tons?)\b", text):
        return "t"
    if re.search(r"\b(?:kilograms?|kg)\b", text):
        return "kg"
    return None


def stock_display_unit(artifacts: list[dict[str, Any]]) -> str:
    policy = next(
        (
            item["content"]
            for item in artifacts
            if item["kind"] == "policy" and item["name"] == "stock_display_units"
        ),
        "",
    )
    return (
        "t"
        if re.search(
            r"^Display unit:\s*metric tons?\s*\(t\)\s*$", policy, re.IGNORECASE | re.MULTILINE
        )
        else "kg"
    )


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        value = f"{value:g}"
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ") or "—"


def _quantity(value: Any, unit: str) -> str:
    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(str(value)) / (1000 if unit == "t" else 1)
    except (InvalidOperation, TypeError):
        return _cell(value)
    return f"{amount:,.3f}".rstrip("0").rstrip(".")


def _total(rows: list[dict[str, Any]], key: str, unit: str) -> str:
    total = Decimal(0)
    for values in rows:
        try:
            total += Decimal(str(values[key]))
        except (KeyError, InvalidOperation, TypeError):
            continue
    return _quantity(total, unit)


def stock_report(records: list[dict[str, Any]], unit: str = "kg") -> str:
    stock = [
        record
        for record in records
        if "stock" in record["schema"]
        or "inventory" in record["schema"]
        or any(key in record["values"] for key in ("on_hand_kg", "quantity_on_hand"))
    ]
    if not stock:
        return "I found no saved stock records in this workspace."

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in stock:
        grouped[record["schema"]].append(record)
    sections = [f"## Stock on record ({len(stock)} records)"]
    origins = {str(record["values"].get("data_origin")) for record in stock}
    if origins == {"synthetic_demo"}:
        sections.append("These are **demo records**, not confirmed live inventory.")
    elif "synthetic_demo" in origins:
        sections.append("Some records are labeled as demo data; check the Origin column.")

    for schema, group in sorted(grouped.items()):
        rows = sorted(
            (record["values"] for record in group),
            key=lambda row: str(row.get("lot_id", row.get("sku", ""))),
        )
        columns = [key for key in PREFERRED_COLUMNS if any(key in row for row in rows)]
        if not columns:
            columns = [key for key in rows[0] if key != "provenance"][:8]
        if len(origins) > 1 and "data_origin" not in columns:
            columns.append("data_origin")
        table = [
            "| "
            + " | ".join(
                LABELS.get(key, key.replace("_", " ").title()).replace("(kg)", f"({unit})")
                for key in columns
            )
            + " |"
        ]
        table.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in rows:
            table.append(
                "| "
                + " | ".join(
                    _quantity(row.get(key), unit) if key.endswith("_kg") else _cell(row.get(key))
                    for key in columns
                )
                + " |"
            )
        section = f"### {schema.replace('_', ' ').title()} ({len(group)})\n\n" + "\n".join(table)
        totals = [
            f"{LABELS[key].replace('(kg)', f'({unit})')}: **{_total(rows, key, unit)}**"
            for key in ("on_hand_kg", "reserved_kg", "qc_hold_kg", "available_kg")
            if key in columns
        ]
        if totals:
            section += "\n\n**Totals:** " + "; ".join(totals) + "."
        sections.append(section)
    return "\n\n".join(sections)
