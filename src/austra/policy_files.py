"""Immutable Markdown copies of approved business policies."""

import re
from pathlib import Path


def write_approved_policy(
    database_path: Path, policy_id: str, title: str, version: int, body: str
) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", policy_id):
        raise ValueError("Invalid policy ID")
    folder = database_path.parent / "policies"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{policy_id}.md"
    safe_title = " ".join(title.split())
    source = (
        f"---\npolicy_id: {policy_id}\nversion: {version}\nstatus: approved\n---\n"
        f"# {safe_title}\n\n{body.strip()}\n"
    )
    with target.open("x", encoding="utf-8", newline="\n") as document:
        document.write(source)
    return target
