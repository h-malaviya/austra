"""Read-only, decimal stock and raw-material planning from saved records."""

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from austra.files import Files

KG_PER_TONNE = Decimal(1000)
OPEN_STATUS = ("pending", "open", "received", "confirmed", "accepted")
CLOSED_STATUS = ("cancelled", "canceled", "closed", "delivered", "rejected", "fulfilled")
RELEASED_STATUS = ("released", "approved", "available")
MATERIAL_ALIASES = {
    "h2": "hydrogen",
    "hcl": "hydrogen chloride",
    "hydrogen chloride": "hydrogen chloride",
    "mg-si": "metallurgical silicon powder",
}


def is_material_plan_request(message: str) -> bool:
    text = message.casefold()
    return bool(
        re.search(r"\b(raw\s*(?:materials?|meterials?)|feedstocks?)\b", text)
        and re.search(r"\b(orders?|sales\s*orders?|so)\b", text)
        and re.search(
            r"\b(give|show|compare|need|needed|require|required|available|calculate)\b", text
        )
        and not re.search(r"\b(save|create|update|delete|change|rule|policy)\b", text)
    )


def is_material_plan_followup(message: str, earlier_user_messages: list[str]) -> bool:
    if is_material_plan_request(message):
        return True
    text = message.casefold().strip()
    if re.search(r"\b(suppliers?|vendors?|customers?|invoices?)\b", text):
        return False
    if not re.search(r"\b(remaining|order[ -]?wise|raw\s*materials?|material\s*plan|fix)\b", text):
        return False
    return any(is_material_plan_request(earlier) for earlier in earlier_user_messages[-4:])


def material_plan_additions(message: str) -> set[str]:
    text = message.casefold()
    additions = set()
    if re.search(r"\b(suppliers?|vendors?)\b", text):
        additions.add("suppliers")
    if re.search(
        r"\b(customers?|invoices?|purchase\s+orders?|quotes?|prices?|costs?|lead\s+times?|risks?)\b",
        text,
    ):
        additions.add("other")
    if not additions and re.search(r"\b(also|plus|along\s+with|as\s+well|in\s+addition)\b", text):
        additions.add("other")
    return additions


def unsupported_allocation_request(message: str) -> bool:
    text = message.casefold()
    if re.search(r"\bearliest\b.*\bdelivery\b", text):
        return False
    return bool(
        re.search(r"\b(?:equally|evenly|proportionally|pro[ -]?rata|fifo|lifo)\b", text)
        or re.search(
            r"\b(?:prioriti[sz]e|allocate)\b.*\b(?:largest|smallest|newest|latest)\b", text
        )
    )


def _decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise ValueError(f"Missing or invalid {label}") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"Invalid {label}")
    return number


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _material_name(value: str) -> str:
    lowered = value.casefold().strip()
    if not lowered:
        raise ValueError("A material record has no material identity")
    for token, name in MATERIAL_ALIASES.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered):
            return name
    for suffix in (" charged", " reacted to form tcs", " used in deposition"):
        if suffix in lowered:
            return lowered.split(suffix, 1)[0]
    return lowered.split(" needed", 1)[0].strip()


