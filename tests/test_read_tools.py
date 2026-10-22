from austra.files import Files
from austra.tools import ReadTools


def test_read_tools_search_approved_files_and_audit_use(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("What is our quartz quality rule?", "Admin")
    draft = files.add_drafts(
        chat["id"],
        [
            {
                "kind": "policy",
                "name": "quartz_quality",
                "summary": "Quality guidance",
                "content": "Only inspect the quartz grade after approval.",
            }
        ],
    )[0]
    files.review(draft["id"], "approved")

    tools = ReadTools(files, chat["id"])
    found = tools.invoke("search_workspace", {"query": "quartz"})
    assert any(item["name"] == "quartz_quality" for item in found["data"])
    read = tools.invoke("read_artifact", {"kind": "policy", "name": "quartz_quality"})
    assert "inspect" in read["data"]["content"]
    assert len(list((tmp_path / "audit").glob("*.json"))) == 2
