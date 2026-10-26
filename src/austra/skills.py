"""Load platform skills from Markdown without seeding business policies."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from austra.files import Files

SKILL_ROOT = Path(__file__).parent / "platform_skills"
PLATFORM_SKILLS = ("foundation", "skill_authoring", "structure_authoring", "credential_setup")


def load_skills(files: "Files | None" = None) -> str:
    platform = "\n\n".join(
        (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8") for name in PLATFORM_SKILLS
    )
    if files is None:
        return platform
    rules = [item for item in files.approved() if item["kind"] in {"policy", "workflow"}]
    if not rules:
        return platform
    return (
        platform
        + "\n\n# Active administrator-approved business rules and workflows\n\n"
        + "\n\n".join(
            f"## {item['kind']}/{item['name']} (v{item['version']})\n{item['content']}"
            for item in rules
        )
    )
