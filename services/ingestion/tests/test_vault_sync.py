import json
from pathlib import Path
import pytest
from services.ingestion.registry import ServiceRegistry
from services.ingestion.vault_sync import apply


def make_service(services_dir, name, protocol_content=None, router_entry=None, vault_protocol=None):
    folder = services_dir / name
    folder.mkdir(parents=True)
    manifest = {
        "name": name, "kind": "tool", "description": name,
        "module": f"services.ingestion.services.{name}.cli",
        "mcp_module": None, "credential_group": None, "depends_on": [],
        "env_vars": {"required": [], "optional": []},
        "vault_protocol": vault_protocol,
        "router_entry": router_entry,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))
    if protocol_content is not None:
        (folder / "PROTOCOL.md").write_text(protocol_content)


def test_apply_creates_protocol_file_in_empty_vault(tmp_path):
    services_dir = tmp_path / "services"
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "CLAUDE.md").write_text("# Header\n<!-- SERVICE_ROUTER_START -->\n<!-- SERVICE_ROUTER_END -->\n# Footer\n")
    make_service(services_dir, "calendar", protocol_content="calendar rules",
                 vault_protocol="calendar/PROTOCOL.md",
                 router_entry={"classification": "Calendar", "routing_rule": "Load calendar/PROTOCOL.md"})
    registry = ServiceRegistry.discover(services_dir)

    changed = apply(registry, {"calendar"}, vault, services_dir)

    assert changed is True
    assert (vault / "calendar" / "PROTOCOL.md").read_text() == "calendar rules"


def test_apply_is_noop_when_vault_already_matches(tmp_path):
    services_dir = tmp_path / "services"
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "CLAUDE.md").write_text("# Header\n<!-- SERVICE_ROUTER_START -->\n<!-- SERVICE_ROUTER_END -->\n# Footer\n")
    make_service(services_dir, "calendar", protocol_content="calendar rules",
                 vault_protocol="calendar/PROTOCOL.md",
                 router_entry={"classification": "Calendar", "routing_rule": "Load calendar/PROTOCOL.md"})
    registry = ServiceRegistry.discover(services_dir)
    apply(registry, {"calendar"}, vault, services_dir)  # first run creates it

    changed = apply(registry, {"calendar"}, vault, services_dir)  # second run

    assert changed is False


def test_apply_overwrites_hand_edited_protocol_file(tmp_path):
    services_dir = tmp_path / "services"
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "CLAUDE.md").write_text("<!-- SERVICE_ROUTER_START -->\n<!-- SERVICE_ROUTER_END -->\n")
    make_service(services_dir, "calendar", protocol_content="template rules",
                 vault_protocol="calendar/PROTOCOL.md",
                 router_entry={"classification": "Calendar", "routing_rule": "Load calendar/PROTOCOL.md"})
    (vault / "calendar").mkdir()
    (vault / "calendar" / "PROTOCOL.md").write_text("hand-edited rules")
    registry = ServiceRegistry.discover(services_dir)

    changed = apply(registry, {"calendar"}, vault, services_dir)

    assert changed is True
    assert (vault / "calendar" / "PROTOCOL.md").read_text() == "template rules"


def test_apply_regenerates_router_marker_block_only(tmp_path):
    services_dir = tmp_path / "services"
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "CLAUDE.md").write_text(
        "# Personal mandates here\n<!-- SERVICE_ROUTER_START -->\nstale content\n<!-- SERVICE_ROUTER_END -->\n# Footer here\n"
    )
    make_service(services_dir, "calendar", protocol_content="rules",
                 vault_protocol="calendar/PROTOCOL.md",
                 router_entry={"classification": "Calendar", "routing_rule": "Load calendar/PROTOCOL.md"})
    registry = ServiceRegistry.discover(services_dir)

    apply(registry, {"calendar"}, vault, services_dir)

    content = (vault / "CLAUDE.md").read_text()
    assert "# Personal mandates here" in content
    assert "# Footer here" in content
    assert "stale content" not in content
    assert "Calendar" in content and "Load calendar/PROTOCOL.md" in content
