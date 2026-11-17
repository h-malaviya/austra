"""Load platform skills from Markdown without seeding business policies."""

from dataclasses import dataclass
from hashlib import sha256
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
REVIEWED_SEMANTIC_SKILLS = ("eliciting-requirements", "modeling-organization")
RETRIEVAL_SKILLS = ("retrieving-evidence",)
SUPERVISOR_SKILLS = ("triaging-feedback",)


@dataclass(frozen=True)
class SkillCard:
    skill_id: str
    path: str
    content_hash: str


def skill_cards() -> tuple[SkillCard, ...]:
    return tuple(
        SkillCard(
            name,
            f"platform_skills/{name}/SKILL.md",
            sha256((SKILL_ROOT / name / "SKILL.md").read_bytes()).hexdigest(),
        )
        for name in PLATFORM_SKILLS
    )


def load_skill(skill_id: str) -> str:
    if skill_id not in PLATFORM_SKILLS + REVIEWED_SEMANTIC_SKILLS + RETRIEVAL_SKILLS + SUPERVISOR_SKILLS:
        raise KeyError(skill_id)
    return (SKILL_ROOT / skill_id / "SKILL.md").read_text(encoding="utf-8")


def semantic_skill_cards() -> tuple[SkillCard, ...]:
    """Reviewed semantic procedures are selected explicitly; legacy loading is unchanged."""
    return tuple(
        SkillCard(name, f"platform_skills/{name}/SKILL.md",
                  sha256((SKILL_ROOT / name / "SKILL.md").read_bytes()).hexdigest())
        for name in REVIEWED_SEMANTIC_SKILLS
    )


def load_skills(files: "Files | None" = None) -> str:
    platform = "\n\n".join(load_skill(name) for name in PLATFORM_SKILLS)
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
