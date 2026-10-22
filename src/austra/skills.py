"""Load platform skills from Markdown without seeding business policies."""

from pathlib import Path

SKILL_ROOT = Path(__file__).parent / "platform_skills"
PLATFORM_SKILLS = ("foundation", "skill_authoring", "structure_authoring", "credential_setup")


def load_skills() -> str:
    return "\n\n".join(
        (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8") for name in PLATFORM_SKILLS
    )
