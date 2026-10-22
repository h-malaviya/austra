"""Allowlisted Compose lifecycle for optional workspace services."""

import json
import os
import subprocess
from pathlib import Path
from typing import Literal

Service = Literal["redis", "mongo", "qdrant"]
Action = Literal["start", "stop"]
SERVICES = {"redis": "cache", "mongo": "nosql", "qdrant": "vector"}


class InfrastructureError(RuntimeError):
    """An approved Compose operation did not complete."""


class Infrastructure:
    def __init__(self, compose_file: str | Path | None = None) -> None:
        self.compose_file = Path(compose_file or os.getenv("AUSTRA_COMPOSE_FILE", "compose.yaml"))
        self.project = os.getenv("AUSTRA_COMPOSE_PROJECT", "austra")

    @property
    def available(self) -> bool:
        return self.compose_file.is_file()

    def _command(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "-p",
            self.project,
            *arguments,
        ]

    def status(self) -> dict[str, str]:
        if not self.available:
            return {service: "unavailable" for service in SERVICES}
        try:
            result = subprocess.run(
                self._command("--profile", "*", "ps", "--all", "--format", "json"),
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise InfrastructureError("Docker Compose status is unavailable") from error
        states = {service: "stopped" for service in SERVICES}
        try:
            output = result.stdout.strip()
            items = (
                json.loads(output)
                if output.startswith("[")
                else [json.loads(line) for line in output.splitlines() if line.strip()]
            )
        except json.JSONDecodeError as error:
            raise InfrastructureError("Docker Compose returned invalid status") from error
        for item in items:
            service = item.get("Service")
            if service in states:
                states[service] = item.get("State", "unknown")
        return states

    def change(self, service: Service, action: Action) -> str:
        if service not in SERVICES or action not in {"start", "stop"}:
            raise ValueError("Unsupported infrastructure change")
        if not self.available:
            raise InfrastructureError("Compose configuration is unavailable")
        arguments = ("up", "-d", "--no-deps", service) if action == "start" else ("stop", service)
        try:
            subprocess.run(
                self._command(*arguments),
                capture_output=True,
                text=True,
                check=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise InfrastructureError(f"Could not {action} {service}") from error
        return self.status()[service]
