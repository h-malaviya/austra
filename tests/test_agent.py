import json
from hashlib import sha256
from types import SimpleNamespace

from austra import agent
from austra.files import Files


class FakeAccess:
    def get(self, name):
        assert name == "ANTHROPIC_API_KEY"
        return "test-key"

    def configured(self):
        return {}


def test_verified_material_plan_keeps_composite_answer_focused_on_addition(tmp_path, monkeypatch):
    files = Files(tmp_path)
    chat = files.create_chat(
        "analyze raw material needed to fulfill open sales orders and show invoices", "Admin"
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({"reply": "Invoice findings."}))],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    plan = {"orders": [{"id": "SO-1", "production_kg": "8500"}]}
    answer = agent.analyze(files, FakeAccess(), chat["id"], verified_material_plan=plan)

    assert answer.reply == "Invoice findings."
    assert json.loads(calls[0]["messages"][0]["content"])["verified_material_plan"] == plan
    assert "Answer only the additional requested topics" in calls[0]["system"]


def test_research_accepts_list_shaped_tool_results(tmp_path, monkeypatch):
    files = Files(tmp_path)
    chat = files.create_chat("hi", "Admin")
    draft = files.add_drafts(
        chat["id"],
        [
            {
                "kind": "policy",
                "name": "sample_rule",
                "summary": "Sample",
                "content": "# Sample\nUse clear language.",
            }
        ],
    )[0]
    files.review(draft["id"], "approved")
    calls = []

    def create(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if len(calls) == 1:
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id="read-1",
                        name="search_workspace",
                        input={"query": "sample"},
                    )
                ],
            )
        if len(calls) == 2:
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text="Found the approved sample rule.")],
            )
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({"reply": "Hello."}))],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    result = agent.analyze(files, FakeAccess(), chat["id"])

    assert result.reply == "Hello."
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert isinstance(json.loads(tool_result["content"])["data"], list)
    assert not tool_result.get("is_error")


def test_empty_workspace_skips_research_for_csv_request(tmp_path, monkeypatch):
    files = Files(tmp_path)
    chat = files.create_chat("Add stock from the attached CSV", "Admin")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "reply": "I can propose a stock schema for review.",
                            "drafts": [],
                            "credential_requests": [],
                        }
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    result = agent.analyze(files, FakeAccess(), chat["id"])

    assert result.reply == "I can propose a stock schema for review."
    assert len(calls) == 1
    assert "output_config" in calls[0]
    assert "Add stock from the attached CSV" in calls[0]["messages"][0]["content"]


def test_invalid_structured_answer_is_retried_with_validation_feedback(tmp_path, monkeypatch):
    files = Files(tmp_path)
    chat = files.create_chat("Update the purchase order", "Admin")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        reply = "" if len(calls) == 1 else "Updated the purchase order."
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({"reply": reply}))],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    result = agent.analyze(files, FakeAccess(), chat["id"])

    assert result.reply == "Updated the purchase order."
    assert len(calls) == 2
    assert "reply" in calls[1]["messages"][-1]["content"]


def test_research_token_limit_falls_back_to_structured_answer(tmp_path, monkeypatch):
    files = Files(tmp_path)
    chat = files.create_chat("Add stock", "Admin")
    draft = files.add_drafts(
        chat["id"],
        [
            {
                "kind": "schema",
                "name": "stock",
                "summary": "Stock fields",
                "content": '{"type":"object"}',
            }
        ],
    )[0]
    files.review(draft["id"], "approved")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(stop_reason="max_tokens", content=[])
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {"reply": "Need more evidence", "drafts": [], "credential_requests": []}
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    result = agent.analyze(files, FakeAccess(), chat["id"])

    assert result.reply == "Need more evidence"
    assert calls[0]["max_tokens"] == agent.RESEARCH_RESPONSE_TOKENS
    assert calls[0]["tool_choice"]["type"] == "any"
    assert len(calls) == 2


