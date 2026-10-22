"""Deterministic case transitions loaded from a versioned workflow file."""

import json
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

WORKFLOW_PATH = Path(__file__).parent / "knowledge" / "workflows" / "case_review" / "WORKFLOW.md"


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["message_added", "plan_proposed"]
    from_states: list[Literal["new", "review"]] = Field(min_length=1)
    to_state: Literal["new", "review"]


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Literal["case_review"]
    version: int = Field(ge=1)
    initial_state: Literal["new"]
    transitions: list[Transition] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_transitions(self) -> "Workflow":
        pairs = [(entry.event, state) for entry in self.transitions for state in entry.from_states]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Workflow has overlapping transitions")
        return self

    def target(self, state: str, event: str) -> str:
        for entry in self.transitions:
            if entry.event == event and state in entry.from_states:
                return entry.to_state
        raise ValueError(f"Event {event} is not allowed from {state}")


def load_workflow() -> Workflow:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    marker = "```json\n"
    if source.count(marker) != 1:
        raise ValueError("Workflow must have one JSON definition")
    definition = source.split(marker, maxsplit=1)[1].split("\n```", maxsplit=1)[0]
    return Workflow.model_validate(json.loads(definition))


def transition_case(
    connection: sqlite3.Connection, case_id: str, event: str, workflow: Workflow
) -> str:
    row = connection.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        raise KeyError(case_id)
    target = workflow.target(row["status"], event)
    connection.execute("UPDATE cases SET status = ? WHERE id = ?", (target, case_id))
    return target
