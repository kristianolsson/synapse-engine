"""Syncs each enabled service's PROTOCOL.md template into the vault and
regenerates the CLAUDE.md service-router marker block. Runs on every
process startup (see main.py) — idempotent, safe to call every boot."""

import logging
import subprocess
from pathlib import Path

from .registry import ServiceRegistry

logger = logging.getLogger(__name__)

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
        logger.warning(
            "%s exists but has no service-router marker block (%s ... %s) — "
            "the service router table cannot be regenerated and will be stale. "
            "Add the two marker lines to the vault's CLAUDE.md around the "
            "service-routing table to enable automatic regeneration.",
            claude_md, ROUTER_START, ROUTER_END,
        )
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
    """Commit (and optionally push) vault changes made by apply().

    Runs at boot, before any listener thread starts, so a git failure here must
    never propagate — a raised exception would crash the process before the bot
    comes up, and under a supervisor that means a crash loop. Matches the
    fail-soft style of core/pipe.py's _sync_git(): log and continue.
    """
    if not changed:
        return

    # Rebase onto the remote first so the subsequent push is less likely to be
    # rejected for divergence. Best-effort: no network (or no remote) is not
    # fatal, we just commit locally and let the next boot catch up.
    try:
        result = subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=vault_path, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "Vault git pull --rebase failed (continuing): %s",
                result.stderr.strip().replace("\n", " ")[:200],
            )
    except Exception as e:
        logger.warning("Vault git pull --rebase exception (continuing): %s", e)

    try:
        subprocess.run(["git", "add", "."], cwd=vault_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "synapse-engine apply(): sync service protocols"],
            cwd=vault_path, check=True, capture_output=True,
        )
        if push:
            subprocess.run(["git", "push"], cwd=vault_path, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        logger.error(
            "Vault git commit/push failed (continuing without it): %s — %s",
            " ".join(e.cmd), stderr.strip().replace("\n", " ")[:200],
        )
    except Exception as e:
        logger.error("Vault git commit/push exception (continuing without it): %s", e)
