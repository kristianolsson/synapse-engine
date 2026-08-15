import json
import pytest
from pathlib import Path
from services.ingestion.registry import ServiceRegistry, RegistryError


@pytest.fixture
def services_dir(tmp_path):
    d = tmp_path / "services"
    d.mkdir()
    return d


def write_manifest(services_dir, folder_name, manifest_name=None, **overrides):
    """folder_name is the directory under services_dir; manifest_name lets a
    test give the manifest a different 'name' field than its folder (e.g.
    folder options_bot/ but manifest name "options-bot") — passing the same
    value via both a positional arg and an overrides kwarg raises TypeError,
    so this is a separate parameter, not an override."""
    folder = services_dir / folder_name
    folder.mkdir()
    manifest = {
        "name": manifest_name or folder_name,
        "kind": "tool",
        "description": f"{folder_name} service",
        "module": f"services.ingestion.services.{folder_name}.cli",
        "mcp_module": None,
        "credential_group": None,
        "depends_on": [],
        "env_vars": {"required": [], "optional": []},
        "vault_protocol": None,
        "router_entry": None,
    }
    manifest.update(overrides)
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_discovers_all_manifests(services_dir):
    write_manifest(services_dir, "calendar")
    write_manifest(services_dir, "email", kind="channel")
    registry = ServiceRegistry.discover(services_dir)
    assert set(registry.services.keys()) == {"calendar", "email"}


def test_validate_enabled_requires_at_least_one_channel(services_dir):
    write_manifest(services_dir, "calendar", kind="tool")
    registry = ServiceRegistry.discover(services_dir)
    with pytest.raises(RegistryError, match="at least one"):
        registry.validate_enabled({"calendar"})


def test_validate_enabled_rejects_unknown_service(services_dir):
    write_manifest(services_dir, "email", kind="channel")
    registry = ServiceRegistry.discover(services_dir)
    with pytest.raises(RegistryError, match="unknown service 'ghost'"):
        registry.validate_enabled({"email", "ghost"})


def test_validate_enabled_checks_required_env_vars(services_dir, monkeypatch):
    write_manifest(services_dir, "email", kind="channel",
                    env_vars={"required": ["EMAIL_ADDRESS"], "optional": []})
    monkeypatch.delenv("EMAIL_ADDRESS", raising=False)
    registry = ServiceRegistry.discover(services_dir)
    with pytest.raises(RegistryError, match="EMAIL_ADDRESS"):
        registry.validate_enabled({"email"})


def test_validate_enabled_checks_depends_on(services_dir):
    write_manifest(services_dir, "etrade", kind="tool")
    write_manifest(services_dir, "options_bot", manifest_name="options-bot",
                    kind="tool", depends_on=["etrade"])
    registry = ServiceRegistry.discover(services_dir)
    with pytest.raises(RegistryError, match="requires 'etrade'"):
        registry.validate_enabled({"options-bot"})
    # Enabling both should pass validation (no channel yet, so add one)
    write_manifest(services_dir, "email", kind="channel")
    registry = ServiceRegistry.discover(services_dir)
    registry.validate_enabled({"etrade", "options-bot", "email"})  # should not raise


def test_discover_rejects_malformed_json(services_dir):
    folder = services_dir / "broken"
    folder.mkdir()
    (folder / "manifest.json").write_text("{ this is not json ]")
    with pytest.raises(RegistryError, match="invalid JSON"):
        ServiceRegistry.discover(services_dir)


def test_discover_reports_offending_path_for_malformed_json(services_dir):
    folder = services_dir / "broken"
    folder.mkdir()
    (folder / "manifest.json").write_text("{")
    with pytest.raises(RegistryError, match="broken/manifest.json"):
        ServiceRegistry.discover(services_dir)


def test_discover_rejects_missing_required_field(services_dir):
    manifest = write_manifest(services_dir, "calendar")
    del manifest["module"]
    (services_dir / "calendar" / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RegistryError, match="missing required manifest field"):
        ServiceRegistry.discover(services_dir)


def test_discover_rejects_invalid_kind(services_dir):
    write_manifest(services_dir, "calendar", kind="widget")
    with pytest.raises(RegistryError, match="invalid kind 'widget'"):
        ServiceRegistry.discover(services_dir)


def test_discover_rejects_duplicate_service_names(services_dir):
    write_manifest(services_dir, "calendar")
    write_manifest(services_dir, "calendar_v2", manifest_name="calendar")
    with pytest.raises(RegistryError) as exc:
        ServiceRegistry.discover(services_dir)
    msg = str(exc.value)
    assert "duplicate service name 'calendar'" in msg
    # Both offending manifest paths are named.
    assert "calendar/manifest.json" in msg
    assert "calendar_v2/manifest.json" in msg
