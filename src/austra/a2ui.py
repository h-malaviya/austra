"""A2UI surfaces for authenticated Austra workspace views."""

import json
from decimal import Decimal
from itertools import islice
from typing import Any

from austra.a2ui_tables import (
    MAX_CHART_ROWS,
    MAX_VISUAL_ROWS,
    markdown_tables,
    table_root,
    visual_is_complete,
)
from austra.files import Files

CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
REPORT_CATALOG_ID = "https://austra.local/catalogs/report.json"
VERSION = "v0.9.1"
PAGE_SIZE = 20
CHAT_RECORD_PREVIEW_COUNT = 5
ARTIFACT_PREVIEW_CHARS = 400
RECORD_PREVIEW_FIELDS = 12
RECORD_VALUE_PREVIEW_CHARS = 400


class Surface:
    def __init__(self, surface_id: str, catalog_id: str = CATALOG_ID) -> None:
        self.id = surface_id
        self.catalog_id = catalog_id
        self.components: list[dict[str, Any]] = []

    def add(self, kind: str, **properties: Any) -> str:
        component_id = f"item_{len(self.components)}"
        self.components.append({"id": component_id, "component": kind, **properties})
        return component_id

    def text(self, value: str, variant: str = "body") -> str:
        return self.add("Text", text=value, variant=variant)

    def column(self, children: list[str]) -> str:
        return self.add("Column", children=children)

    def card(self, children: list[str]) -> str:
        return self.add("Card", child=self.column(children))

    def button(self, label: str, action: str, context: dict[str, Any]) -> str:
        return self.add(
            "Button",
            child=self.text(label),
            variant="primary" if action == "approve_draft" else "default",
            action={"event": {"name": action, "context": context}},
        )

    def messages(self, root: str) -> list[dict[str, Any]]:
        components = [
            {**component, "id": "root" if component["id"] == root else component["id"]}
            for component in self.components
        ]
        return [
            {
                "version": VERSION,
                "createSurface": {"surfaceId": self.id, "catalogId": self.catalog_id},
            },
            {
                "version": VERSION,
                "updateComponents": {"surfaceId": self.id, "components": components},
            },
        ]


def _draft_card(surface: Surface, draft: dict[str, Any]) -> str:
    children = [
        surface.text(f"{draft['kind'].replace('_', ' ').title()} · {draft['name']}", "h4"),
        surface.text(draft["summary"]),
        surface.text(draft["status"].title(), "caption"),
    ]
    if draft["status"] == "proposed":
        buttons = [
            surface.button("Approve", "approve_draft", {"draft_id": draft["id"]}),
            surface.button("Reject", "reject_draft", {"draft_id": draft["id"]}),
        ]
        children.append(surface.add("Row", children=buttons))
    return surface.card(children)


