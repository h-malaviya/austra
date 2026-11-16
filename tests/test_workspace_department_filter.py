import json

from austra.a2ui import workspace_page
from austra.files import Files


def test_workspace_page_filters_records_before_pagination(tmp_path):
    files = Files(tmp_path)
    folder = tmp_path / "records" / "inventory_lot"
    folder.mkdir(parents=True)
    for record_id in ("first", "second"):
        (folder / f"{record_id}.json").write_text(
            json.dumps(
                {
                    "id": record_id,
                    "schema": "inventory_lot",
                    "values": {"lot": record_id},
                    "approved_at": "2026-11-16T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

    page = workspace_page(files, "records", allowed_ids={"second"})

    assert page["total"] == 1
    assert [item["id"] for item in page["items"]] == ["second"]
