"""Bounded Sonnet analysis that proposes, but never executes, actions."""

import json
import os

from anthropic import Anthropic

from austra.plan_schema import PLAN_SCHEMA
from austra.plan_types import CasePlan
from austra.skills import load_builtin_skill
from austra.tool_registry import ToolRegistry


class AnalysisUnavailable(RuntimeError):
    """The model cannot be called with the current configuration."""


def analyze(
    request_text: str,
    policies: list[dict[str, object]],
    references: list[dict[str, object]],
    registry: ToolRegistry | None = None,
    case_id: str | None = None,
) -> CasePlan:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise AnalysisUnavailable("ANTHROPIC_API_KEY is not configured")

    policy_context = [
        {
            "id": policy["id"],
            "title": policy["title"],
            "version": policy["version"],
            "body": policy["body"],
        }
        for policy in policies
    ]
    client = Anthropic(api_key=api_key, timeout=30.0, max_retries=1)
    system = load_builtin_skill("case_planning").body
    messages = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "approved_policies": policy_context,
                    "reference_documents": references,
                    "request": request_text,
                }
            ),
        }
    ]
    if registry is not None:
        if case_id is None:
            raise ValueError("Case ID is required for tool use")
        for _ in range(3):
            research = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=1400,
                system=system,
                messages=messages,
                tools=registry.definitions(),
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            )
            messages.append({"role": "assistant", "content": research.content})
            if research.stop_reason != "tool_use":
                if research.stop_reason != "end_turn":
                    raise AnalysisUnavailable("Agent research did not complete")
                messages.append({"role": "user", "content": "Return the final structured plan."})
                break
            calls = [block for block in research.content if block.type == "tool_use"]
            if not calls:
                raise AnalysisUnavailable("Agent requested a tool without a call")
            results = []
            for call in calls:
                try:
                    result = registry.invoke(call.name, call.input, case_id)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": json.dumps(result),
                        }
                    )
                except (KeyError, ValueError) as error:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": str(error),
                            "is_error": True,
                        }
                    )
            messages.append({"role": "user", "content": results})

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4000,
        system=system,
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
    )
    if response.stop_reason != "end_turn":
        raise AnalysisUnavailable("The model did not return a completed plan")
    text_blocks = [block.text for block in response.content if block.type == "text"]
    if len(text_blocks) != 1:
        raise AnalysisUnavailable("The model returned an unexpected response")
    return CasePlan.model_validate_json(text_blocks[0])
