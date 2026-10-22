import pytest

from austra import skills
from austra.initial_policy import BODY


def test_approved_case_skill_and_policy_load_from_markdown():
    skill = skills.load_builtin_skill("case_planning")
    workflow_skill = skills.load_builtin_skill("workflow_authoring")

    assert skill.identifier == "case_planning"
    assert skill.version == 7
    assert "deterministic backend" in skill.body
    assert "state machine" in workflow_skill.body
    assert "3 metric tonnes" in BODY


def test_skill_loader_rejects_unknown_or_unapproved_files(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Unknown"):
        skills.load_builtin_skill("../secrets")

    monkeypatch.setattr(skills, "SKILL_ROOT", tmp_path)
    candidate = tmp_path / "case_planning"
    candidate.mkdir()
    (candidate / "SKILL.md").write_text(
        "---\nid: case_planning\nversion: 2\nstatus: draft\n---\n# Replacement",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not approved"):
        skills.load_builtin_skill("case_planning")
