import pytest
from homespace_ai.core import business_properties
from homespace_ai.core.config import Settings


def test_business_flag_comes_from_properties_and_ignores_old_env_name(tmp_path, monkeypatch):
    path = tmp_path / "application.properties"
    path.write_text("nearby.query-enabled=false\nnearby.default-radius-m=2000\nlisting.search-utc-offset=+07:00\n", encoding="utf-8")
    monkeypatch.setattr(business_properties, "PROPERTIES_FILE", path)
    monkeypatch.setenv("NEARBY_QUERY_ENABLED", "true")
    assert Settings(_env_file=None).nearby_query_enabled is False
    path.write_text("nearby.query-enabled=true\nnearby.default-radius-m=2500\nlisting.search-utc-offset=+07:00\n", encoding="utf-8")
    assert Settings(_env_file=None).nearby_query_enabled is True
    assert Settings(_env_file=None).nearby_default_radius_m == 2500


@pytest.mark.parametrize("radius", ["", "0", "5001", "invalid"])
def test_missing_or_invalid_nearby_radius_fails_startup(tmp_path, monkeypatch, radius):
    path = tmp_path / "application.properties"
    path.write_text(f"nearby.query-enabled=true\nnearby.default-radius-m={radius}\n", encoding="utf-8")
    monkeypatch.setattr(business_properties, "PROPERTIES_FILE", path)
    with pytest.raises(ValueError, match="must explicitly set nearby.default-radius-m"):
        Settings(_env_file=None)


@pytest.mark.parametrize("content", ["", "nearby.query-enabled=", "nearby.query-enabled=invalid"])
def test_missing_or_invalid_business_setting_fails_startup(tmp_path, monkeypatch, content):
    path = tmp_path / "application.properties"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(business_properties, "PROPERTIES_FILE", path)
    with pytest.raises(ValueError, match="must explicitly set nearby.query-enabled"):
        Settings(_env_file=None)


def test_missing_properties_file_fails_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(business_properties, "PROPERTIES_FILE", tmp_path / "missing.properties")
    with pytest.raises(FileNotFoundError):
        Settings(_env_file=None)
