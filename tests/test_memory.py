"""Memory extraction separates routine notes from completion and compaction."""

import json
from types import SimpleNamespace

from austra import memory


def test_memory_extractor_keeps_routine_turn_unsummarized(monkeypatch):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({
                "notes": [{
                    "scope": "user",
                    "text": "Prefers concise reports",
                    "source_quote": "I prefer concise reports",
                }],
                "requirements_complete": False,
                "final_summary": "",
                "working_compaction": "",
            }))],
        )

    monkeypatch.setattr(
        memory, "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    update = memory.extract_memory_update(
        "fixture", "I prefer concise reports",
        [{"role": "user", "body": "I prefer concise reports"}], [], [],
    )
    assert update.notes[0].scope == "user"
    assert not update.requirements_complete
    assert update.final_summary == ""
    assert json.loads(captured["messages"][0]["content"])["compaction_needed"] is False
    assert "Do not summarize a routine turn" in captured["system"]


def test_memory_extractor_requests_compaction_only_near_context_limit(monkeypatch):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({
                "notes": [], "requirements_complete": False, "final_summary": "",
                "working_compaction": "The earlier requirements remain open.",
            }))],
        )

    monkeypatch.setattr(
        memory, "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    update = memory.extract_memory_update(
        "fixture", "What is next?", [], ["Prior fact"], ["Who approves?"],
        prior_compaction="Initial requirement", compaction_needed=True,
    )
    payload = json.loads(captured["messages"][0]["content"])
    assert payload["compaction_needed"] is True
    assert payload["prior_compaction"] == "Initial requirement"
    assert update.working_compaction == "The earlier requirements remain open."
