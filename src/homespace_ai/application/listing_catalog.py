"""Core field catalog v2 for planning and validation. Core still compiles SQL and re-validates."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

SUPPORTED_VERSION = 2
NUMERIC = {"NUMBER", "INTEGER"}
MONEY_UNITS = {"VND", "VND/tháng"}


class CatalogError(ValueError):
    """A planned criterion does not fit the catalog; never silently dropped or treated as no results."""

    def __init__(self, reason: str, field: str | None, message: str, allowed: list[str] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.field = field
        self.allowed = allowed or []


_MONEY = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(tr|triệu|trieu|củ|cu|m|k|nghìn|nghin|ngàn|ngan)?\s*(?:đ|vnd|đồng)?\s*$", re.IGNORECASE)


def money(value: Any) -> Decimal | None:
    """'3tr', '3,5 triệu', '3 củ', '3500k', '3000000' as VND. Plain small numbers are not guessed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).strip().lower().replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        return Decimal(text.replace(".", ""))
    match = _MONEY.match(text)
    if not match:
        return None
    number = Decimal(match[1].replace(",", "."))
    unit = (match[2] or "").lower()
    if unit in {"tr", "triệu", "trieu", "củ", "cu", "m"}:
        return number * 1_000_000
    if unit in {"k", "nghìn", "nghin", "ngàn", "ngan"}:
        return number * 1_000
    return number


class SearchCatalog:
    def __init__(self, raw: dict[str, Any]) -> None:
        if not isinstance(raw, dict) or raw.get("version") != SUPPORTED_VERSION or not isinstance(raw.get("fields"), list):
            raise CatalogError("UNSUPPORTED_VERSION", None, "Unsupported Core field catalog")
        self.version: int = raw["version"]
        self.catalog_version = str(raw.get("catalogVersion") or "")
        self.fields: dict[str, dict[str, Any]] = {}
        for spec in raw["fields"]:
            if isinstance(spec, dict) and isinstance(spec.get("code"), str) and isinstance(spec.get("operators"), list):
                self.fields[spec["code"]] = spec
        self.sorts = [s for s in raw.get("sorts") or [] if isinstance(s, str)]
        self.unsupported = [u for u in raw.get("unsupported") or [] if isinstance(u, dict) and isinstance(u.get("code"), str)]
        self.kinds = [k for k in raw.get("nearbyKinds") or [] if isinstance(k, dict) and isinstance(k.get("code"), str)]
        self.context_fields = [c for c in raw.get("contextFields") or [] if isinstance(c, str)]

    def searchable(self) -> list[str]:
        return sorted(code for code, spec in self.fields.items() if spec.get("role") != "DISPLAY_ONLY")

    def label(self, code: str) -> str:
        spec = self.fields.get(code)
        return str(spec.get("label") or code) if spec else code

    def value_label(self, code: str, value: Any) -> str:
        spec = self.fields.get(code) or {}
        for choice in spec.get("values") or []:
            if isinstance(choice, dict) and str(choice.get("code")) == str(value).upper():
                return str(choice.get("label"))
        return str(value)

    def is_money(self, code: str) -> bool:
        spec = self.fields.get(code) or {}
        return spec.get("kind") == "NUMBER" and spec.get("unit") in MONEY_UNITS

    def requires_context(self, code: str) -> list[str]:
        return [c for c in (self.fields.get(code) or {}).get("requiresContext") or [] if isinstance(c, str)]

    def validate(self, constraint: dict[str, Any]) -> dict[str, Any]:
        """Typed, catalog-checked criterion. Raises CatalogError with a reason code."""
        code, op = constraint.get("field"), constraint.get("op")
        spec = self.fields.get(code) if isinstance(code, str) else None
        if spec is None:
            raise CatalogError("UNKNOWN_FIELD", code if isinstance(code, str) else None, "Unknown listing field")
        if spec.get("role") == "DISPLAY_ONLY":
            raise CatalogError("NOT_FILTERABLE", code, "Display-only field")
        kind = spec.get("kind")
        strength = constraint.get("strength", "hard")
        if op == "approx":
            if kind not in NUMERIC or strength != "preference":
                raise CatalogError("UNSUPPORTED_OPERATOR", code, "Approximate values are preferences on numbers", spec["operators"])
        elif op not in spec["operators"]:
            raise CatalogError("UNSUPPORTED_OPERATOR", code, "Operator is not valid for this field", spec["operators"])
        allowed = [str(c.get("code")) for c in spec.get("values") or [] if isinstance(c, dict)]
        raw = constraint.get("value")
        if op == "in":
            values = raw if isinstance(raw, list) else [v for v in str(raw).split(",") if v.strip()]
            if not 1 <= len(values) <= 20:
                raise CatalogError("INVALID_VALUE", code, "in needs 1 to 20 values", allowed)
            value: Any = [self._value(code, kind, v, allowed) for v in values]
        else:
            value = self._value(code, kind, raw, allowed)
        return {**constraint, "field": code, "op": op, "value": value, "strength": strength}

    def _value(self, code: str, kind: Any, raw: Any, allowed: list[str]) -> Any:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise CatalogError("INVALID_VALUE", code, "A value is required", allowed)
        text = str(raw).strip()
        if kind == "ENUM":
            value = text.upper()
            if value not in allowed:
                raise CatalogError("INVALID_VALUE", code, "Value is not in the catalog", allowed)
            return value
        if kind == "BOOLEAN":
            if text.lower() not in {"true", "false"}:
                raise CatalogError("INVALID_VALUE", code, "Boolean must be true or false", ["true", "false"])
            return text.lower() == "true"
        if kind == "INTEGER":
            if not re.fullmatch(r"-?\d{1,9}", text):
                raise CatalogError("INVALID_VALUE", code, "Integer expected")
            return int(text)
        if kind == "NUMBER":
            number = money(text) if self.is_money(code) else None
            if number is None:
                try:
                    number = Decimal(text.replace(",", "."))
                except InvalidOperation:
                    raise CatalogError("INVALID_VALUE", code, "Number expected") from None
            if not number.is_finite() or number < 0:
                raise CatalogError("INVALID_VALUE", code, "Number expected")
            return format(number.normalize(), "f") if number != number.to_integral() else str(int(number))
        if kind == "DATE":
            try:
                return date.fromisoformat(text[:10]).isoformat()
            except ValueError:
                raise CatalogError("INVALID_VALUE", code, "Date must be YYYY-MM-DD") from None
        if len(text) > 160:
            raise CatalogError("INVALID_VALUE", code, "Text is too long")
        return text

    def prompt_text(self) -> str:
        """Compact catalog for the planner: code, kind, operators, unit, categories and enum codes."""
        lines = []
        for code in self.searchable():
            spec = self.fields[code]
            parts = [code, str(spec.get("kind")), "/".join(spec.get("operators") or [])]
            if spec.get("unit"):
                parts.append("unit=" + str(spec["unit"]))
            cats = spec.get("categories") or []
            if cats and len(cats) < 3:
                parts.append("for=" + "/".join(cats))
            values = [str(c.get("code")) for c in spec.get("values") or [] if isinstance(c, dict)]
            if values:
                parts.append("values=" + "|".join(values))
            if spec.get("requiresContext"):
                parts.append("needs=" + "/".join(spec["requiresContext"]))
            parts.append(str(spec.get("label") or ""))
            lines.append(" ; ".join(parts))
        return "\n".join(lines)
