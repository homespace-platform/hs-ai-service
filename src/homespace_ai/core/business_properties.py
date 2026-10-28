"""Required non-secret settings, independent of the process working directory."""
from pathlib import Path

PROPERTIES_FILE = Path(__file__).resolve().parents[1] / "application.properties"


def _properties() -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in PROPERTIES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        key, separator, value = line.partition("=")
        if not separator or key.strip() in properties:
            raise ValueError("Invalid or duplicate entry in AI application.properties")
        properties[key.strip()] = value.strip()
    return properties


def nearby_query_enabled() -> bool:
    value = _properties().get("nearby.query-enabled")
    if value not in {"true", "false"}:
        raise ValueError("AI application.properties must explicitly set nearby.query-enabled=true or false")
    return value == "true"


def nearby_default_radius_m() -> int:
    value = _properties().get("nearby.default-radius-m", "")
    if not value.isdigit() or not 100 <= int(value) <= 5000:
        raise ValueError("AI application.properties must explicitly set nearby.default-radius-m between 100 and 5000")
    return int(value)