def test_fully_visible_attachment_skips_research(tmp_path, monkeypatch):
    files = Files(tmp_path)
    content = b"Supplier: Example, source: synthetic demo"
    digest = sha256(content).hexdigest()
    chat, _, _ = files.submit_message(
        None,
        "Add supplier list",
        [
            {
                "id": digest,
                "name": "suppliers.txt",
                "extension": ".txt",
                "content": content,
                "text": content.decode(),
                "excerpt": content.decode(),
            }
        ],
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "reply": "Saved",
                            "drafts": [
                                {
                                    "kind": "record",
                                    "name": "supplier",
                                    "summary": "Supplier",
                                    "content": '{"name":"Example"}',
                                }
                            ],
                            "credential_requests": [],
                        }
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    agent.analyze(files, FakeAccess(), chat["id"])
    assert len(calls) == 1
    assert "output_config" in calls[0]


def test_document_import_uses_extracted_text_and_retries_empty_drafts(tmp_path, monkeypatch):
    files = Files(tmp_path)
    content = b"Order DOC-1, quantity 14,000 kg, unit price USD 17.50/kg"
    digest = sha256(content).hexdigest()
    chat, _, _ = files.submit_message(
        None,
        "Add this order",
        [
            {
                "id": digest,
                "name": "order.docx",
                "extension": ".docx",
                "content": content,
                "text": content.decode(),
                "excerpt": content.decode(),
            }
        ],
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        answer = (
            {"reply": "I will read it later", "drafts": []}
            if len(calls) == 1
            else {
                "reply": "Ready to save",
                "save_records": True,
                "drafts": [
                    {
                        "kind": "record",
                        "name": "sales_order",
                        "summary": "Order DOC-1",
                        "content": '{"order_id":"DOC-1"}',
                    }
                ],
            }
        )
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(answer))],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    result = agent.analyze(files, FakeAccess(), chat["id"])

    assert len(calls) == 2
    assert "USD 17.50/kg" in calls[0]["messages"][0]["content"]
    assert "Complete the document import now" in calls[1]["messages"][-1]["content"]
    assert len(result.drafts) == 1


def test_document_import_keeps_specific_question_when_source_details_conflict(
    tmp_path, monkeypatch
):
    files = Files(tmp_path)
    content = b"Order DOC-3, two different prices: USD 17 and USD 18 per kg"
    digest = sha256(content).hexdigest()
    chat, _, _ = files.submit_message(
        None,
        "Add this order",
        [
            {
                "id": digest,
                "name": "sales_order.txt",
                "extension": ".txt",
                "content": content,
                "text": content.decode(),
                "excerpt": content.decode(),
            }
        ],
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "reply": "The order shows both USD 17 and USD 18 per kg. Which price should I use?",
                            "needs_input": True,
                            "save_records": True,
                        }
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )

    result = agent.analyze(files, FakeAccess(), chat["id"])

    assert result.needs_input is True
    assert "Which price should I use?" in result.reply
    assert len(calls) == 1


def test_followup_import_includes_prior_document_evidence(tmp_path, monkeypatch):
    files = Files(tmp_path)
    content = b"Order DOC-2, quantity 10,000 kg"
    digest = sha256(content).hexdigest()
    chat, _, _ = files.submit_message(
        None,
        "Review this order",
        [
            {
                "id": digest,
                "name": "order.docx",
                "extension": ".docx",
                "content": content,
                "text": content.decode(),
                "excerpt": content.decode(),
            }
        ],
    )
    files.add_message(chat["id"], "assistant", "Reviewed.", "Austra")
    files.add_message(chat["id"], "user", "add it", "Admin")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if "tools" in kwargs:
            return SimpleNamespace(
                stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Ready")]
            )
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "reply": "Ready to save",
                            "drafts": [
                                {
                                    "kind": "record",
                                    "name": "sales_order",
                                    "summary": "Order DOC-2",
                                    "content": '{"order_id":"DOC-2"}',
                                }
                            ],
                        }
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    result = agent.analyze(files, FakeAccess(), chat["id"])

    context = json.loads(calls[0]["messages"][0]["content"])
    assert context["document_evidence"][0]["text"] == content.decode()
    assert len(result.drafts) == 1


