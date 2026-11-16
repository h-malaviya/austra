"""Assign the existing polysilicon workspace to explicit department ownership."""

import json
import os
import re
from pathlib import Path
from uuid import UUID

import psycopg

DEPARTMENTS = {
    "sales": "Sales",
    "procurement": "Procurement",
    "inventory": "Inventory",
    "production": "Production",
    "planning-operations": "Planning & Operations",
}
SCHEMA_DEPARTMENT = {
    "customer_sales_order_request": "sales",
    "historical_sales_order": "sales",
    "sales_order_so_aus_2026_203": "sales",
    "historical_purchase_order": "procurement",
    "purchase_order": "procurement",
    "supplier_candidate": "procurement",
    "raw_material_inventory_lot": "inventory",
    "polysilicon_stock_lot": "inventory",
    "polysilicon_production_formula": "production",
    "polysilicon_production_material_ratio": "production",
}
NAMED_DEPARTMENT = {
    "raw_material_wastage_allowance": "production",
    "stock_display_units": "inventory",
    "sales_order_fulfillment_workflow": "planning-operations",
    "stock_vs_open_orders_lookup": "planning-operations",
}


def legacy_catalog_signature(root: Path) -> tuple[tuple[str, int], ...]:
    """Detect newly created workspace entries without opening every business file."""
    directories = []
    for current, children, _files in os.walk(root):
        children.sort()
        directory = Path(current)
        directories.append(
            (directory.relative_to(root).as_posix(), directory.stat().st_mtime_ns)
        )
    return tuple(directories)


def _chat_department(title: str) -> str:
    lower = title.casefold()
    if any(word in lower for word in ("fulfill", "material requirements", "stock vs open")):
        return "planning-operations"
    if any(word in lower for word in ("sales", "customer", "order status")):
        return "sales"
    if any(word in lower for word in ("supplier", "purchase", "quartzite order", "po detail")):
        return "procurement"
    if any(word in lower for word in ("production", "material ratio")):
        return "production"
    if any(word in lower for word in ("stock", "inventory")):
        return "inventory"
    return "planning-operations"


def catalog_legacy_workspace(
    connection: psycopg.Connection, organization_id: UUID, root: Path
) -> dict[str, int]:
    """Register existing objects without changing their source files or prior assignments."""
    departments = {}
    for slug, name in DEPARTMENTS.items():
        row = connection.execute(
            "INSERT INTO departments (organization_id, slug, name) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (organization_id, slug, name),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT id FROM departments WHERE organization_id = %s AND slug = %s "
                "AND archived_at IS NULL",
                (organization_id, slug),
            ).fetchone()
        if row is None:
            raise ValueError(f"Legacy department {slug} is archived")
        departments[slug] = row[0]

    counts: dict[str, int] = {}

    def register(kind: str, external_id: str, path: Path, slug: str | None) -> None:
        result = connection.execute(
            "INSERT INTO business_objects "
            "(organization_id, department_id, kind, external_id, source_path) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
            (
                organization_id,
                departments.get(slug),
                kind,
                external_id,
                path.relative_to(root).as_posix(),
            ),
        ).fetchone()
        if result:
            counts[kind] = counts.get(kind, 0) + 1

    chat_departments = {}
    attachment_departments: dict[str, set[str]] = {}
    for path in (root / "chats").glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        slug = _chat_department(item.get("title", ""))
        chat_departments[item["id"]] = slug
        for message in item.get("messages", []):
            attachment_ids = [entry.get("id", "") for entry in message.get("attachments", [])]
            attachment_ids.extend(re.findall(r"\(([0-9a-f]{64})\):", message.get("body", "")))
            for attachment_id in attachment_ids:
                if re.fullmatch(r"[0-9a-f]{64}", attachment_id):
                    attachment_departments.setdefault(attachment_id, set()).add(slug)
        register("chat", item["id"], path, slug)

    for path in (root / "drafts").glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        slug = SCHEMA_DEPARTMENT.get(item.get("name", ""))
        slug = slug or NAMED_DEPARTMENT.get(item.get("name", ""))
        slug = slug or chat_departments.get(item.get("chat_id", ""))
        register("draft", item["id"], path, slug)

    for path in (root / "records").glob("*/*.json"):
        register("record", path.stem, path, SCHEMA_DEPARTMENT.get(path.parent.name))

    for path in (root / "approved").glob("*/*"):
        if path.is_dir():
            slug = SCHEMA_DEPARTMENT.get(path.name) or NAMED_DEPARTMENT.get(path.name)
            register("artifact", f"{path.parent.name}:{path.name}", path, slug)
            for version in path.glob("v*.*"):
                register("artifact_version", version.relative_to(root).as_posix(), version, slug)

    for path in (root / "record_history").glob("*/*/*.json"):
        register(
            "record_history",
            path.relative_to(root).as_posix(),
            path,
            SCHEMA_DEPARTMENT.get(path.parts[-3]),
        )

    for path in (root / "memory" / "short_term").glob("*.json"):
        register("chat_memory", path.stem, path, chat_departments.get(path.stem))
    for path in (root / "memory" / "recovery").glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        register("recovery", path.stem, path, chat_departments.get(item.get("chat_id", "")))

    for path in (root / "audit").glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        register("audit", path.stem, path, chat_departments.get(item.get("chat_id", "")))

    for folder, kind in (
        ("evidence", "evidence"),
        ("generated", "generated"),
        ("evidence_text", "evidence_text"),
    ):
        for path in (root / folder).glob("*"):
            if path.is_file():
                digest = path.name.split(".", 1)[0]
                owners = attachment_departments.get(digest, set())
                register(kind, path.name, path, next(iter(owners)) if len(owners) == 1 else None)

    for folder in ("external_calls", "slack_events"):
        for path in (root / folder).glob("*"):
            if path.is_file():
                register(folder, path.name, path, None)

    return counts