def chat_surfaces(files: Files, chat_id: str) -> list[dict[str, Any]]:
    chat = files.chat(chat_id)
    surfaces = []
    for message in chat["messages"]:
        if message["role"] == "assistant" and isinstance(message.get("report"), dict):
            surface = Surface(f"report_{message['id']}", REPORT_CATALOG_ID)
            surfaces.append(
                {
                    "id": surface.id,
                    "kind": "report",
                    "message_id": message["id"],
                    "visual_complete": _material_report_complete(message["report"]),
                    "messages": surface.messages(_material_report(surface, message["report"])),
                }
            )
        elif message["role"] == "assistant" and message.get("visualize"):
            tables = markdown_tables(message["body"])
            if tables:
                surface = Surface(f"tables_{message['id']}", REPORT_CATALOG_ID)
                surfaces.append(
                    {
                        "id": surface.id,
                        "kind": "report",
                        "message_id": message["id"],
                        "visual_complete": visual_is_complete(message["body"], tables),
                        "messages": surface.messages(table_root(surface, tables)),
                    }
                )
    for draft in files.drafts(chat_id):
        if draft["kind"] in {"schema", "record"}:
            continue
        surface = Surface(f"draft_{draft['id']}")
        surfaces.append(
            {
                "id": surface.id,
                "kind": "draft",
                "source": draft["content"],
                "messages": surface.messages(_draft_card(surface, draft)),
            }
        )
    records = [record for record in files.records() if record.get("source_chat") == chat_id]
    if records:
        surface = Surface(f"records_{chat_id}")
        heading = surface.text(f"Saved records · {len(records)}", "h4")
        cards = [_record_card(surface, record) for record in records[-CHAT_RECORD_PREVIEW_COUNT:]]
        if len(records) > CHAT_RECORD_PREVIEW_COUNT:
            cards.append(
                surface.text(
                    f"Showing the latest {CHAT_RECORD_PREVIEW_COUNT}. "
                    "Open Workspace to browse all records."
                )
            )
        surfaces.append(
            {
                "id": surface.id,
                "kind": "records",
                "messages": surface.messages(surface.column([heading, *cards])),
            }
        )
    if chat["messages"] and chat["messages"][-1].get("needs_input"):
        message = chat["messages"][-1]
        surface = Surface(f"answer_{message['id']}")
        field = surface.add("TextField", label="Your answer", value={"path": "/answer"})
        button = surface.button("Send answer", "submit_answer", {"answer": {"path": "/answer"}})
        root = surface.card([field, button])
        messages = surface.messages(root)
        messages.insert(
            1,
            {
                "version": VERSION,
                "updateDataModel": {"surfaceId": surface.id, "path": "/answer", "value": ""},
            },
        )
        surfaces.append({"id": surface.id, "kind": "answer", "messages": messages})
    return surfaces


def _tonnes(value: str | None) -> str:
    if value is None:
        return "Unknown"
    tonnes = format(Decimal(value) / 1000, "f")
    if "." in tonnes:
        tonnes = tonnes.rstrip("0").rstrip(".")
    return f"{tonnes} t"


def _tonnes_number(value: str | None) -> str:
    return _tonnes(value).removesuffix(" t")


def _material_report_complete(report: dict[str, Any]) -> bool:
    return (
        not report.get("extension")
        and len(report["orders"]) <= MAX_VISUAL_ROWS
        and len(report["materials"]) <= MAX_VISUAL_ROWS
        and len(report["inventory"]) <= MAX_VISUAL_ROWS
        and sum(len(row["by_order"]) for row in report["materials"]) <= MAX_VISUAL_ROWS
        and sum(len(row["lots"]) for row in report["inventory"]) <= MAX_VISUAL_ROWS
        and len(report.get("supplier_shortlist", {}).get("rows", [])) <= MAX_VISUAL_ROWS
    )


