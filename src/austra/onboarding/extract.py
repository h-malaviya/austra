"""Bounded extraction of untrusted source bytes with stable source locators."""

import csv
import io
import re
import zipfile
from collections.abc import Iterator

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader, PdfWriter

from austra.pdf_text import extract_pdf_text

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ROWS_PER_GROUP = 25
MAX_WORDS_PER_CHUNK = 600
MAX_CHARS_PER_CHUNK = 5000


def validate_file(name: str, content: bytes) -> str:
    suffix = name.lower().rsplit(".", 1)[-1]
    media = {
        "txt": "text/plain", "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(suffix)
    if media is None or not content:
        raise ValueError("Use a nonempty TXT, PDF, DOCX, CSV, or XLSX file")
    if suffix == "pdf" and not content.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF file")
    if suffix in {"docx", "xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > MAX_ARCHIVE_BYTES:
                    raise ValueError("Office document expands beyond the extraction limit")
                expected = "word/document.xml" if suffix == "docx" else "xl/workbook.xml"
                if expected not in archive.namelist():
                    raise ValueError("Invalid Office document")
        except zipfile.BadZipFile as error:
            raise ValueError("Invalid Office document") from error
    if suffix in {"txt", "csv"}:
        content.decode("utf-8-sig")
    return media


def _pdf_units(content: bytes) -> Iterator[tuple[str, str]]:
    reader = PdfReader(io.BytesIO(content))
    for index, page in enumerate(reader.pages, 1):
        value = page.extract_text() or ""
        if not value.strip():
            single = PdfWriter()
            single.add_page(page)
            buffer = io.BytesIO()
            single.write(buffer)
            value = extract_pdf_text(buffer.getvalue())
        yield f"page {index}", value


def _docx_units(content: bytes) -> Iterator[tuple[str, str]]:
    document = Document(io.BytesIO(content))
    heading = "document"
    for index, paragraph in enumerate(document.paragraphs, 1):
        if paragraph.style.name.lower().startswith("heading") and paragraph.text.strip():
            heading = paragraph.text.strip()
        if paragraph.text.strip():
            yield f"section {heading} · paragraph {index}", paragraph.text
    for table_index, table in enumerate(document.tables, 1):
        rows = table.rows
        if not rows:
            continue
        headers = [cell.text.strip() for cell in rows[0].cells]
        for start in range(1, len(rows), MAX_ROWS_PER_GROUP):
            group = rows[start:start + MAX_ROWS_PER_GROUP]
            value = " | ".join(headers) + "\n" + "\n".join(
                " | ".join(cell.text.strip() for cell in row.cells) for row in group
            )
            yield f"table {table_index} · rows {start + 1}-{start + len(group)}", value


def _csv_units(content: bytes) -> Iterator[tuple[str, str]]:
    reader = csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""))
    headers = next(reader, None)
    if headers is None:
        return
    group = []
    start = 2
    for row_number, row in enumerate(reader, 2):
        group.append(" | ".join(row))
        if len(group) == MAX_ROWS_PER_GROUP:
            yield f"table 1 · rows {start}-{row_number}", " | ".join(headers) + "\n" + "\n".join(group)
            group, start = [], row_number + 1
    if group:
        yield f"table 1 · rows {start}-{start + len(group) - 1}", " | ".join(headers) + "\n" + "\n".join(group)


def _xlsx_units(content: bytes) -> Iterator[tuple[str, str]]:
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        for sheet in workbook:
            rows = sheet.iter_rows(values_only=True)
            headers = next(rows, None)
            if headers is None:
                continue
            header_line = " | ".join("" if value is None else str(value) for value in headers)
            group = []
            start = 2
            for row_number, row in enumerate(rows, 2):
                group.append(" | ".join("" if value is None else str(value) for value in row))
                if len(group) == MAX_ROWS_PER_GROUP:
                    yield f"sheet {sheet.title} · rows {start}-{row_number}", header_line + "\n" + "\n".join(group)
                    group, start = [], row_number + 1
            if group:
                yield f"sheet {sheet.title} · rows {start}-{start + len(group) - 1}", header_line + "\n" + "\n".join(group)
    finally:
        workbook.close()


def units(media_type: str, content: bytes) -> Iterator[tuple[str, str]]:
    if media_type == "application/pdf":
        yield from _pdf_units(content)
    elif media_type.endswith("wordprocessingml.document"):
        yield from _docx_units(content)
    elif media_type.endswith("spreadsheetml.sheet"):
        yield from _xlsx_units(content)
    elif media_type == "text/csv":
        yield from _csv_units(content)
    else:
        yield "text", content.decode("utf-8-sig", errors="replace")


def chunks(locator: str, content: str) -> Iterator[tuple[str, str]]:
    paragraphs = re.split(r"\n\s*\n", content.strip())
    part = 0
    words = []
    character_count = 0
    for paragraph in paragraphs:
        for clause in re.split(r"(?<=[.!?;])\s+", paragraph):
            for match in re.finditer(r"\S+", clause):
                raw_word = match.group()
                for start in range(0, len(raw_word), MAX_CHARS_PER_CHUNK):
                    word = raw_word[start:start + MAX_CHARS_PER_CHUNK]
                    if words and (len(words) >= MAX_WORDS_PER_CHUNK
                                  or character_count + len(word) + 1 > MAX_CHARS_PER_CHUNK):
                        part += 1
                        yield f"{locator} · part {part}", " ".join(words)
                        words = []
                        character_count = 0
                    words.append(word)
                    character_count += len(word) + (1 if character_count else 0)
    if words:
        part += 1
        yield f"{locator} · part {part}", " ".join(words)
