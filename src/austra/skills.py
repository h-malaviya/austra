"""Load versioned, approved instructions for internal agents."""

from dataclasses import dataclass
from pathlib import Path

SKILL_ROOT = Path(__file__).parent / "knowledge" / "skills"
BUILTIN_SKILLS = frozenset(
    {"case_planning", "skill_authoring", "workflow_authoring", "system_builder"}
)


@dataclass(frozen=True)
class Skill:
    identifier: str
    version: int
    body: str


def load_builtin_skill(identifier: str) -> Skill:
    if identifier not in BUILTIN_SKILLS:
        raise ValueError("Unknown built-in skill")
    source = (SKILL_ROOT / identifier / "SKILL.md").read_text(encoding="utf-8")
    header, separator, body = source.partition("\n---\n")
    if not source.startswith("---\n") or not separator or not body.strip():
        raise ValueError(f"Invalid skill file: {identifier}")
    metadata = dict(
        line.split(": ", maxsplit=1)
        for line in header.removeprefix("---\n").splitlines()
        if ": " in line
    )
    if metadata.get("id") != identifier or metadata.get("status") != "approved":
        raise ValueError(f"Skill is not approved: {identifier}")
    try:
        version = int(metadata["version"])
    except (KeyError, ValueError) as error:
        raise ValueError(f"Invalid skill version: {identifier}") from error
    if version < 1:
        raise ValueError(f"Invalid skill version: {identifier}")
    return Skill(identifier=identifier, version=version, body=body.strip())