def _unique(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        identity = str(record["values"].get(key, "")).strip()
        if not identity:
            raise ValueError(f"A {record['schema']} record has no {key}")
        grouped[identity.casefold()].append(record)
    selected = []
    for identity, candidates in grouped.items():
        actual = [
            record
            for record in candidates
            if record["values"].get("data_origin") != "synthetic_demo"
        ]
        if len(actual) > 1:
            raise ValueError(f"Conflicting active records for {identity}")
        if actual:
            selected.append(actual[0])
        # Demo evidence may explain a source, but cannot establish live stock.
    return selected


def _available(record: dict[str, Any], status_field: str) -> Decimal:
    values = record["values"]
    status = str(values.get(status_field, "")).casefold()
    verification = str(values.get("verification_status", "")).casefold()
    if not status or not verification:
        raise ValueError(f"Missing quality or verification status for {values.get('lot_id')}")
    if status not in RELEASED_STATUS or verification != "verified":
        return Decimal(0)
    on_hand = _decimal(values.get("on_hand_kg"), "on-hand quantity")
    reserved = _decimal(values.get("reserved_kg", 0), "reserved quantity")
    held = _decimal(values.get("qc_hold_kg", 0), "QC hold quantity")
    reported = _decimal(values.get("available_kg"), "available quantity")
    derived = on_hand - reserved - held
    if derived < 0 or reported > derived:
        raise ValueError(f"Inconsistent available quantity for {values.get('lot_id')}")
    return reported


def _allowance(files: Files) -> tuple[Decimal, str]:
    policy = next(
        (
            item
            for item in files.approved()
            if item["kind"] == "policy" and item["name"] == "raw_material_wastage_allowance"
        ),
        None,
    )
    if policy is None:
        raise ValueError("No active raw-material allowance is recorded")
    match = re.search(r"Specific multiplier.*?\*\*(\d+(?:\.\d+)?)\*\*", policy["content"])
    if match is None:
        raise ValueError("The raw-material allowance has no numeric multiplier")
    return _decimal(
        match.group(1), "raw-material allowance"
    ), f"{policy['name']} v{policy['version']}"


def build_material_plan(files: Files) -> dict[str, Any]:
    """Return auditable figures without changing business records or allocations."""
    records = files.records()
    candidate_orders = [
        record
        for record in records
        if (
            "sales_order" in record["schema"]
            and "historical" not in record["schema"]
            and "order_id" in record["values"]
        )
    ]
    if any(not record["values"].get("order_status") for record in candidate_orders):
        raise ValueError("A sales order has no status")
    orders = []
    for record in candidate_orders:
        status = str(record["values"]["order_status"]).casefold()
        if status.startswith(OPEN_STATUS):
            orders.append(record)
        elif not status.startswith(CLOSED_STATUS):
            raise ValueError(f"Unmapped sales order status for {record['values']['order_id']}")
    stock = [record for record in records if record["schema"] == "polysilicon_stock_lot"]
    raw = [record for record in records if record["schema"] == "raw_material_inventory_lot"]
    ratios = [
        record for record in records if record["schema"] == "polysilicon_production_material_ratio"
    ]
    if not orders or not stock or not ratios:
        raise ValueError("Order, finished stock, or material-ratio records are missing")
    excluded_demo_stock = sum(
        record["values"].get("data_origin") == "synthetic_demo" for record in stock
    )
    excluded_demo_raw = sum(
        record["values"].get("data_origin") == "synthetic_demo" for record in raw
    )
    source_counts = {
        "finished_records": len(stock),
        "raw_records": len(raw),
    }
    orders = _unique(orders, "order_id")
    stock = _unique(stock, "lot_id")
    raw = _unique(raw, "lot_id")
    ratios = _unique(ratios, "material_or_stage")
    if not stock:
        raise ValueError("No non-demo finished inventory is available")
    if not ratios:
        raise ValueError("No non-demo material ratios are available")
    for order in orders:
        if not order["values"].get("requested_final_delivery_date"):
            raise ValueError(f"Missing requested delivery for {order['values']['order_id']}")
    multiplier, allowance_source = _allowance(files)
    sorted_orders = sorted(
        orders,
        key=lambda record: (
            str(record["values"]["requested_final_delivery_date"]),
            str(record["values"]["order_id"]),
        ),
    )
    stock_remaining = {record["id"]: _available(record, "qc_status") for record in stock}
    order_rows = []
    for order in sorted_orders:
        values = order["values"]
        demand = _decimal(values.get("ordered_kg"), "order quantity")
        grade = str(values.get("grade", "")).casefold()
        if "solar" not in grade:
            raise ValueError(f"No reviewed material mapping for order {values['order_id']} grade")
        if "chunk" not in str(values.get("form", "")).casefold():
            raise ValueError(f"No reviewed form mapping for order {values['order_id']}")
        purity = _decimal(values.get("minimum_silicon_purity_percent"), "order purity")
        if purity != Decimal("99.9999"):
            raise ValueError(f"No reviewed material mapping for order {values['order_id']} purity")
        minimum_size = _decimal(values.get("chunk_size_min_mm"), "minimum chunk size")
        maximum_size = _decimal(values.get("chunk_size_max_mm"), "maximum chunk size")
        if minimum_size > maximum_size:
            raise ValueError(f"Invalid chunk size for {values['order_id']}")
        allocated = Decimal(0)
        lot_ids = []
        for lot in sorted(stock, key=lambda record: str(record["values"]["lot_id"])):
            current = lot["values"]
            if (
                current.get("data_origin") == "synthetic_demo"
                or str(current.get("product_grade", "")).casefold() != "solar"
                or (
                    values.get("form")
                    and current.get("form")
                    and ("chunk" in str(values["form"]).casefold())
                    != ("chunk" in str(current["form"]).casefold())
                )
                or _decimal(current.get("silicon_purity_percent"), "lot purity") < purity
                or _decimal(current.get("chunk_size_min_mm"), "lot minimum size") < minimum_size
                or _decimal(current.get("chunk_size_max_mm"), "lot maximum size") > maximum_size
            ):
                continue
            taken = min(stock_remaining[lot["id"]], demand - allocated)
            if taken > 0:
                stock_remaining[lot["id"]] -= taken
                allocated += taken
                lot_ids.append(current["lot_id"])
            if allocated == demand:
                break
        order_rows.append(
            {
                "id": values["order_id"],
                "record_id": order["id"],
                "delivery": str(values.get("requested_final_delivery_date", "")),
                "demand_kg": _text(demand),
                "finished_allocated_kg": _text(allocated),
                "production_kg": _text(demand - allocated),
                "lot_ids": lot_ids,
            }
        )
    raw_by_name: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"available_kg": Decimal(0), "lot_ids": [], "lots": []}
    )
    for lot in raw:
        values = lot["values"]
        name = _material_name(str(values.get("material_name", "")))
        available = _available(lot, "qa_status")
        raw_by_name[name]["available_kg"] += available
        raw_by_name[name]["lot_ids"].append(values["lot_id"])
        raw_by_name[name]["lots"].append(
            {
                "id": values["lot_id"],
                "record_id": lot["id"],
                "available_kg": _text(available),
                "status": str(values["qa_status"]),
            }
        )
    material_rows = []
    seen_materials = set()
    for ratio in sorted(ratios, key=lambda record: str(record["values"]["material_or_stage"])):
        values = ratio["values"]
        name = _material_name(str(values["material_or_stage"]))
        if name in seen_materials:
            raise ValueError(f"Multiple active ratios for {name} need a reviewed mapping")
        seen_materials.add(name)
        unit = str(values.get("unit", ""))
        if unit not in {"kg", "t"}:
            raise ValueError(f"Unsupported ratio unit for {name}")
        per_tonne_kg = _decimal(values.get("quantity_per_tonne_finished_6n"), f"{name} ratio") * (
            KG_PER_TONNE if unit == "t" else 1
        )
        inventory = raw_by_name.get(name)
        if inventory is None:
            available = None
        else:
            available = inventory["available_kg"]
        by_order = [
            {
                "order_id": order["id"],
                "needed_kg": _text(
                    Decimal(order["production_kg"]) / KG_PER_TONNE * per_tonne_kg * multiplier
                ),
            }
            for order in order_rows
        ]
        needed = sum((Decimal(item["needed_kg"]) for item in by_order), Decimal(0))
        material_rows.append(
            {
                "name": name,
                "ratio_record_id": ratio["id"],
                "basis": str(values.get("basis", "")),
                "per_tonne_kg": _text(per_tonne_kg),
                "needed_kg": _text(needed),
                "available_kg": _text(available) if available is not None else None,
                "shortfall_kg": _text(max(needed - available, 0))
                if available is not None
                else None,
                "by_order": by_order,
                "lot_ids": inventory["lot_ids"] if inventory else [],
            }
        )
    return {
        "orders": order_rows,
        "materials": material_rows,
        "inventory": [
            {
                "name": name,
                "available_kg": _text(item["available_kg"]),
                "lot_ids": item["lot_ids"],
                "lots": item["lots"],
            }
            for name, item in sorted(raw_by_name.items())
        ],
        "finished_lots": [
            {
                "id": lot["values"]["lot_id"],
                "record_id": lot["id"],
            }
            for lot in sorted(stock, key=lambda item: str(item["values"]["lot_id"]))
        ],
        "allowance": _text(multiplier),
        "allowance_source": allowance_source,
        "allocation": "earliest requested delivery first, then order ID; hypothetical only",
        "as_of": sorted({str(lot["values"].get("as_of_date") or "unknown") for lot in stock + raw}),
        "excluded_demo_stock": excluded_demo_stock,
        "excluded_demo_raw": excluded_demo_raw,
        "source_counts": {
            "open_orders": len(orders),
            **source_counts,
            "finished_lots": len(stock),
            "raw_lots": len(raw),
            "material_ratios": len(ratios),
        },
    }


