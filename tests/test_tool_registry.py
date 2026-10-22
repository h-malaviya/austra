import pytest
from pydantic import ValidationError

from austra.documents import DocumentLibrary
from austra.store import Store
from austra.tool_registry import ToolRegistry


def test_allowlisted_tool_uses_current_case_and_records_audit(tmp_path):
    store = Store(tmp_path / "business.db")
    documents = DocumentLibrary(store, tmp_path / "documents")
    registry = ToolRegistry(store, documents)
    case = store.create_case("Need quartz", "ui", "Owner")

    result = registry.invoke("get_current_case", {}, case["id"])

    assert result["data"]["id"] == case["id"]
    assert store.get_case(case["id"])["events"][-1]["kind"] == "tool_invoked"
    assert all(item["strict"] for item in registry.definitions())


def test_tool_rejects_unregistered_names_and_extra_arguments(tmp_path):
    store = Store(tmp_path / "business.db")
    registry = ToolRegistry(store, DocumentLibrary(store, tmp_path / "documents"))
    case = store.create_case("Need quartz", "ui", "Owner")

    with pytest.raises(ValueError, match="not registered"):
        registry.invoke("send_email", {}, case["id"])
    with pytest.raises(ValidationError):
        registry.invoke("get_current_case", {"other_case_id": "someone_else"}, case["id"])
