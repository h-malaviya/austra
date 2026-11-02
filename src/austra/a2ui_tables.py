"""Turn completed Markdown tables into accessible A2UI report sections."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_VISUAL_ROWS = 200
MAX_CHART_ROWS = 20
MAX_VISUAL_TABLES = 8
SEPARATOR = re.compile(r"^:?-{3,}:?$")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
NUMBER = re.compile(r"^\s*[$₹]?\s*([\d,]+(?:\.\d+)?)\s*(?:kg|t|%|usd)?\s*$", re.IGNORECASE)


def _cells(line: str) -> list[str]:
    cells = [cell.strip().replace(r"\|", "|") for cell in CELL_SPLIT.split(line.strip("| "))]
    return [cell[2:-2] if cell.startswith("**") and cell.endswith("**") else cell for cell in cells]


def markdown_tables(body: str) -> list[dict[str, Any]]:
    lines = body.splitlines()
    tables = []
    index = 0
    last_table_end = 0
    while index + 2 < len(lines) and len(tables) < MAX_VISUAL_TABLES:
        if "|" not in lines[index] or "|" not in lines[index + 1]:
            index += 1
            continue
        headers = _cells(lines[index])
        separator = _cells(lines[index + 1])
        if (
            len(headers) < 2
            or len(separator) != len(headers)
            or not all(SEPARATOR.fullmatch(cell) for cell in separator)
        ):
            index += 1
            continue
        start_line = index
        title = next(
            (line.strip().lstrip("#* ") for line in reversed(lines[:index]) if line.strip()),
            f"Table {len(tables) + 1}",
        )
        rows = []
        omitted_rows = 0
        index += 2
        while index < len(lines) and "|" in lines[index]:
            cells = _cells(lines[index])
            if len(cells) == len(headers):
                rows.append(cells)
            else:
                omitted_rows += 1
            index += 1
        if rows:
            tables.append(
                {
                    "title": title[:80],
                    "headers": headers,
                    "rows": rows,
                    "start_line": start_line,
                    "omitted_rows": omitted_rows,
                }
            )
            last_table_end = index
    if tables:
        tables[-1]["tail"] = lines[last_table_end:]
    return tables


def table_summary(body: str) -> str:
    lines = []
    for line in body.splitlines():
        if "|" in line:
            break
        if line.strip():
            lines.append(line.strip())
    return " ".join(lines)[:300] or "Interactive report"


def visual_is_complete(body: str, tables: list[dict[str, Any]]) -> bool:
    if len(tables) != 1:
        return False
    table = tables[0]
    prefix = " ".join(
        line.strip() for line in body.splitlines()[: table["start_line"]] if line.strip()
    )
    tail_bullets = sum(line.strip().startswith(("- ", "* ")) for line in table.get("tail", []))
    return (
        len(prefix) <= 300
        and len(table["rows"]) <= MAX_VISUAL_ROWS
        and not table["omitted_rows"]
        and tail_bullets <= MAX_VISUAL_ROWS
    )


def _numeric(value: str) -> Decimal | None:
    match = NUMBER.fullmatch(value.replace("**", ""))
    if match is None:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _tail_components(surface: Any, lines: list[str]) -> list[str]:
    components = []
    heading = ""
    bullets: list[str] = []

    def flush_bullets() -> None:
        nonlocal heading
        if not bullets:
            return
        if all(" — " in item for item in bullets):
            components.append(
                surface.add(
                    "AustraDataTable",
                    title=heading or "Additional details",
                    headers=["Item", "Details"],
                    rows=[item.split(" — ", 1) for item in bullets[:MAX_VISUAL_ROWS]],
                )
            )
        else:
            if heading:
                components.append(surface.text(heading, "h4"))
            components.extend(surface.text(f"• {item}") for item in bullets[:MAX_VISUAL_ROWS])
        if len(bullets) > MAX_VISUAL_ROWS:
            components.append(surface.text("More details are in the full answer."))
        bullets.clear()
        heading = ""

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("- ", "* ")):
            bullets.append(line[2:].strip())
            continue
        flush_bullets()
        title = re.fullmatch(r"\*\*(.+)\*\*", line)
        if title or line.startswith("#"):
            if heading:
                components.append(surface.text(heading, "h4"))
            heading = (title.group(1) if title else line.lstrip("# ")).rstrip(":")
        else:
            if heading:
                components.append(surface.text(heading, "h4"))
                heading = ""
            components.append(surface.text(line))
    flush_bullets()
    if heading:
        components.append(surface.text(heading, "h4"))
    return components


def table_root(surface: Any, tables: list[dict[str, Any]]) -> str:
    tabs = []
    for index, table in enumerate(tables, 1):
        headers = table["headers"]
        rows = table["rows"]
        numeric_columns = [
            column
            for column in range(1, len(headers))
            if all(_numeric(row[column]) is not None for row in rows)
        ]
        numeric_index = next(
            (column for column in numeric_columns if "available" in headers[column].casefold()),
            None,
        )
        if numeric_index is None:
            numeric_index = next(
                (
                    column
                    for column in numeric_columns
                    if any(
                        word in headers[column].casefold()
                        for word in (
                            "qty",
                            "quantity",
                            "demand",
                            "needed",
                            "available",
                            "total",
                            "gap",
                            "stock",
                        )
                    )
                ),
                None,
            )
        children = []
        chart_rows = (
            sorted(
                rows[:MAX_VISUAL_ROWS],
                key=lambda row: _numeric(row[numeric_index]),
                reverse=True,
            )[:MAX_CHART_ROWS]
            if numeric_index is not None
            else []
        )
        if chart_rows and _numeric(chart_rows[0][numeric_index]) > 0:
            unit_match = re.search(r"\((kg|t|%)\)", headers[numeric_index], re.IGNORECASE)
            children.append(
                surface.add(
                    "AustraBarChart",
                    title=(
                        "Available stock"
                        if "available" in headers[numeric_index].casefold()
                        else headers[numeric_index]
                    ),
                    mode="single",
                    unit=unit_match.group(1) if unit_match else "",
                    rows=[
                        {
                            "label": row[0],
                            "first": str(_numeric(row[numeric_index])),
                            "second": None,
                            "first_label": headers[numeric_index],
                            "second_label": "",
                            "details": [
                                {"label": header, "value": value}
                                for header, value in zip(headers[1:], row[1:], strict=True)
                            ],
                        }
                        for row in chart_rows
                    ],
                )
            )
            if len(rows) > MAX_CHART_ROWS:
                children.append(
                    surface.text(
                        f"Chart shows the {MAX_CHART_ROWS} largest values; the table includes more."
                    )
                )
        subject = re.sub(r"\bID\b", "", headers[0], flags=re.IGNORECASE).strip()
        children.append(
            surface.add(
                "AustraDataTable",
                title=(table["title"] if len(table["title"]) <= 48 else f"{subject} details"),
                headers=headers,
                rows=rows[:MAX_VISUAL_ROWS],
            )
        )
        children.extend(_tail_components(surface, table.get("tail", [])))
        if len(rows) > MAX_VISUAL_ROWS:
            children.append(
                surface.text(
                    f"Showing {MAX_VISUAL_ROWS} of {len(rows)} rows. View the full answer."
                )
            )
        tab_title = table["title"][:24] if len(table["title"]) <= 48 else subject[:24]
        tabs.append({"title": tab_title or f"Table {index}", "child": surface.column(children)})
    return tabs[0]["child"] if len(tabs) == 1 else surface.add("Tabs", tabs=tabs)
