"""Private organization-scoped credentials for Austra's model provider."""

from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from austra.integrations.secrets import ConnectionSecrets

ANTHROPIC_CONNECTION_ID = uuid5(NAMESPACE_URL, "austra:model:anthropic")


class ModelCredentials:
    def __init__(self, root: Path) -> None:
        self.vault = ConnectionSecrets(root)

    def get(self, organization_id: UUID) -> str:
        try:
            return self.vault.read(organization_id, ANTHROPIC_CONNECTION_ID)["ANTHROPIC_API_KEY"]
        except (KeyError, RuntimeError):
            return ""

    def save(self, organization_id: UUID, key: str) -> None:
        self.vault.save(organization_id, ANTHROPIC_CONNECTION_ID, {"ANTHROPIC_API_KEY": key})
