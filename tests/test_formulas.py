import json

import pytest

from austra.files import Files
from austra.tools import ReadTools


def _draft(files, chat_id, kind, name, content):
    return files.add_drafts(
        chat_id,
        [{"kind": kind, "name": name, "summary": name, "content": json.dumps(content)}],
    )[0]


def _record(root, schema, record_id, values):
    path = root / "records" / schema / f"{record_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": record_id,
                "schema": schema,
                "values": values,
                "approved_at": "2026-11-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _setup(tmp_path):
    files = Files(tmp_path)
    chat_id = files.create_chat("Calculate the net amount", "Admin")["id"]
    schema = {
        "type": "object",
        "properties": {
            "invoice_id": {"type": "string"},
            "gross": {"type": "number"},
            "discount": {"type": "number"},
            "rate": {"type": "number"},
        },
    }
    files.review(_draft(files, chat_id, "schema", "invoice", schema)["id"], "approved")
    definition = {
        "expression": "(gross - discount) * rate",
        "inputs": {
            name: {
                "schema": "invoice",
                "match_field": "invoice_id",
                "value_field": name,
            }
            for name in ("gross", "discount", "rate")
        },
    }
    return files, chat_id, definition


def test_approved_formula_maps_records_and_runs_read_only_scenario(tmp_path):
    files, chat_id, definition = _setup(tmp_path)
    _record(
        tmp_path,
        "invoice",
        "live",
        {"invoice_id": "I-1", "gross": "120.50", "discount": "20.50", "rate": "1.05"},
    )
    _record(
        tmp_path,
        "invoice",
        "demo",
        {
            "invoice_id": "I-1",
            "gross": "999",
            "discount": "0",
            "rate": "1",
            "data_origin": "synthetic_demo",
        },
    )
    tools = ReadTools(files, chat_id)
    with pytest.raises(ValueError, match="Approved formula not found"):
        tools.invoke("run_approved_formula", {"name": "net_total", "identity": "I-1"})
    draft = _draft(files, chat_id, "formula", "net_total", definition)
    assert files.approved()[0]["kind"] == "schema"
    files.review(draft["id"], "approved")
    baseline = tools.invoke("run_approved_formula", {"name": "net_total", "identity": "I-1"})[
        "data"
    ]
    assert baseline["value"] == "105.0000"
    assert baseline["version"] == 1
    assert baseline["sources"]["gross"] == {
        "record_id": "live",
        "schema": "invoice",
        "field": "gross",
        "value": "120.50",
    }
    scenario = tools.invoke(
        "run_approved_formula",
        {"name": "net_total", "identity": "I-1", "overrides": {"rate": "1.10"}},
    )["data"]
    assert scenario["value"] == "110.0000"
    assert scenario["overrides"] == {"rate": "1.10"}
    assert (
        next(record for record in files.records() if record["id"] == "live")["values"]["rate"]
        == "1.05"
    )
    assert not (tmp_path / "records" / "invoice" / "scenario.json").exists()


def test_formula_rejects_conflicts_missing_sources_and_invalid_scenarios(tmp_path):
    files, chat_id, definition = _setup(tmp_path)
    files.review(_draft(files, chat_id, "formula", "net_total", definition)["id"], "approved")
    _record(
        tmp_path,
        "invoice",
        "first",
        {"invoice_id": "I-1", "gross": "20", "discount": "2", "rate": "1.1"},
    )
    tools = ReadTools(files, chat_id)
    assert (
        tools.invoke("run_approved_formula", {"name": "net_total", "identity": "I-1"})["data"][
            "value"
        ]
        == "19.8"
    )
    _record(
        tmp_path,
        "invoice",
        "second",
        {"invoice_id": "I-1", "gross": "50", "discount": "0", "rate": "1"},
    )
    with pytest.raises(ValueError, match="Conflicting active sources"):
        tools.invoke(
            "run_approved_formula",
            {"name": "net_total", "identity": "I-1", "overrides": {"gross": "5"}},
        )
    (tmp_path / "records" / "invoice" / "second.json").unlink()
    with pytest.raises(ValueError, match="unmapped formula input"):
        tools.invoke(
            "run_approved_formula",
            {"name": "net_total", "identity": "I-1", "overrides": {"other": "5"}},
        )
    with pytest.raises(ValueError, match="finite"):
        tools.invoke(
            "run_approved_formula",
            {"name": "net_total", "identity": "I-1", "overrides": {"gross": "NaN"}},
        )
    with pytest.raises(ValueError, match="Missing source"):
        tools.invoke("run_approved_formula", {"name": "net_total", "identity": "I-2"})


def test_formula_approval_requires_reviewed_mapping_and_safe_expression(tmp_path):
    files, chat_id, definition = _setup(tmp_path)
    definition["inputs"]["gross"]["value_field"] = "unreviewed_field"
    draft = _draft(files, chat_id, "formula", "net_total", definition)
    with pytest.raises(ValueError, match="missing from approved schema"):
        files.review(draft["id"], "approved")
    assert files.drafts()[1]["status"] == "proposed"
    assert not (tmp_path / "approved" / "formula").exists()
    definition["expression"] = "__import__('os').system('exit')"
    with pytest.raises(ValueError, match="Only decimal arithmetic"):
        _draft(files, chat_id, "formula", "unsafe", definition)


def test_new_formula_version_can_map_another_approved_schema(tmp_path):
    files, chat_id, definition = _setup(tmp_path)
    _record(
        tmp_path,
        "invoice",
        "invoice1",
        {"invoice_id": "I-1", "gross": "100", "discount": "10", "rate": "1"},
    )
    files.review(_draft(files, chat_id, "formula", "net_total", definition)["id"], "approved")
    tools = ReadTools(files, chat_id)
    assert (
        tools.invoke("run_approved_formula", {"name": "net_total", "identity": "I-1"})["data"][
            "value"
        ]
        == "90"
    )
    rate_schema = {
        "type": "object",
        "properties": {"invoice_id": {"type": "string"}, "rate": {"type": "number"}},
    }
    files.review(_draft(files, chat_id, "schema", "rate_card", rate_schema)["id"], "approved")
    _record(tmp_path, "rate_card", "rate1", {"invoice_id": "I-1", "rate": "1.2"})
    definition["inputs"]["rate"]["schema"] = "rate_card"
    files.review(_draft(files, chat_id, "formula", "net_total", definition)["id"], "approved")
    result = tools.invoke("run_approved_formula", {"name": "net_total", "identity": "I-1"})["data"]
    assert result["value"] == "108.0"
    assert result["version"] == 2
    assert result["sources"]["rate"]["record_id"] == "rate1"


def test_formula_approval_validates_structure_without_evaluating_placeholders(tmp_path):
    files, chat_id, definition = _setup(tmp_path)
    definition["expression"] = "gross / (rate - 1)"
    files.review(_draft(files, chat_id, "formula", "ratio", definition)["id"], "approved")
    _record(
        tmp_path,
        "invoice",
        "invoice1",
        {"invoice_id": "I-1", "gross": "10", "discount": "0", "rate": "1.5"},
    )
    result = ReadTools(files, chat_id).invoke(
        "run_approved_formula", {"name": "ratio", "identity": "I-1"}
    )
    assert result["data"]["value"] == "20"