def test_research_worker_is_read_only_and_returns_findings(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Review inventory and supplier evidence", "Admin")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id="tool-1",
                        name="search_workspace",
                        input={"query": "inventory"},
                    )
                ],
            )
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="No inventory policy was found.")],
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    result = agent._delegate_research(client, files, chat["id"], "Find inventory policy")

    assert result == "No inventory policy was found."
    assert "delegate_research" not in [tool["name"] for tool in calls[0]["tools"]]
    assert calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_stock_order_research_passes_complete_lots_to_final_answer(tmp_path, monkeypatch):
    files = Files(tmp_path)
    chat = files.create_chat("Can current stock match all open sales orders?", "Admin")
    folder = tmp_path / "records" / "finished_goods_stock_lot"
    folder.mkdir(parents=True)
    for index in range(11):
        record = {
            "id": f"{index:032x}",
            "schema": "finished_goods_stock_lot",
            "approved_at": f"2026-10-23T00:00:{index:02d}+00:00",
            "values": {"lot_id": f"LOT-{index:02d}", "available_kg": 100},
        }
        (folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")
    requests = tmp_path / "records" / "sales_order_request"
    requests.mkdir(parents=True)
    for index in range(2):
        record = {
            "id": f"{index + 11:032x}",
            "schema": "sales_order_request",
            "approved_at": f"2026-10-24T00:00:{index:02d}+00:00",
            "values": {"order_id": f"SO-20{index + 1}", "ordered_kg": 500},
        }
        (requests / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")
    orders = tmp_path / "records" / "sales_order"
    orders.mkdir(parents=True)
    order = {
        "id": f"{13:032x}",
        "schema": "sales_order",
        "approved_at": "2026-10-25T00:00:00+00:00",
        "values": {"order_id": "SO-203", "ordered_kg": 600},
    }
    (orders / f"{order['id']}.json").write_text(json.dumps(order), encoding="utf-8")
    calls = []
    schemas = ["finished_goods_stock_lot", "sales_order_request", "sales_order"]

    def create(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if len(calls) <= len(schemas):
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id=f"tool-{len(calls)}",
                        name="list_records",
                        input={"schema": schemas[len(calls) - 1], "limit": 8},
                    )
                ],
            )
        if "tools" in kwargs:
            return SimpleNamespace(stop_reason="end_turn", content=[])
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "reply": (
                                "| Order | Requested | Available | Shortfall | Purchase needed |\n"
                                "| --- | ---: | ---: | ---: | ---: |\n"
                                "| SO-201 | 500 kg | Unknown | Unknown | Unknown |"
                            )
                        }
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    answer = agent.analyze(files, FakeAccess(), chat["id"])

    pages = [
        json.loads(calls[index + 1]["messages"][-1]["content"][0]["content"])["data"]
        for index in range(len(schemas))
    ]
    page = pages[0]
    assert "SO-201 | 500 kg" in answer.reply
    assert page["complete"] is True
    assert page["total"] == len(page["records"]) == 11
    assert {record["values"]["lot_id"] for record in page["records"]} == {
        f"LOT-{index:02d}" for index in range(11)
    }
    assert all(page["complete"] for page in pages)
    assert [page["total"] for page in pages] == [11, 2, 1]
    assert {record["values"]["order_id"] for page in pages[1:] for record in page["records"]} == {
        "SO-201",
        "SO-202",
        "SO-203",
    }


