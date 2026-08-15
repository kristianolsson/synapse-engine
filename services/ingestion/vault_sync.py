"""Syncs each enabled service's PROTOCOL.md template into the vault and
regenerates the CLAUDE.md service-router marker block. Runs on every
process startup (see main.py) — idempotent, safe to call every boot."""

import subprocess
from pathlib import Path

from .registry import ServiceRegistry

ROUTER_START = "<!-- SERVICE_ROUTER_START -->"
ROUTER_END = "<!-- SERVICE_ROUTER_END -->"


def _router_table(registry: ServiceRegistry, enabled: set) -> str:
    rows = ["| Type | Description |", "|---|---|"]
    for name in sorted(enabled):
        spec = registry.services[name]
        if spec.router_entry:
            rows.append(f"| **{spec.router_entry['classification']}** | {spec.router_entry['routing_rule']} |")
    return "\n".join(rows)


def _regenerate_router(vault_path: Path, registry: ServiceRegistry, enabled: set) -> bool:
    claude_md = vault_path / "CLAUDE.md"
    if not claude_md.exists():
        return False
    content = claude_md.read_text()
    if ROUTER_START not in content or ROUTER_END not in content:
        return False
    before, rest = content.split(ROUTER_START, 1)
    _, after = rest.split(ROUTER_END, 1)
    new_content = f"{before}{ROUTER_START}\n{_router_table(registry, enabled)}\n{ROUTER_END}{after}"
    if new_content == content:
        return False
    claude_md.write_text(new_content)
    return True


def apply(registry: ServiceRegistry, enabled: set, vault_path: Path, services_dir: Path) -> bool:
    changed = False

    for name in sorted(enabled):
        spec = registry.services[name]
        if not spec.vault_protocol:
            continue
        template_path = services_dir / name / "PROTOCOL.md"
        if not template_path.exists():
            continue
        template_content = template_path.read_text()
        vault_protocol_path = vault_path / spec.vault_protocol
        vault_protocol_path.parent.mkdir(parents=True, exist_ok=True)
        if not vault_protocol_path.exists() or vault_protocol_path.read_text() != template_content:
            vault_protocol_path.write_text(template_content)
            changed = True

    if _regenerate_router(vault_path, registry, enabled):
        changed = True

    return changed


def commit_and_push_if_changed(vault_path: Path, changed: bool, push: bool = True) -> None:
    if not changed:
        return
    subprocess.run(["git", "add", "."], cwd=vault_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "synapse-engine apply(): sync service protocols"],
        cwd=vault_path, check=True, capture_output=True,
    )
    if push:
        subprocess.run(["git", "push"], cwd=vault_path, check=True, capture_output=True)
