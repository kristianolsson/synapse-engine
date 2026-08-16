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
CLASSIFICATION_START = "<!-- CLASSIFICATION_START -->"
CLASSIFICATION_END = "<!-- CLASSIFICATION_END -->"

# Classification buckets that aren't tied to any service — always present,
# regardless of which services are enabled.
_CORE_CLASSIFICATIONS_BEFORE = [
    ("TODO / Task", "Deferred items for the USER to act on later (e.g., logging tasks for future tracking)"),
    ("Link URL", "A URL to save and categorize"),
    ("Question / Work", "Requests for the AI to execute immediately (e.g., questions, web research, complex live "
                         "tasks). **You must use web-search tools to retrieve facts. NEVER guess or rely solely "
                         "on pre-trained knowledge.**"),
    ("Project", "Managing personal projects"),
]
_CORE_CLASSIFICATIONS_AFTER = [
    ("Undefined", "No clear classification"),
]


def _router_table(registry: ServiceRegistry, enabled: set) -> str:
    rows = ["| Type | Description |", "|---|---|"]
    for name in sorted(enabled):
        spec = registry.services[name]
        if spec.router_entry:
            rows.append(f"| **{spec.router_entry['classification']}** | {spec.router_entry['routing_rule']} |")
    return "\n".join(rows)


def _classification_table(registry: ServiceRegistry, enabled: set) -> str:
    rows = ["| Type | Description |", "|---|---|"]
    rows += [f"| **{name}** | {desc} |" for name, desc in _CORE_CLASSIFICATIONS_BEFORE]
    for name in sorted(enabled):
        spec = registry.services[name]
        if spec.router_entry and spec.router_entry.get("classification_description"):
            rows.append(
                f"| **{spec.router_entry['classification']}** | {spec.router_entry['classification_description']} |"
            )
    rows += [f"| **{name}** | {desc} |" for name, desc in _CORE_CLASSIFICATIONS_AFTER]
    return "\n".join(rows)


def _replace_marker_block(claude_md: Path, content: str, start: str, end: str, table: str, label: str) -> str:
    if start not in content or end not in content:
        logger.warning(
            "%s exists but has no %s marker block (%s ... %s) — the table cannot be "
            "regenerated and will be stale. Add the two marker lines to the vault's "
            "CLAUDE.md to enable automatic regeneration.",
            claude_md, label, start, end,
        )
        return content
    before, rest = content.split(start, 1)
    _, after = rest.split(end, 1)
    return f"{before}{start}\n{table}\n{end}{after}"


def _regenerate_router(vault_path: Path, registry: ServiceRegistry, enabled: set) -> bool:
    claude_md = vault_path / "CLAUDE.md"
    if not claude_md.exists():
        return False
    original = claude_md.read_text()
    content = _replace_marker_block(
        claude_md, original, ROUTER_START, ROUTER_END, _router_table(registry, enabled), "service-router"
    )
    content = _replace_marker_block(
        claude_md, content, CLASSIFICATION_START, CLASSIFICATION_END,
        _classification_table(registry, enabled), "classification",
    )
    if content == original:
        return False
    claude_md.write_text(content)
    return True


def apply(registry: ServiceRegistry, enabled: set, vault_path: Path, services_dir: Path) -> list:
    """Returns the vault-relative paths written, so callers can stage exactly
    those files rather than everything in the working tree."""
    changed_paths = []

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
            changed_paths.append(spec.vault_protocol)

    if _regenerate_router(vault_path, registry, enabled):
        changed_paths.append("CLAUDE.md")

    return changed_paths


def commit_and_push_if_changed(vault_path: Path, changed_paths: list, push: bool = True) -> None:
    """Commit (and optionally push) the vault paths written by apply().

    Runs at boot, before any listener thread starts, so a git failure here must
    never propagate — a raised exception would crash the process before the bot
    comes up, and under a supervisor that means a crash loop. Matches the
    fail-soft style of core/pipe.py's _sync_git(): log and continue.

    Stages only `changed_paths` (not the whole working tree) so unrelated,
    concurrently in-progress vault edits are never swept into this commit.
    Commits first, then rebases onto the remote — pull --rebase requires a
    clean working tree, and apply() has already written to it — matching the
    add/commit/pull --rebase/push order used by reminder/cli.py.
    """
    if not changed_paths:
        return

    try:
        subprocess.run(["git", "add", "--", *changed_paths], cwd=vault_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "synapse-engine apply(): sync service protocols"],
            cwd=vault_path, check=True, capture_output=True,
        )

        # Rebase onto the remote now that our changes are safely committed, so
        # the subsequent push is less likely to be rejected for divergence.
        # Best-effort: no network (or no remote) is not fatal, we just push
        # (or fail to) and let the next boot catch up.
        try:
            result = subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=vault_path, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.warning(
                    "Vault git pull --rebase failed, aborting rebase (continuing): %s",
                    result.stderr.strip().replace("\n", " ")[:200],
                )
                # A conflict leaves .git/rebase-merge/ in place with HEAD detached.
                # Abort it so the vault isn't left mid-rebase — otherwise the next
                # boot's commit would land on the detached HEAD instead of the
                # branch, silently orphaning it. Harmless (and a no-op) if the
                # failure wasn't a conflict and no rebase is actually in progress.
                subprocess.run(
                    ["git", "rebase", "--abort"], cwd=vault_path, capture_output=True, timeout=30,
                )
        except Exception as e:
            logger.warning("Vault git pull --rebase exception (continuing): %s", e)

        if push:
            subprocess.run(["git", "push"], cwd=vault_path, check=True, capture_output=True, timeout=60)
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