def material_plan_reply(plan: dict[str, Any]) -> str:
    orders = plan["orders"]
    materials = plan["materials"]
    demand = sum((Decimal(row["demand_kg"]) for row in orders), Decimal(0))
    allocated = sum((Decimal(row["finished_allocated_kg"]) for row in orders), Decimal(0))
    production = demand - allocated
    lines = [
        (
            f"Open orders need {_text(demand / KG_PER_TONNE)} t. "
            f"Eligible finished stock covers {_text(allocated / KG_PER_TONNE)} t; "
            f"new production needed is {_text(production / KG_PER_TONNE)} t."
        ),
        (
            f"Complete record snapshot: {plan['source_counts']['open_orders']} open orders, "
            f"{plan['source_counts']['finished_records']} finished records "
            f"({plan['source_counts']['finished_lots']} distinct non-demo lots), "
            f"{plan['source_counts']['raw_records']} raw records "
            f"({plan['source_counts']['raw_lots']} distinct non-demo lots), "
            f"and {plan['source_counts']['material_ratios']} material ratios."
        ),
        "Distinct non-demo finished lot IDs: "
        + ", ".join(lot["id"] for lot in plan["finished_lots"])
        + ".",
        "",
        "| Order | Delivery | Demand (t) | Finished allocated (t) | Produce (t) | Source lots |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in orders:
        lines.append(
            f"| {row['id']} | {row['delivery']} | "
            f"{_text(Decimal(row['demand_kg']) / KG_PER_TONNE)} | "
            f"{_text(Decimal(row['finished_allocated_kg']) / KG_PER_TONNE)} | "
            f"{_text(Decimal(row['production_kg']) / KG_PER_TONNE)} "
            f"| {', '.join(row['lot_ids']) or 'none'} |"
        )
    lines.extend(
        [
            "",
            "| Raw material | Ratio (kg/t finished) | Needed (t) | Available (t) | Gap (t) | Basis |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in materials:
        available = (
            "unknown"
            if row["available_kg"] is None
            else _text(Decimal(row["available_kg"]) / KG_PER_TONNE)
        )
        gap = (
            "unknown"
            if row["shortfall_kg"] is None
            else _text(Decimal(row["shortfall_kg"]) / KG_PER_TONNE)
        )
        lines.append(
            f"| {row['name']} | {row['per_tonne_kg']} "
            f"| {_text(Decimal(row['needed_kg']) / KG_PER_TONNE)} "
            f"| {available} | {gap} | {row['basis']} |"
        )
    lines.extend(
        [
            "",
            "Order-wise raw material needed (t), after the active allowance:",
            "",
            "| Material | " + " | ".join(row["id"] for row in orders) + " |",
            "| --- | " + " | ".join("---:" for _ in orders) + " |",
        ]
    )
    for row in materials:
        lines.append(
            f"| {row['name']} | "
            + " | ".join(
                _text(Decimal(item["needed_kg"]) / KG_PER_TONNE) for item in row["by_order"]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Current raw material availability (t), including materials outside this ratio:",
            "",
            "| Material | Available (t) | Lots |",
            "| --- | ---: | --- |",
        ]
    )
    for row in plan["inventory"]:
        lots = ", ".join(
            f"{lot['id']} ({_text(Decimal(lot['available_kg']) / KG_PER_TONNE)} t, {lot['status']})"
            for lot in row["lots"]
        )
        lines.append(
            f"| {row['name']} | {_text(Decimal(row['available_kg']) / KG_PER_TONNE)} | {lots} |"
        )
    lines.extend(
        [
            "",
            (
                f"Calculation: each order's production gap × saved 6N ratio × "
                f"{plan['allowance']} allowance ({plan['allowance_source']}). "
                f"Finished-stock allocation: {plan['allocation']}. "
                f"Inventory dates: {', '.join(plan['as_of'])}. "
                f"Excluded demo records: {plan['excluded_demo_stock']} finished lots, "
                f"{plan['excluded_demo_raw']} raw lots. "
                "Availability is a mass balance, not a capacity or delivery confirmation. "
                "Ratios marked ideal reaction equivalent may not be procurement quantities."
            ),
        ]
    )
    return "\n".join(lines)
