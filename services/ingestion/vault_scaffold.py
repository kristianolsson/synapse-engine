"""Writes a fresh, empty vault from vault_template/ — CLAUDE.md plus the
always-needed tasks/links/projects modules. Pure file-writing, no git; the
caller (./synapse setup) handles git init/commit/push separately, since
that step branches on target (mac vs. QNAP containerized git)."""

from pathlib import Path

PERSONAL_CONTEXT_PLACEHOLDER = "{{PERSONAL_CONTEXT}}"

# (vault-relative destination, template-relative source) pairs copied
# verbatim, no substitution.
_STATIC_FILES = [
    ("tasks/PROTOCOL.md", "core/tasks/PROTOCOL.md"),
    ("tasks/master_todos.md", "core/tasks/master_todos.md"),
    ("links/PROTOCOL.md", "core/links/PROTOCOL.md"),
    ("links/links.md", "core/links/links.md"),
    ("projects/PROTOCOL.md", "core/projects/PROTOCOL.md"),
    ("projects/templates/project_template.md", "core/projects/templates/project_template.md"),
]


def scaffold(vault_path: Path, personal_context: str, template_dir: Path) -> None:
    """Writes a fresh vault skeleton into vault_path.

    Raises FileExistsError if vault_path already exists and is non-empty —
    scaffolding must never run against a populated vault.
    """
    if vault_path.exists() and any(vault_path.iterdir()):
        raise FileExistsError(f"{vault_path} already exists and is not empty — refusing to scaffold")

    vault_path.mkdir(parents=True, exist_ok=True)

    claude_md_template = (template_dir / "CLAUDE.md.template").read_text()
    claude_md_content = claude_md_template.replace(PERSONAL_CONTEXT_PLACEHOLDER, personal_context)
    (vault_path / "CLAUDE.md").write_text(claude_md_content)

    for dest_rel, src_rel in _STATIC_FILES:
        dest_path = vault_path / dest_rel
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text((template_dir / src_rel).read_text())
