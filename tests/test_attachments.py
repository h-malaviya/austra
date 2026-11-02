import hashlib
import io
import json
import threading
import time

from docx import Document
from fastapi.testclient import TestClient

from austra.agent import AgentReply, Draft
from austra.web import _extract_attachment, create_app


def test_multi_file_submission_analyzes_once_and_skips_reuploads(tmp_path):
    analyzed = []

    def planner(_files, _access, chat_id):
        analyzed.append(chat_id)
        return AgentReply(reply="Files received")

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    first = client.post(
        "/api/messages",
        data={"body": "Read these reports"},
        files=[
            ("uploads", ("one.csv", b"name,value\nalpha,1", "text/csv")),
            ("uploads", ("two.csv", b"name,value\nbeta,2", "text/csv")),
        ],
    )
    assert first.status_code == 200
    chat = first.json()["chat"]
    assert len(analyzed) == 1
    assert len(chat["messages"]) == 2
    assert chat["messages"][0]["display_body"] == "Read these reports"
    assert [item["name"] for item in chat["messages"][0]["attachments"]] == [
        "one.csv",
        "two.csv",
    ]
    assert "alpha,1" in chat["messages"][0]["body"]
    assert len(list((tmp_path / "evidence").iterdir())) == 2

    duplicate = client.post(
        "/api/messages",
        data={"chat_id": chat["id"]},
        files=[("uploads", ("renamed.csv", b"name,value\nalpha,1", "text/csv"))],
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["unchanged"] is True
    assert duplicate.json()["duplicate_files"] == ["renamed.csv"]
    assert len(duplicate.json()["chat"]["messages"]) == 2
    assert len(analyzed) == 1
    assert len(list((tmp_path / "evidence").iterdir())) == 2

    legacy_route = client.post(
        f"/api/chats/{chat['id']}/attachments",
        files={"upload": ("one.csv", b"name,value\nalpha,1", "text/csv")},
    )
    assert legacy_route.json()["unchanged"] is True
    assert len(analyzed) == 1


def test_file_only_submission_and_duplicate_in_same_batch(tmp_path):
    client = TestClient(
        create_app(
            tmp_path,
            planner=lambda _files, _access, _chat: AgentReply(reply="Reviewed"),
        )
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    content = b"item,quantity\nquartz,3"
    response = client.post(
        "/api/messages",
        files=[
            ("uploads", ("stock.csv", content, "text/csv")),
            ("uploads", ("copy.csv", content, "text/csv")),
        ],
    )
    assert response.status_code == 200
    result = response.json()
    assert result["duplicate_files"] == ["copy.csv"]
    assert result["chat"]["title"] == "Uploaded stock.csv"
    assert result["chat"]["messages"][0]["display_body"] == ""
    assert len(result["chat"]["messages"][0]["attachments"]) == 1
    assert (tmp_path / "evidence" / hashlib.sha256(content).hexdigest()).exists()


def test_docx_extraction_keeps_table_values_in_document_order():
    document = Document()
    document.add_paragraph("Order details")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Quantity"
    table.cell(0, 1).text = "Unit price"
    table.cell(1, 0).text = "14,000 kg"
    table.cell(1, 1).text = "USD 17.50/kg"
    document.add_paragraph("Delivery pending")
    content = io.BytesIO()
    document.save(content)

    extracted = _extract_attachment("order.docx", content.getvalue())

    assert extracted.splitlines() == [
        "Order details",
        "Quantity\tUnit price",
        "14,000 kg\tUSD 17.50/kg",
        "Delivery pending",
    ]


def test_conflicting_order_details_ask_one_question_and_save_nothing(tmp_path):
    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="The order lists USD 17 and USD 18 per kg. Which price should I use?",
            needs_input=True,
            save_records=True,
            drafts=[
                Draft(
                    kind="record",
                    name="sales_order",
                    summary="Incomplete order",
                    content='{"order_id":"DOC-3"}',
                )
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})

    response = client.post(
        "/api/messages",
        data={"body": "Add this sales order"},
        files=[("uploads", ("sales_order.txt", b"Order DOC-3", "text/plain"))],
    )

    assert response.status_code == 200
    reply = response.json()["chat"]["messages"][-1]["body"]
    assert reply.startswith("Nothing new was saved.")
    assert "Which price should I use?" in reply
    assert client.get("/api/records").json() == []


def test_saved_orders_are_reported_without_storage_terms(tmp_path):
    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="Both orders are on file.",
            save_records=True,
            drafts=[
                Draft(
                    kind="record",
                    name="sales_order",
                    summary=f"Order {order_id}",
                    content=json.dumps({"order_id": order_id}),
                )
                for order_id in ("SO-201", "SO-202")
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})

    response = client.post(
        "/api/messages",
        data={"body": "Add these sales orders"},
        files=[
            ("uploads", ("sales_order_201.txt", b"Order SO-201", "text/plain")),
            ("uploads", ("sales_order_202.txt", b"Order SO-202", "text/plain")),
        ],
    )

    assert response.status_code == 200
    reply = response.json()["chat"]["messages"][-1]["body"]
    assert "Saved 2 sales orders." in reply
    assert "schema" not in reply.casefold()