def _material_report(surface: Surface, report: dict[str, Any]) -> str:
    orders = report["orders"]
    materials = report["materials"]
    demand = sum((Decimal(row["demand_kg"]) for row in orders), Decimal(0))
    allocated = sum((Decimal(row["finished_allocated_kg"]) for row in orders), Decimal(0))
    overview = [
        surface.text("Stock and open orders", "h3"),
        surface.text(
            f"Demand {_tonnes(str(demand))} · eligible finished stock "
            f"{_tonnes(str(allocated))} · produce {_tonnes(str(demand - allocated))}"
        ),
    ]
    overview.append(
        surface.add(
            "AustraBarChart",
            title="Demand by order",
            mode="stacked",
            unit="t",
            rows=[
                {
                    "label": row["id"],
                    "first": str(Decimal(row["finished_allocated_kg"]) / 1000),
                    "second": str(Decimal(row["production_kg"]) / 1000),
                    "first_label": "Finished stock",
                    "second_label": "To produce",
                    "details": [
                        {"label": "Demand", "value": _tonnes(row["demand_kg"])},
                        {"label": "Finished stock", "value": _tonnes(row["finished_allocated_kg"])},
                        {"label": "To produce", "value": _tonnes(row["production_kg"])},
                        {"label": "Delivery", "value": row["delivery"]},
                    ],
                }
                for row in orders[:MAX_CHART_ROWS]
            ],
        )
    )
    if len(orders) > MAX_CHART_ROWS:
        overview.append(
            surface.text(f"Chart shows the first {MAX_CHART_ROWS} orders; see the Orders table.")
        )
    if len(orders) > MAX_VISUAL_ROWS:
        overview.append(
            surface.text(f"Showing {MAX_VISUAL_ROWS} of {len(orders)} orders. View full answer.")
        )
    order_cards = [
        surface.add(
            "AustraDataTable",
            title="Order-wise production gaps",
            headers=[
                "Order",
                "Demand (t)",
                "Finished stock (t)",
                "To produce (t)",
                "Delivery",
                "Lots",
            ],
            rows=[
                [
                    row["id"],
                    _tonnes_number(row["demand_kg"]),
                    _tonnes_number(row["finished_allocated_kg"]),
                    _tonnes_number(row["production_kg"]),
                    row["delivery"],
                    ", ".join(row["lot_ids"]) or "None",
                ]
                for row in orders[:MAX_VISUAL_ROWS]
            ],
        )
    ]
    if len(orders) > MAX_VISUAL_ROWS:
        order_cards.append(
            surface.text(f"Showing {MAX_VISUAL_ROWS} of {len(orders)} orders. View full answer.")
        )
    material_cards = [
        surface.add(
            "AustraBarChart",
            title="Needed versus available",
            mode="paired",
            unit="t",
            rows=[
                {
                    "label": row["name"].title(),
                    "first": str(Decimal(row["needed_kg"]) / 1000),
                    "second": (
                        None
                        if row["available_kg"] is None
                        else str(Decimal(row["available_kg"]) / 1000)
                    ),
                    "first_label": "Needed",
                    "second_label": "Available",
                    "details": [
                        {"label": "Needed", "value": _tonnes(row["needed_kg"])},
                        {"label": "Available", "value": _tonnes(row["available_kg"])},
                        {"label": "Gap", "value": _tonnes(row["shortfall_kg"])},
                        {"label": "Ratio", "value": f"{row['per_tonne_kg']} kg/t"},
                    ],
                }
                for row in materials[:MAX_CHART_ROWS]
            ],
        ),
        surface.add(
            "AustraDataTable",
            title="Raw material coverage",
            headers=["Material", "Needed (t)", "Available (t)", "Gap (t)", "Ratio (kg/t)", "Basis"],
            rows=[
                [
                    row["name"].title(),
                    _tonnes_number(row["needed_kg"]),
                    _tonnes_number(row["available_kg"]),
                    _tonnes_number(row["shortfall_kg"]),
                    row["per_tonne_kg"],
                    row["basis"],
                ]
                for row in materials[:MAX_VISUAL_ROWS]
            ],
        ),
    ]
    if len(materials) > MAX_CHART_ROWS:
        material_cards.append(
            surface.text(
                f"Chart shows the first {MAX_CHART_ROWS} materials; see the coverage table."
            )
        )
    by_order = [
        [item["order_id"], row["name"].title(), _tonnes_number(item["needed_kg"])]
        for row in materials
        for item in row["by_order"]
    ]
    if by_order:
        material_cards.append(
            surface.add(
                "AustraDataTable",
                title="Requirements by order",
                headers=["Order", "Material", "Needed (t)"],
                rows=by_order[:MAX_VISUAL_ROWS],
            )
        )
    if len(materials) > MAX_VISUAL_ROWS:
        material_cards.append(
            surface.text(
                f"Showing {MAX_VISUAL_ROWS} of {len(materials)} materials. View full answer."
            )
        )
    inventory_cards = []
    if report["inventory"]:
        inventory_cards.append(
            surface.add(
                "AustraBarChart",
                title="Available raw stock",
                mode="single",
                unit="t",
                rows=[
                    {
                        "label": row["name"].title(),
                        "first": str(Decimal(row["available_kg"]) / 1000),
                        "second": None,
                        "first_label": "Available",
                        "second_label": "",
                        "details": [
                            {"label": "Available", "value": _tonnes(row["available_kg"])},
                            {"label": "Lots", "value": str(len(row["lots"]))},
                        ],
                    }
                    for row in report["inventory"][:MAX_CHART_ROWS]
                ],
            )
        )
        if len(report["inventory"]) > MAX_CHART_ROWS:
            inventory_cards.append(
                surface.text(
                    f"Chart shows the first {MAX_CHART_ROWS} materials; see the inventory table."
                )
            )
    inventory_cards.append(
        surface.add(
            "AustraDataTable",
            title="Current available raw inventory",
            headers=["Material", "Available (t)", "Lots"],
            rows=[
                [
                    row["name"].title(),
                    _tonnes_number(row["available_kg"]),
                    str(len(row["lots"])),
                ]
                for row in report["inventory"][:MAX_VISUAL_ROWS]
            ],
        )
    )
    lot_rows = [
        [row["name"].title(), lot["id"], _tonnes_number(lot["available_kg"]), lot["status"]]
        for row in report["inventory"]
        for lot in row["lots"]
    ]
    if lot_rows:
        inventory_cards.append(
            surface.add(
                "AustraDataTable",
                title="Inventory lots",
                headers=["Material", "Lot", "Available (t)", "Status"],
                rows=lot_rows[:MAX_VISUAL_ROWS],
            )
        )
    if len(report["inventory"]) > MAX_VISUAL_ROWS:
        inventory_cards.append(
            surface.text(
                f"Showing {MAX_VISUAL_ROWS} of {len(report['inventory'])} inventory materials. "
                "View full answer."
            )
        )
    method = surface.column(
        [
            surface.text("Calculation basis", "h3"),
            surface.text(
                f"Complete record snapshot: {report['source_counts']['open_orders']} open orders, "
                f"{report['source_counts']['finished_records']} finished records "
                f"({report['source_counts']['finished_lots']} distinct non-demo lots), "
                f"{report['source_counts']['raw_records']} raw records "
                f"({report['source_counts']['raw_lots']} distinct non-demo lots), "
                f"and {report['source_counts']['material_ratios']} material ratios."
            ),
            surface.text(
                f"Each order's production gap × saved 6N ratio × {report['allowance']} allowance."
            ),
            surface.text(f"Allocation: {report['allocation']}."),
            surface.text(
                f"Source policy: {report['allowance_source']}. Inventory dates: {', '.join(report['as_of'])}."
            ),
            surface.text(
                f"Excluded demo records: {report['excluded_demo_stock']} finished lots, "
                f"{report['excluded_demo_raw']} raw lots."
            ),
            surface.text(
                "Distinct non-demo finished lot IDs: "
                + ", ".join(lot["id"] for lot in report["finished_lots"])
            ),
            surface.text(
                "This is a mass balance. It does not confirm production capacity, delivery dates, or purchase authority. Ratios marked ideal reaction equivalent may not be procurement quantities."
            ),
        ]
    )
    tabs = [
        {"title": title, "child": surface.column(children)}
        for title, children in (
            ("Overview", overview),
            ("Orders", order_cards),
            ("Materials", material_cards),
            ("Inventory", inventory_cards),
        )
    ]
    if "supplier_shortlist" in report:
        shortlist = report["supplier_shortlist"]
        supplier_children = [surface.text("Supplier candidates for material gaps", "h3")]
        if shortlist["rows"]:
            supplier_children.append(
                surface.add(
                    "AustraDataTable",
                    title="Candidate sources and reasons",
                    headers=[
                        "Material",
                        "Planning gap (t)",
                        "Supplier ID",
                        "Candidate",
                        "Location",
                        "Reason",
                        "Status",
                    ],
                    rows=[
                        [
                            row[field]
                            for field in (
                                "material",
                                "gap_t",
                                "supplier_id",
                                "supplier",
                                "location",
                                "reason",
                                "status",
                            )
                        ]
                        for row in shortlist["rows"][:MAX_VISUAL_ROWS]
                    ],
                )
            )
        else:
            supplier_children.append(surface.text("No verified material gap needs a supplier."))
        supplier_children.append(surface.text(shortlist["note"]))
        tabs.append({"title": "Suppliers", "child": surface.column(supplier_children)})
    if report.get("extension"):
        extension = report["extension"]
        extra_tables = markdown_tables(extension)
        extra_children = []
        if extra_tables:
            prefix = "\n".join(extension.splitlines()[: extra_tables[0]["start_line"]]).strip()
            if prefix:
                extra_children.append(surface.text(prefix))
            extra_children.append(table_root(surface, extra_tables))
        else:
            extra_children.append(surface.text(extension))
        tabs.append({"title": "Additional", "child": surface.column(extra_children)})
    tabs.append({"title": "Method", "child": method})
    return surface.add("Tabs", tabs=tabs)


