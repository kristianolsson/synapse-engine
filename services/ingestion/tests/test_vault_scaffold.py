from pathlib import Path

import pytest

from services.ingestion.vault_scaffold import scaffold

TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "vault_template"


def test_scaffold_writes_claude_md_with_personal_context(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault, "Jane Doe (jdoe), living in Austin, TX", TEMPLATE_DIR)
    content = (vault / "CLAUDE.md").read_text()
    assert "Jane Doe (jdoe), living in Austin, TX" in content
    assert "{{PERSONAL_CONTEXT}}" not in content


def test_scaffold_claude_md_has_empty_router_markers(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault, "Jane Doe", TEMPLATE_DIR)
    content = (vault / "CLAUDE.md").read_text()
    assert "<!-- SERVICE_ROUTER_START -->\n<!-- SERVICE_ROUTER_END -->" in content


def test_scaffold_copies_core_protocol_files_verbatim(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault, "Jane Doe", TEMPLATE_DIR)
    assert (vault / "tasks" / "PROTOCOL.md").read_text() == (TEMPLATE_DIR / "core" / "tasks" / "PROTOCOL.md").read_text()
    assert (vault / "links" / "PROTOCOL.md").read_text() == (TEMPLATE_DIR / "core" / "links" / "PROTOCOL.md").read_text()
    assert (vault / "projects" / "PROTOCOL.md").read_text() == (TEMPLATE_DIR / "core" / "projects" / "PROTOCOL.md").read_text()


def test_scaffold_writes_starter_data_files(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault, "Jane Doe", TEMPLATE_DIR)
    assert (vault / "tasks" / "master_todos.md").exists()
    assert (vault / "links" / "links.md").exists()
    assert (vault / "projects" / "templates" / "project_template.md").exists()


def test_scaffold_refuses_nonempty_directory(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "existing_note.md").write_text("do not touch")
    with pytest.raises(FileExistsError):
        scaffold(vault, "Jane Doe", TEMPLATE_DIR)
    assert (vault / "existing_note.md").read_text() == "do not touch"


def test_scaffold_allows_existing_empty_directory(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    scaffold(vault, "Jane Doe", TEMPLATE_DIR)
    assert (vault / "CLAUDE.md").exists()


def test_scaffold_allows_missing_directory(tmp_path):
    vault = tmp_path / "does_not_exist_yet"
    scaffold(vault, "Jane Doe", TEMPLATE_DIR)
    assert (vault / "CLAUDE.md").exists()
