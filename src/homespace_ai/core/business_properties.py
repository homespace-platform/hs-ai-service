"""Required non-secret settings, independent of the process working directory."""
import re
from datetime import timedelta
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


def listing_search_utc_offset() -> timedelta:
    value = _properties().get("listing.search-utc-offset", "")
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not match or int(match[2]) > 14 or int(match[3]) >= 60:
        raise ValueError("AI application.properties must explicitly set listing.search-utc-offset as +HH:MM")
    offset = timedelta(hours=int(match[2]), minutes=int(match[3]))
    return offset if match[1] == "+" else -offset


def nearby_default_radius_m() -> int:
    value = _properties().get("nearby.default-radius-m", "")
    if not value.isdigit() or not 100 <= int(value) <= 5000:
        raise ValueError("AI application.properties must explicitly set nearby.default-radius-m between 100 and 5000")
    return int(value)


def value_advice_timeout_seconds() -> float:
    value = _properties().get("listing.value-advice.timeout-seconds", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", value) or not 1 <= float(value) <= 20:
        raise ValueError("AI application.properties must explicitly set listing.value-advice.timeout-seconds between 1 and 20")
    return float(value)
