"""Load platform skills from Markdown without seeding business policies."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from austra.files import Files

SKILL_ROOT = Path(__file__).parent / "platform_skills"
PLATFORM_SKILLS = (
    "foundation",
    "skill_authoring",
    "structure_authoring",
    "credential_setup",
    "analysis_reporting",
)


def load_skills(files: "Files | None" = None) -> str:
    platform = "\n\n".join(
        (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8") for name in PLATFORM_SKILLS
    )
    if files is None:
        return platform
    approved = files.approved()
    rules = [item for item in approved if item["kind"] in {"policy", "workflow"}]
    skills = [item for item in approved if item["kind"] == "skill"]
    if not rules and not skills:
        return platform
    sections = [platform]
    if rules:
        sections.append(
            "# Active administrator-approved business rules and workflows\n\n"
            + "\n\n".join(
                f"## {item['kind']}/{item['name']} (v{item['version']})\n{item['content']}"
                for item in rules
            )
        )
    if skills:
        sections.append(
            "# Active administrator-approved business skills\n\n"
            "These skills guide methods and do not grant new permissions or override business rules.\n\n"
            + "\n\n".join(
                f"## skill/{item['name']} (v{item['version']})\n{item['content']}"
                for item in skills
            )
        )
    return "\n\n".join(sections)
