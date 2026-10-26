"""Extract selectable PDF text and OCR pages that have no text layer."""

import io
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from pypdf import PdfReader

MAX_OCR_PAGES = 50
RENDER_TIMEOUT_SECONDS = 45
OCR_TIMEOUT_SECONDS = 45
OCR_DPI = 160


class PDFExtractionError(ValueError):
    """A PDF could not be read well enough for an upload."""


def extract_pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    pages = [page.extract_text() or "" for page in reader.pages]
    missing = [index for index, text in enumerate(pages) if not text.strip()]
    if not missing:
        return "\n\n".join(pages)
    if len(missing) > MAX_OCR_PAGES:
        raise PDFExtractionError(
            f"This PDF has more than {MAX_OCR_PAGES} scanned pages. Upload a smaller section."
        )
    if shutil.which("pdftoppm") is None or shutil.which("tesseract") is None:
        raise PDFExtractionError("Scanned PDF reading is unavailable on this installation.")

    with TemporaryDirectory(prefix="austra-pdf-") as directory:
        source = Path(directory) / "source.pdf"
        source.write_bytes(content)
        for index in missing:
            page_number = index + 1
            image_prefix = Path(directory) / f"page-{page_number}"
            image_path = image_prefix.with_suffix(".png")
            try:
                subprocess.run(
                    [
                        "pdftoppm",
                        "-f",
                        str(page_number),
                        "-l",
                        str(page_number),
                        "-r",
                        str(OCR_DPI),
                        "-gray",
                        "-png",
                        "-singlefile",
                        str(source),
                        str(image_prefix),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=RENDER_TIMEOUT_SECONDS,
                )
                result = subprocess.run(
                    ["tesseract", str(image_path), "stdout", "-l", "eng", "--psm", "3"],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=OCR_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise PDFExtractionError(
                    f"Could not read scanned page {page_number}. Try a clearer PDF."
                ) from error
            pages[index] = result.stdout.strip()
    return "\n\n".join(pages)
