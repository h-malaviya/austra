"""Evidence-backed supplier shortlists for verified material gaps."""

from decimal import Decimal
from typing import Any

KG_PER_TONNE = Decimal(1000)


def _material_key(value: str) -> str:
    return value.casefold().replace("(anhydrous)", "").strip()


def _tonnes(value: str) -> str:
    number = Decimal(value) / KG_PER_TONNE
    return format(number.normalize(), "f") if number else "0"


def supplier_shortlist(plan: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    candidates: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record["schema"] != "supplier_candidate":
            continue
        values = record["values"]
        category = values.get("material_category")
        if isinstance(category, str):
            candidates.setdefault(_material_key(category), []).append(values)

    rows = []
    for material in plan["materials"]:
        gap = material["shortfall_kg"]
        if gap is not None and Decimal(gap) <= 0:
            continue
        name = material["name"]
        gap_t = "Unknown" if gap is None else _tonnes(gap)
        for candidate in sorted(
            candidates.get(_material_key(name), []),
            key=lambda item: str(item.get("supplier_id", "")),
        ):
            notes = str(candidate.get("notes", ""))
            reason = f"Listed for {name}; planning gap {gap_t} t."
            if "internal intermediate" in notes.casefold():
                reason = f"Backup external source for an internal intermediate; gap {gap_t} t."
            if material["basis"] == "ideal reaction equivalent":
                reason += " Confirm the procurement basis."
            lowered_notes = notes.casefold()
            pending = []
            if "not contacted" in lowered_notes:
                pending.append("Not contacted")
            if "qualified" in lowered_notes and "not contacted" in lowered_notes:
                pending.append("Qualification unconfirmed")
            if "price-checked" in lowered_notes and "not contacted" in lowered_notes:
                pending.append("Price unchecked")
            if "approved" in lowered_notes and "not contacted" in lowered_notes:
                pending.append("Approval pending")
            status = "; ".join(pending) or str(
                candidate.get("qualification_status", "Unknown")
            ).replace("_", " ")
            rows.append(
                {
                    "material": name,
                    "gap_t": gap_t,
                    "supplier_id": str(candidate.get("supplier_id", "")),
                    "supplier": str(candidate.get("supplier_name", "")),
                    "location": ", ".join(
                        str(candidate[field]) for field in ("city", "state") if candidate.get(field)
                    ),
                    "reason": reason,
                    "status": status,
                }
            )
        if not candidates.get(_material_key(name)):
            rows.append(
                {
                    "material": name,
                    "gap_t": gap_t,
                    "supplier_id": "",
                    "supplier": "No saved candidate",
                    "location": "",
                    "reason": "No matching supplier directory entry for this material gap.",
                    "status": "Unknown",
                }
            )
    return {
        "rows": rows,
        "note": (
            "These are candidate sources, not a ranked or approved purchase recommendation. "
            "Confirm current price, lead time, capacity, and qualification before selection. "
            "Planning gaps exclude QC-held stock; reaction-equivalent ratios may differ from procurement quantities."
        ),
    }


def supplier_shortlist_reply(shortlist: dict[str, Any]) -> str:
    if not shortlist["rows"]:
        return "No verified raw-material gap needs a supplier."

    def cell(value: str) -> str:
        return value.replace("|", r"\|").replace("\n", " ")

    lines = [
        "Supplier candidates for raw-material gaps",
        "",
        "| Material | Planning gap (t) | Supplier ID | Candidate | Location | Reason | Status |",
        "| --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for row in shortlist["rows"]:
        lines.append(
            "| "
            + " | ".join(
                cell(str(row[field]))
                for field in (
                    "material",
                    "gap_t",
                    "supplier_id",
                    "supplier",
                    "location",
                    "reason",
                    "status",
                )
            )
            + " |"
        )
    lines.extend(["", shortlist["note"]])
    return "\n".join(lines)
