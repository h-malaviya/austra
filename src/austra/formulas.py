"""Reviewed decimal formulas over explicitly mapped business records."""

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from austra.arithmetic import calculate_expression, validate_expression

FIELD_NAME = r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"
SCHEMA_NAME = r"^[a-z][a-z0-9_]{1,63}$"


class FormulaSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: str = Field(alias="schema", pattern=SCHEMA_NAME)
    match_field: str = Field(pattern=FIELD_NAME)
    value_field: str = Field(pattern=FIELD_NAME)


class FormulaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1, max_length=500)
    inputs: dict[str, FormulaSource] = Field(min_length=1, max_length=20)


def parse_formula(content: str) -> FormulaDefinition:
    try:
        definition = FormulaDefinition.model_validate_json(content)
    except ValueError as error:
        raise ValueError("Invalid formula definition") from error
    if any(not re.fullmatch(FIELD_NAME, name) for name in definition.inputs):
        raise ValueError("Invalid formula input name")
    validate_expression(definition.expression, set(definition.inputs))
    return definition


def validate_formula_mapping(content: str, approved: list[dict[str, Any]]) -> None:
    definition = parse_formula(content)
    schemas = {item["name"]: item for item in approved if item["kind"] == "schema"}
    for source in definition.inputs.values():
        schema = schemas.get(source.schema_name)
        if schema is None:
            raise ValueError(f"Approved schema {source.schema_name} is required for formula")
        properties = json.loads(schema["content"]).get("properties", {})
        if source.match_field not in properties or source.value_field not in properties:
            raise ValueError(f"Formula field is missing from approved schema {source.schema_name}")


def run_formula(
    approved: list[dict[str, Any]],
    records: list[dict[str, Any]],
    name: str,
    identity: str,
    overrides: dict[str, str],
) -> dict[str, Any]:
    formula = next(
        (item for item in approved if item["kind"] == "formula" and item["name"] == name),
        None,
    )
    if formula is None:
        raise ValueError("Approved formula not found")
    definition = parse_formula(formula["content"])
    validate_formula_mapping(formula["content"], approved)
    if set(overrides) - set(definition.inputs):
        raise ValueError("Scenario names an unmapped formula input")
    values: dict[str, str] = {}
    sources: dict[str, dict[str, str]] = {}
    for variable, mapping in definition.inputs.items():
        matches = [
            record
            for record in records
            if record["schema"] == mapping.schema_name
            and str(record["values"].get(mapping.match_field, "")) == identity
            and record["values"].get("data_origin") != "synthetic_demo"
        ]
        if not matches:
            raise ValueError(f"Missing source for {variable} and identity {identity}")
        if len(matches) != 1:
            raise ValueError(f"Conflicting active sources for {variable} and identity {identity}")
        record = matches[0]
        raw_value = record["values"].get(mapping.value_field)
        if raw_value is None or isinstance(raw_value, bool):
            raise ValueError(f"Missing numeric value for {variable}")
        source_value = str(raw_value)
        calculate_expression(variable, {variable: source_value})
        values[variable] = source_value
        sources[variable] = {
            "record_id": record["id"],
            "schema": mapping.schema_name,
            "field": mapping.value_field,
            "value": source_value,
        }
    scenario = {**values, **overrides}
    result = calculate_expression(definition.expression, scenario)
    return {
        "formula": name,
        "version": formula["version"],
        "expression": definition.expression,
        "identity": identity,
        "value": result,
        "inputs": scenario,
        "sources": sources,
        "overrides": overrides,
    }
