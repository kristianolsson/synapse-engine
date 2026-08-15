import pytest
from services.ingestion.registry import ServiceRegistry, RegistryError
from services.ingestion.main import validate_startup


def test_validate_startup_raises_on_registry_error(tmp_path, monkeypatch):
    # No manifests at all -> registry has zero channels -> RegistryError
    registry = ServiceRegistry.discover(tmp_path)
    with pytest.raises(RegistryError):
        validate_startup(registry, {"email"})
