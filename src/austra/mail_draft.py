"""Reviewable email and PDF based on a verified material-plan snapshot."""

import io
import re
from decimal import Decimal
from html import escape
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)


def is_requirement_mail_request(message: str) -> bool:
    text = message.casefold()
    return bool(
        re.search(r"\b(?:draft|compose|write|prepare)\b", text)
        and re.search(r"\b(?:mail|email)\b", text)
        and re.search(r"\b(?:pdf|attachment)\b", text)
        and re.search(r"\b(?:requirement|material|stock|order)\b", text)
    )


def _tonnes(value: str | None) -> str:
    if value is None:
        return "Unknown"
    amount = Decimal(value) / Decimal(1000)
    return format(amount.normalize(), "f") if amount else "0"


def requirement_email(plan: dict[str, Any]) -> str:
    dates = ", ".join(plan["as_of"])
    lines = [
        "## Email draft for review",
        "",
        "**To:** [Add recipient]",
        "**Subject:** Review of material requirements for open sales orders",
        "",
        "Dear [Name],",
        "",
        (
            "Please review the attached material-requirements PDF for the current open sales "
            "orders. The figures below are planning estimates based on saved inventory "
            f"dated {dates}."
        ),
        "",
        "| Material | Estimated need (t) | Available (t) | Planning gap (t) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in plan["materials"]:
        lines.append(
            f"| {row['name']} | {_tonnes(row['needed_kg'])} | "
            f"{_tonnes(row['available_kg'])} | {_tonnes(row['shortfall_kg'])} |"
        )
    lines.extend(
        [
            "",
            (
                "Please confirm the required specifications, quantities to request, "
                "delivery location, and dates before this becomes a supplier inquiry. "
                "Some figures are ideal reaction equivalents and may differ from "
                "procurement quantities."
            ),
            "",
            "Regards,",
            "[Your name]",
            "",
            (
                "**Review details to fill in:** recipient and name, sender name, "
                "material specifications, delivery location, and target dates. "
                "Confirm procurement quantities for ideal reaction equivalents. "
                "No email has been sent."
            ),
        ]
    )
    return "\n".join(lines)


def requirement_pdf(plan: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "AustraTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=17,
        leading=21,
        textColor=colors.HexColor("#17324d"),
        alignment=TA_CENTER,
    )
    heading = ParagraphStyle(
        "AustraHeading",
        parent=styles["Heading2"],
        fontSize=11,
        textColor=colors.HexColor("#17324d"),
        spaceBefore=12,
        spaceAfter=6,
    )
    body = ParagraphStyle("AustraBody", parent=styles["BodyText"], fontSize=8.5, leading=12)
    cell = ParagraphStyle("AustraCell", parent=body, fontSize=7.5, leading=10)

    def paragraph(value: Any, style: ParagraphStyle = cell) -> Paragraph:
        return Paragraph(escape(str(value)), style)

    def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> LongTable:
        data = [[paragraph(item) for item in headers]]
        data.extend([[paragraph(item) for item in row] for row in rows])
        result = LongTable(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        result.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f0f7")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#17324d")),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#f7fafc")],
                    ),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#cbd8e3")),
                ]
            )
        )
        return result

    orders = plan["orders"]
    materials = plan["materials"]
    demand = sum((Decimal(row["demand_kg"]) for row in orders), Decimal(0))
    allocated = sum((Decimal(row["finished_allocated_kg"]) for row in orders), Decimal(0))
    story = [
        paragraph("Material requirements for open sales orders", title),
        Spacer(1, 5 * mm),
        paragraph(
            f"Saved inventory dates: {', '.join(plan['as_of'])}. "
            f"Open-order demand: {_tonnes(str(demand))} t; eligible finished stock allocated: "
            f"{_tonnes(str(allocated))} t; production gap: "
            f"{_tonnes(str(demand - allocated))} t.",
            body,
        ),
        paragraph("Open orders and finished stock", heading),
        table(
            ["Order", "Delivery", "Demand (t)", "Allocated (t)", "Produce (t)"],
            [
                [
                    row["id"],
                    row["delivery"],
                    _tonnes(row["demand_kg"]),
                    _tonnes(row["finished_allocated_kg"]),
                    _tonnes(row["production_kg"]),
                ]
                for row in orders
            ],
            [100, 91, 91, 91, 91],
        ),
        paragraph("Raw material coverage", heading),
        table(
            ["Material", "Need (t)", "Available (t)", "Gap (t)", "Ratio basis"],
            [
                [
                    row["name"],
                    _tonnes(row["needed_kg"]),
                    _tonnes(row["available_kg"]),
                    _tonnes(row["shortfall_kg"]),
                    row["basis"] or "Unspecified",
                ]
                for row in materials
            ],
            [137, 75, 75, 75, 102],
        ),
        paragraph("Order-wise raw needs (t)", heading),
        table(
            ["Material", *[row["id"] for row in orders]],
            [
                [row["name"], *[_tonnes(item["needed_kg"]) for item in row["by_order"]]]
                for row in materials
            ],
            [140, *([324 // len(orders)] * len(orders))],
        ),
        Spacer(1, 4 * mm),
        KeepTogether(
            [
                paragraph("Calculation and review notes", heading),
                paragraph(
                    f"Per-order production gap x saved material ratio x {plan['allowance']} "
                    f"allowance ({plan['allowance_source']}). Finished stock allocation: "
                    f"{plan['allocation']}. Excluded demo records: "
                    f"{plan['excluded_demo_stock']} finished and "
                    f"{plan['excluded_demo_raw']} raw. This is a mass balance, not a "
                    "capacity or delivery confirmation. Ideal reaction equivalents "
                    "need review before use as procurement quantities.",
                    body,
                ),
            ]
        ),
    ]
    document.build(story)
    return buffer.getvalue()
