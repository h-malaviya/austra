"""CEO-provided initial purchasing limits and deliberate review gaps."""

from pathlib import Path

from austra.store import Store

TITLE = "Initial PO approval guidance"
BODY = (
    (Path(__file__).parent / "knowledge" / "policies" / "initial_po.md")
    .read_text(encoding="utf-8")
    .strip()
)


def ensure_initial_policy(store: Store) -> None:
    if any(policy["title"] == TITLE for policy in store.list_policies()):
        return
    policy = store.add_policy(TITLE, BODY)
    store.activate_policy(policy["id"], "CEO instruction")
