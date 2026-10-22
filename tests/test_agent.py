import json
from copy import deepcopy
from types import SimpleNamespace

from austra.agent import analyze
from austra.documents import DocumentLibrary
from austra.plan_schema import PLAN_SCHEMA
from austra.plan_types import CasePlan
from austra.store import Store
from austra.tool_registry import ToolRegistry


def test_structured_output_schema_matches_typed_plan():
    assert set(PLAN_SCHEMA["properties"]) == set(CasePlan.model_fields)
    assert set(PLAN_SCHEMA["required"]) == set(CasePlan.model_fields)


def test_agent_uses_allowlisted_tool_before_structured_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    store = Store(tmp_path / "business.db")
    case = store.create_case("Find policy", "ui", "Owner")
    registry = ToolRegistry(store, DocumentLibrary(store, tmp_path / "documents"))
    calls = []

    class FakeMessages:
        def create(self, **kwargs):
            calls.append(deepcopy(kwargs))
            if len(calls) == 1:
                return SimpleNamespace(
                    stop_reason="tool_use",
                    content=[
                        SimpleNamespace(
                            type="tool_use", id="call-1", name="list_active_policies", input={}
                        )
                    ],
                )
            if len(calls) == 2:
                return SimpleNamespace(
                    stop_reason="end_turn",
                    content=[SimpleNamespace(type="text", text="Research complete")],
                )
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(
                            {
                                "summary": "Manager policy review",
                                "category": "purchasing",
                                "missing_information": [],
                                "actions": [],
                                "record_types": [],
                                "skill_drafts": [],
                                "memory_drafts": [],
                            }
                        ),
                    )
                ],
            )

    monkeypatch.setattr(
        "austra.agent.Anthropic", lambda **_kwargs: SimpleNamespace(messages=FakeMessages())
    )
    result = analyze("Find policy", [], [], registry, case["id"])

    assert result.summary == "Manager policy review"
    assert calls[0]["tools"][0]["strict"] is True
    assert calls[1]["messages"][-1]["content"][0]["tool_use_id"] == "call-1"
    assert "output_config" in calls[2]
    assert store.get_case(case["id"])["events"][-1]["kind"] == "tool_invoked"