def test_mixed_csv_and_documents_save_every_order(tmp_path, monkeypatch):
    monkeypatch.setattr("austra.web.extract_pdf_text", lambda _content: "Order PDF-1")
    document = Document()
    document.add_paragraph("Order DOC-1")
    content = io.BytesIO()
    document.save(content)
    calls = []

    def planner(_files, _access, chat_id):
        calls.append(chat_id)
        return AgentReply(
            reply="Orders processed.",
            save_records=True,
            drafts=[
                Draft(
                    kind="schema",
                    name="document_order",
                    summary="Document orders",
                    content=json.dumps(
                        {"type": "object", "properties": {"order_id": {"type": "string"}}}
                    ),
                ),
                *[
                    Draft(
                        kind="record",
                        name="document_order",
                        summary="Document order",
                        content=json.dumps({"order_id": order_id}),
                    )
                    for order_id in ("DOC-1", "PDF-1")
                ],
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post(
        "/api/messages",
        data={"body": "Add these orders"},
        files=[
            ("uploads", ("one.csv", b"order_id\nCSV-1\n", "text/csv")),
            (
                "uploads",
                (
                    "two.docx",
                    content.getvalue(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            ),
            ("uploads", ("three.pdf", b"pdf placeholder", "application/pdf")),
        ],
    ).json()

    assert len(calls) == 1
    assert "Saved 3 items" in result["chat"]["messages"][-1]["body"]
    assert {record["values"]["order_id"] for record in client.get("/api/records").json()} == {
        "CSV-1",
        "DOC-1",
        "PDF-1",
    }


def test_followup_add_in_mixed_chat_reaches_document_planner(tmp_path):
    calls = []

    def planner(_files, _access, chat_id):
        calls.append(chat_id)
        if len(calls) == 1:
            return AgentReply(reply="Table imported.", save_records=True)
        return AgentReply(
            reply="Document imported.",
            save_records=True,
            drafts=[
                Draft(
                    kind="schema",
                    name="document_order",
                    summary="Document order",
                    content=json.dumps(
                        {"type": "object", "properties": {"order_id": {"type": "string"}}}
                    ),
                ),
                Draft(
                    kind="record",
                    name="document_order",
                    summary="Document order",
                    content='{"order_id":"DOC-1"}',
                ),
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    first = client.post(
        "/api/messages",
        data={"body": "Add these"},
        files=[
            ("uploads", ("one.csv", b"order_id\nCSV-1\n", "text/csv")),
            ("uploads", ("two.txt", b"Order DOC-1", "text/plain")),
        ],
    ).json()
    assert len(client.get("/api/records").json()) == 1

    followup = client.post(
        f"/api/chats/{first['chat']['id']}/messages", json={"body": "add it"}
    ).json()

    assert len(calls) == 2
    assert "Saved 1 item" in followup["chat"]["messages"][-1]["body"]
    assert {record["values"]["order_id"] for record in client.get("/api/records").json()} == {
        "CSV-1",
        "DOC-1",
    }


def test_followup_refreshes_old_docx_text_cache(tmp_path):
    document = Document()
    document.add_paragraph("Order DOC-1")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Unit price"
    table.cell(0, 1).text = "USD 17.50/kg"
    content = io.BytesIO()
    document.save(content)
    extracted_text = []

    def planner(files, _access, chat_id):
        digest = files.chat(chat_id)["messages"][0]["attachments"][0]["id"]
        extracted_text.append(
            (files.root / "evidence_text" / f"{digest}.txt").read_text(encoding="utf-8")
        )
        return AgentReply(reply="Reviewed")

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    first = client.post(
        "/api/messages",
        data={"body": "Review this order"},
        files={
            "uploads": (
                "order.docx",
                content.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    ).json()
    digest = first["chat"]["messages"][0]["attachments"][0]["id"]
    (tmp_path / "evidence_text" / f"{digest}.txt").write_text("Order DOC-1", encoding="utf-8")

    client.post(f"/api/chats/{first['chat']['id']}/messages", json={"body": "add it"})

    assert "USD 17.50/kg" in extracted_text[-1]


def test_invalid_attachment_batch_does_not_create_chat_or_evidence(tmp_path):
    client = TestClient(
        create_app(
            tmp_path,
            planner=lambda _files, _access, _chat: AgentReply(reply="Reviewed"),
        )
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post(
        "/api/messages",
        data={"body": "Review files"},
        files=[
            ("uploads", ("good.csv", b"item\none", "text/csv")),
            ("uploads", ("bad.exe", b"unsupported", "application/octet-stream")),
        ],
    )
    assert response.status_code == 400
    assert client.get("/api/chats").json() == []
    assert not (tmp_path / "evidence").exists()


def test_attachment_stays_downloadable_in_chat_and_audit(tmp_path):
    client = TestClient(
        create_app(tmp_path, planner=lambda _files, _access, _chat: AgentReply(reply="Received"))
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    content = b"item,quantity\nquartz,3"
    result = client.post(
        "/api/messages", files={"uploads": ("stock.csv", content, "text/csv")}
    ).json()
    chat_id = result["chat"]["id"]
    user_message = result["chat"]["messages"][0]
    digest = user_message["attachments"][0]["id"]
    url = f"/api/chats/{chat_id}/attachments/{digest}"

    assert client.get(f"/api/chats/{chat_id}").json()["chat"]["messages"][0]["attachments"]
    assert client.get(url).content == content
    assert client.delete(f"/api/chats/{chat_id}").status_code == 200
    assert client.get(url).status_code == 404
    assert client.get(f"/api/audit/chats/{chat_id}/attachments/{digest}").content == content


def test_background_message_reports_running_time_and_completion(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def planner(_files, _access, _chat):
        started.set()
        assert release.wait(5)
        return AgentReply(reply="Finished processing")

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post("/api/messages/background", data={"body": "Review this"})
    assert response.status_code == 202
    job = response.json()["job"]
    assert started.wait(2)
    assert job["status"] == "running"
    assert client.get("/api/jobs").json()[0]["chat_id"] == response.json()["chat"]["id"]
    assert client.get(f"/api/jobs/{job['id']}").json()["elapsed_seconds"] >= 0
    release.set()
    for _ in range(100):
        result = client.get(f"/api/jobs/{job['id']}").json()
        if result["status"] == "completed":
            break
        time.sleep(0.02)
    assert result["status"] == "completed"
    assert client.get("/api/jobs").json() == []
    assert (
        client.get(f"/api/chats/{job['chat_id']}").json()["chat"]["messages"][-1]["body"]
        == "Finished processing"
    )
