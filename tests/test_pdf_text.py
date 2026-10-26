import io
import subprocess

from pypdf import PdfWriter

from austra.pdf_text import extract_pdf_text


def test_textless_pdf_pages_are_read_with_ocr(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    monkeypatch.setattr("austra.pdf_text.shutil.which", lambda _name: "/usr/bin/tool")
    read_pages = []

    def run(command, **_options):
        if command[0] == "pdftoppm":
            open(f"{command[-1]}.png", "wb").close()
            return subprocess.CompletedProcess(command, 0)
        assert command[0] == "tesseract"
        page_number = int(command[1].split("page-")[-1].split(".")[0])
        read_pages.append(page_number)
        return subprocess.CompletedProcess(command, 0, stdout=f"Supplier page {page_number}")

    monkeypatch.setattr("austra.pdf_text.subprocess.run", run)
    text = extract_pdf_text(stream.getvalue())

    assert read_pages == [1, 2]
    assert text == "Supplier page 1\n\nSupplier page 2"
