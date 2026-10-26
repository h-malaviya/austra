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
                    text=json.dumps({"reply": "Saved", "drafts": [], "credential_requests": []}),
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
