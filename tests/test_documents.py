import io

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from austra.documents import DocumentLibrary
from austra.store import Store
from austra.web import create_app


def test_csv_upload_is_searchable_and_linked_to_case(tmp_path, monkeypatch):
    monkeypatch.setenv("AUSTRA_MANAGER_TOKEN", "test-secret")
    client = TestClient(create_app(tmp_path / "business.db"))
    case = client.post("/api/cases", json={"message": "Find quartz supplier"}).json()

    response = client.post(
        "/api/admin/documents",
        headers={"X-Manager-Token": "test-secret"},
        data={"uploaded_by": "Manager A", "case_id": case["id"]},
        files={"file": ("suppliers.csv", b"name,material\nAcme,quartz\n", "text/csv")},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "indexed"
    assert (
        client.get("/api/documents/search", params={"q": "quartz supplier"}).json()[0]["id"]
        == response.json()["id"]
    )
    assert (
        client.get(f"/api/cases/{case['id']}").json()["documents"][0]["id"] == response.json()["id"]
    )
    assert client.get(f"/api/admin/documents/{response.json()['id']}").status_code == 403
    preview = client.get(
        f"/api/admin/documents/{response.json()['id']}",
        headers={"X-Manager-Token": "test-secret"},
    )
    assert "Acme" in preview.json()["extracted_text"]


def test_docx_and_image_only_pdf_have_distinct_status(tmp_path):
    store = Store(tmp_path / "business.db")
    library = DocumentLibrary(store, tmp_path / "documents")
    document = Document()
    document.add_paragraph("Supplier Alpha sells quartz")
    docx = io.BytesIO()
    document.save(docx)
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    pdf_bytes = io.BytesIO()
    pdf.write(pdf_bytes)

    indexed = library.add("supplier.docx", docx.getvalue(), "Manager A")
    needs_review = library.add("scan.pdf", pdf_bytes.getvalue(), "Manager A")

    assert indexed["status"] == "indexed"
    assert needs_review["status"] == "needs_text_review"
    assert library.search("quartz")[0]["id"] == indexed["id"]