def _record_card(surface: Surface, record: dict[str, Any]) -> str:
    children = [surface.text(record["schema"].replace("_", " ").title(), "h4")]
    values = record.get("values", {})
    for key, value in islice(values.items(), RECORD_PREVIEW_FIELDS):
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        children.append(
            surface.text(f"{key.replace('_', ' ')}: {shown[:RECORD_VALUE_PREVIEW_CHARS]}")
        )
    if len(values) > RECORD_PREVIEW_FIELDS:
        children.append(surface.text(f"+ {len(values) - RECORD_PREVIEW_FIELDS} more fields"))
    children.append(surface.text(f"Record {record['id']}", "caption"))
    return surface.card(children)


def _artifact_card(surface: Surface, artifact: dict[str, Any]) -> str:
    label = f"{artifact['kind'].title()} · {artifact['name']} · v{artifact['version']}"
    return surface.card(
        [surface.text(label, "h4"), surface.text(artifact["content"][:ARTIFACT_PREVIEW_CHARS])]
    )


def _activity_card(surface: Surface, event: dict[str, Any]) -> str:
    label = event.get("action", "Activity").replace(".", " ").replace("_", " ").title()
    target = f"{event.get('target_kind', '')}: {event.get('target_name', '')}".strip(": ")
    return surface.card(
        [
            surface.text(label, "h4"),
            surface.text(target),
            surface.text(event.get("at", ""), "caption"),
        ]
    )


