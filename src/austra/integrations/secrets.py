"""Tenant scoped encrypted credential files outside prompts and database rows."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken


class ConnectionSecrets:
    def __init__(self, root: Path) -> None:
        self.root = root / "integration_secrets"

    def _cipher(self) -> Fernet:
        key = os.getenv("AUSTRA_CONNECTION_ENCRYPTION_KEY", "")
        if not key:
            raise RuntimeError("Connection vault is not configured; set its private encryption key")
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError) as error:
            raise RuntimeError("Connection vault encryption key is invalid") from error

    def _path(self, organization_id: UUID, connection_id: UUID) -> Path:
        return self.root / str(organization_id) / str(connection_id)

    def save(self, organization_id: UUID, connection_id: UUID, values: dict[str, str]) -> None:
        if not values or any(
            not isinstance(value, str)
            or (not value and name != "token")
            or len(value) > 8000
            or "\n" in value
            or "\r" in value
            for name, value in values.items()
        ):
            raise ValueError("Private credential fields must be nonempty single lines")
        path = self._path(organization_id, connection_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        encoded = self._cipher().encrypt(json.dumps(values).encode())
        temporary = path.with_name(f".{uuid4().hex}.tmp")
        temporary.write_bytes(encoded)
        temporary.chmod(0o600)
        temporary.replace(path)

    def read(self, organization_id: UUID, connection_id: UUID) -> dict[str, str]:
        try:
            return json.loads(
                self._cipher().decrypt(self._path(organization_id, connection_id).read_bytes())
            )
        except (FileNotFoundError, InvalidToken) as error:
            raise RuntimeError("Connection credential is unavailable; reconnect") from error

    def revoke(self, organization_id: UUID, connection_id: UUID) -> None:
        self._path(organization_id, connection_id).unlink(missing_ok=True)
