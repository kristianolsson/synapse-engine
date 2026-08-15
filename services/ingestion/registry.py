"""Discovers and validates the pluggable services under services/ingestion/services/."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


VALID_KINDS = ("channel", "tool")


class RegistryError(Exception):
    """Raised when service discovery or ENABLED_SERVICES validation fails."""


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    kind: Literal["channel", "tool"]
    description: str
    module: str
    mcp_module: Optional[str] = None
    credential_group: Optional[str] = None
    depends_on: list = field(default_factory=list)
    env_vars: dict = field(default_factory=lambda: {"required": [], "optional": []})
    vault_protocol: Optional[str] = None
    router_entry: Optional[dict] = None

    @classmethod
    def from_manifest(cls, data: dict) -> "ServiceSpec":
        return cls(
            name=data["name"],
            kind=data["kind"],
            description=data["description"],
            module=data["module"],
            mcp_module=data.get("mcp_module"),
            credential_group=data.get("credential_group"),
            depends_on=data.get("depends_on", []),
            env_vars=data.get("env_vars", {"required": [], "optional": []}),
            vault_protocol=data.get("vault_protocol"),
            router_entry=data.get("router_entry"),
        )


class ServiceRegistry:
    def __init__(self, services: dict):
        self.services = services

    @classmethod
    def discover(cls, services_dir: Path) -> "ServiceRegistry":
        services = {}
        origins: dict = {}
        for manifest_path in sorted(services_dir.glob("*/manifest.json")):
            try:
                data = json.loads(manifest_path.read_text())
            except json.JSONDecodeError as e:
                raise RegistryError(f"{manifest_path}: invalid JSON — {e}") from e

            if not isinstance(data, dict):
                raise RegistryError(
                    f"{manifest_path}: manifest must be a JSON object, got {type(data).__name__}"
                )

            try:
                spec = ServiceSpec.from_manifest(data)
            except KeyError as e:
                raise RegistryError(
                    f"{manifest_path}: missing required manifest field {e}"
                ) from e

            if spec.kind not in VALID_KINDS:
                raise RegistryError(
                    f"{manifest_path}: invalid kind '{spec.kind}' "
                    f"(must be one of {', '.join(sorted(VALID_KINDS))})"
                )

            if spec.name in services:
                raise RegistryError(
                    f"duplicate service name '{spec.name}' declared by both "
                    f"{origins[spec.name]} and {manifest_path}"
                )

            services[spec.name] = spec
            origins[spec.name] = manifest_path
        return cls(services)

    def validate_enabled(self, enabled_names: set) -> None:
        for name in enabled_names:
            if name not in self.services:
                raise RegistryError(f"ENABLED_SERVICES: unknown service '{name}'")

        for name in enabled_names:
            spec = self.services[name]
            for dep in spec.depends_on:
                if dep not in enabled_names:
                    raise RegistryError(
                        f"service '{name}' requires '{dep}' to also be enabled"
                    )
            for var in spec.env_vars.get("required", []):
                if not os.getenv(var):
                    raise RegistryError(
                        f"service '{name}' is enabled but required env var {var} is not set"
                    )

        channels = {n for n in enabled_names if self.services[n].kind == "channel"}
        if not channels:
            raise RegistryError(
                "ENABLED_SERVICES must include at least one channel (email or telegram)"
            )