def workspace_page(files: Files, section: str, offset: int = 0) -> dict[str, Any]:
    if section == "records":
        items = files.records()
        render_item = _record_card
    elif section == "files":
        items = files.approved()
        render_item = _artifact_card
    elif section == "drafts":
        items = [item for item in files.drafts() if item["status"] == "proposed"]
        render_item = _draft_card
    elif section == "activity":
        items = files.audit_entries()
        render_item = _activity_card
    else:
        raise ValueError("Unknown workspace section")
    if section != "activity":
        items = list(reversed(items))
    page = items[offset : offset + PAGE_SIZE]
    surface = Surface(f"workspace_{section}_{offset}")
    cards = [render_item(surface, item) for item in page]
    if not cards:
        cards = [surface.text("Nothing here yet.")]
    root = surface.column(cards)
    return {
        "section": section,
        "offset": offset,
        "page_size": PAGE_SIZE,
        "total": len(items),
        "items": [
            {
                "id": item.get("id"),
                "title": (
                    f"{item['schema']} · {item['id']}"
                    if section == "records"
                    else f"{item['kind']} · {item['name']}"
                ),
                "source": (
                    json.dumps(item["values"], ensure_ascii=False, indent=2)
                    if section == "records"
                    else item.get("content")
                ),
            }
            for item in page
            if section in {"records", "files", "drafts"}
        ],
        "surface": {"id": surface.id, "messages": surface.messages(root)},
    }