def test_repeated_read_is_replanned_and_recovery_is_used_next_time(tmp_path, monkeypatch):
    files = Files(tmp_path)
    chat = files.create_chat("Compare stock and sales orders", "Admin")
    for index, schema in enumerate(("stock_lot", "sales_order")):
        folder = tmp_path / "records" / schema
        folder.mkdir(parents=True)
        record = {
            "id": f"{index:032x}",
            "schema": schema,
            "approved_at": f"2026-10-23T00:00:{index:02d}+00:00",
            "values": {"reference": f"REF-{index}"},
        }
        (folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")
    calls = []

    def create(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if len(calls) <= 3:
            name = "list_records" if len(calls) <= 2 else "compose_record_lookup"
            arguments = (
                {"schema": "stock_lot"}
                if len(calls) <= 2
                else {
                    "schemas": ["stock_lot", "sales_order"],
                    "save_as": "stock_order_match",
                }
            )
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(
                        type="tool_use", id=f"tool-{len(calls)}", name=name, input=arguments
                    )
                ],
            )
        if "tools" in kwargs:
            return SimpleNamespace(stop_reason="end_turn", content=[])
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "reply": (
                                "| Order | Requested | Available | Shortfall | Purchase needed |\n"
                                "| --- | ---: | ---: | ---: | ---: |\n"
                                "| SO-1 | 10 kg | Unknown | Unknown | Unknown |"
                            )
                        }
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    answer = agent.analyze(files, FakeAccess(), chat["id"])
    repeated = calls[2]["messages"][-1]["content"][0]
    composed = json.loads(calls[3]["messages"][-1]["content"][0]["content"])["data"]

    assert "SO-1 | 10 kg" in answer.reply
    assert repeated["is_error"] is True
    assert calls[1]["tool_choice"]["type"] == "auto"
    assert calls[2]["tool_choice"]["type"] == "any"
    assert composed["complete"] is True
    assert composed["saved_as"] == "stock_order_match"
    assert files.recovery_lessons() == [
        {
            "tool": "list_records",
            "failure": "repeated_read",
            "resolution": "composed_record_lookup",
        }
    ]

    later = files.create_chat("Review a new order", "Admin")
    calls.clear()
    agent.analyze(files, FakeAccess(), later["id"])
    context = json.loads(calls[0]["messages"][0]["content"])
    assert context["recovery_lessons"][0]["resolution"] == "composed_record_lookup"
    assert context["saved_read_plans"][0]["name"] == "stock_order_match"


def test_approved_lookup_skill_and_report_gate_replace_placeholder(tmp_path, monkeypatch):
    files = Files(tmp_path)
    chat = files.create_chat("Analyze current stock against open sales orders", "Admin")
    skill = files.add_drafts(
        chat["id"],
        [
            {
                "kind": "skill",
                "name": "stock_vs_open_orders_lookup",
                "summary": "Compare complete stock and orders",
                "content": "# Stock versus orders\nRead every lot and open order before reporting.",
            }
        ],
    )[0]
    files.review(skill["id"], "approved")
    for index, schema in enumerate(("finished_goods_stock_lot", "sales_order_request")):
        folder = tmp_path / "records" / schema
        folder.mkdir(parents=True)
        record = {
            "id": f"{index:032x}",
            "schema": schema,
            "approved_at": f"2026-10-23T00:00:{index:02d}+00:00",
            "values": {"reference": f"REF-{index}", "available_kg": 8, "ordered_kg": 10},
        }
        (folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")
    calls = []
    report = (
        "| Order | Requested | Available | Shortfall | Purchase needed |\n"
        "| --- | ---: | ---: | ---: | ---: |\n"
        "| SO-1 | 10 kg | 8 kg | 2 kg | 2 kg |"
    )

    def create(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if "tools" in kwargs:
            return SimpleNamespace(stop_reason="end_turn", content=[])
        reply = "placeholder" if len(calls) == 2 else report
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({"reply": reply}))],
        )

    monkeypatch.setattr(
        agent,
        "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    answer = agent.analyze(files, FakeAccess(), chat["id"])

    assert answer.reply == report
    assert "skill/stock_vs_open_orders_lookup" in calls[0]["system"]
    evidence = calls[1]["messages"][-1]["content"]
    assert "finished_goods_stock_lot" in evidence
    assert "sales_order_request" in evidence
    assert "placeholder" in calls[2]["messages"][-1]["content"]
    assert files.recovery_lessons()[0] == {
        "tool": "stock_order_report",
        "failure": "placeholder_reply",
        "resolution": "revised_report_with_table",
    }


def test_large_message_uses_model_token_window_without_30000_char_clip(tmp_path, monkeypatch):
    files = Files(tmp_path)
    body = "Analyze " + "x" * 110000
    chat = files.create_chat(body, "Admin")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({"reply": "Reviewed"}))],
        )

    client = SimpleNamespace(
        models=SimpleNamespace(retrieve=lambda _name: SimpleNamespace(max_input_tokens=200000)),
        messages=SimpleNamespace(
            create=create,
            count_tokens=lambda **_kwargs: SimpleNamespace(input_tokens=30000),
        ),
    )
    monkeypatch.setattr(agent, "Anthropic", lambda **_kwargs: client)
    result = agent.analyze(files, FakeAccess(), chat["id"])

    assert result.reply == "Reviewed"
    assert body in calls[0]["messages"][0]["content"]
